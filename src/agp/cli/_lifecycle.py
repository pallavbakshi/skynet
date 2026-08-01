"""Agent lifecycle commands — up, down, interrupt."""

from __future__ import annotations

import typer

from agp.cli import app
from agp.cli._helpers import (
    _cli_client,
    _format_http_error,
    _make_client,
    _print_banner,
)
from agp.cli._infra import _runtime_binding_warning


def _poll_agent_ready(
    client, agent_id: str, *, timeout: float = 120.0, heartbeat_interval: float = 5.0
) -> dict:
    """Poll until agent reaches idle status.  Returns agent dict.

    Currently the server transitions agents to IDLE synchronously inside
    the ``POST /agents/up`` response, so the first poll always succeeds.
    The loop exists for forward-compatibility with async provisioning
    (e.g. waiting for a runtime to bind).
    """
    import time

    import httpx as _httpx

    start = time.monotonic()
    deadline = start + timeout
    last_heartbeat = start

    while time.monotonic() < deadline:
        try:
            agent = client.get_agent(agent_id)
        except _httpx.HTTPStatusError as exc:
            # Non-retryable HTTP errors — bail immediately
            if exc.response.status_code in (401, 403, 404):
                raise
            # 5xx or other — keep polling
        except _httpx.TransportError:
            # Network-level failures (timeout, DNS, connection reset) — keep polling
            pass
        else:
            status = agent.get("status")
            if status == "idle":
                return agent
            # Terminal statuses will never become idle — exit early
            if status in ("error", "failed"):
                return agent

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start)
            typer.echo(f"[..] Waiting for agent registration... ({elapsed}s elapsed)")
            last_heartbeat = now

        time.sleep(1)

    # Final check — guarded so a down server doesn't produce a raw traceback
    try:
        return client.get_agent(agent_id)
    except (_httpx.HTTPStatusError, _httpx.TransportError):
        return {"status": "unknown"}


@app.command()
def up(
    capability_name: str = typer.Argument(..., help="Capability name (must match agp ls output)."),
    server_url: str = typer.Option(None, help="CP URL."),
    agent_id: str = typer.Option(None, "--agent-id", help="Explicit agent ID (default: auto-generated)."),
    workspace_ref: str = typer.Option(None, "--workspace", help="Working directory for the agent."),
    timeout: int = typer.Option(120, help="Max seconds to wait for agent to become idle."),
    max_retries: int = typer.Option(3, help="Provisioning retry attempts on server error."),
) -> None:
    """Provision an agent from a capability. Blocks until the agent is IDLE.

    Resolves the capability by display name (as shown in agp ls), creates an
    agent, and waits for it to become ready.
    """
    import time

    import httpx as _httpx

    with _make_client(server_url) as client:
        # Self-registration model: pass capability name directly as a
        # capability string.  No need to resolve against /capabilities table.
        typer.echo(f"[..] Provisioning capability '{capability_name}'...")

        # Retry loop for provisioning
        data: dict | None = None
        for attempt in range(1, max_retries + 1):
            typer.echo(f"[..] Registering agent... (Attempt {attempt}/{max_retries})")
            try:
                data = client.register_agent(
                    agent_id=agent_id,
                    capabilities=[capability_name],
                    workspace_ref=workspace_ref,
                )
                break
            except _httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 409:
                    detail = agent_id or "(auto-generated)"
                    _print_banner("ERROR", "Provisioning Failed")
                    typer.echo(f"FATAL: Agent already exists: {detail}")
                    raise typer.Exit(1)
                if status >= 500 and attempt < max_retries:
                    typer.echo(f"[..] Server error. Retrying... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(2)
                    continue
                _print_banner("ERROR", "Provisioning Failed")
                typer.echo(f"FATAL: Could not bring up '{capability_name}' after {attempt} attempts.")
                typer.echo(f"REASON: {exc}")
                raise typer.Exit(1)
            except _httpx.TransportError:
                if attempt < max_retries:
                    typer.echo(f"[..] Network error. Retrying... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(2)
                    continue
                _print_banner("ERROR", "Provisioning Failed")
                typer.echo(f"FATAL: Could not reach server after {max_retries} attempts.")
                raise typer.Exit(1)

        if data is None:
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Could not bring up '{capability_name}' after {max_retries} attempts.")
            typer.echo("REASON: Infrastructure unavailable or insufficient resources.")
            typer.echo("ACTION: Pivot your strategy or try a different capability.")
            raise typer.Exit(1)

        resolved_agent_id = data["agent_id"]

        # Print agent ID early so the user can recover if polling fails
        typer.echo(f"[..] Agent {resolved_agent_id} created. Waiting for IDLE...")

        # Poll until idle
        agent = _poll_agent_ready(client, resolved_agent_id, timeout=timeout)

        if agent.get("status") != "idle":
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Agent {resolved_agent_id} did not reach IDLE within {timeout}s.")
            typer.echo(f"STATUS: {agent.get('status', '?').upper()}")
            typer.echo("ACTION: Check runtime logs or try again.")
            raise typer.Exit(1)

        _print_banner("SUCCESS", "Agent Provisioned Successfully")
        typer.echo(f"CAPABILITY: {capability_name}")
        typer.echo(f"AGENT_ID:   {resolved_agent_id}")
        typer.echo(f"STATUS:     {agent.get('status', 'idle').upper()}")
        typer.echo(f"CWD:        {agent.get('workspace_ref') or '-'}")
        warning = _runtime_binding_warning(client, resolved_agent_id)
        if warning:
            typer.echo(warning)
        typer.echo("-----------------------------------------")
        typer.echo("Ready. You may now route tasks using:")
        typer.echo(f"  agp send {resolved_agent_id} \"your prompt here\"")


@app.command()
def down(
    agent_id: str = typer.Argument(..., help="Agent ID to tear down."),
    server_url: str = typer.Option(None, help="CP URL."),
    force: bool = typer.Option(False, "--force", help="Force teardown even if agent is busy (cancels active jobs)."),
) -> None:
    """Tear down an agent and release its resources.

    If the agent is busy, use --force to cancel active jobs and destroy it.
    Without --force, busy agents will be rejected — use --force explicitly.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        typer.echo(f"[..] Locating agent {agent_id}...")

        try:
            agent = client.get_agent(agent_id)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                _print_banner("ERROR", "Teardown Failed")
                typer.echo(f"FATAL: Agent not found: {agent_id}")
                raise typer.Exit(1)
            raise

        agent_status = agent.get("status", "unknown")

        # Statuses that may have active work — require --force
        _HAS_ACTIVE_WORK = ("busy", "draining")

        if agent_status in _HAS_ACTIVE_WORK and not force:
            _print_banner("ERROR", "Teardown Blocked")
            typer.echo(f"Agent {agent_id} is {agent_status.upper()} (may have active work).")
            typer.echo("Use --force to cancel active jobs and destroy it:")
            typer.echo(f"  agp down {agent_id} --force")
            raise typer.Exit(1)

        # Force-delete in both cases: busy agents had --force, idle agents have no work to drain.
        if agent_status in _HAS_ACTIVE_WORK:
            typer.echo(f"[..] WARNING: Agent is {agent_status.upper()}.")
            typer.echo("[..] Aborting active jobs and clearing queue...")
            mode = "force"
        else:
            typer.echo(f"[..] Agent is {agent_status.upper()}. Proceeding with teardown...")
            mode = "force"

        try:
            result = client.agent_down(agent_id, mode=mode)
        except _httpx.HTTPStatusError as exc:
            # 409 from TOCTOU guard — agent changed state between our check and the call
            if exc.response.status_code == 409:
                try:
                    detail = exc.response.json().get("error", {}).get("message", "")
                except Exception:
                    detail = ""
                if "force" in detail:
                    _print_banner("ERROR", "Teardown Blocked")
                    typer.echo(f"Agent {agent_id} has active work.")
                    typer.echo("Use --force to cancel active jobs and destroy it:")
                    typer.echo(f"  agp down {agent_id} --force")
                else:
                    _print_banner("ERROR", "Teardown Failed")
                    typer.echo(f"FATAL: {detail or exc}")
                raise typer.Exit(1)
            _print_banner("ERROR", "Teardown Failed")
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)

        result_status = result.get("status", "deleted").upper()
        if mode == "force":
            _print_banner("SUCCESS", "Agent Forcefully Destroyed")
        else:
            _print_banner("SUCCESS", "Agent Destroyed")

        typer.echo(f"AGENT_ID:   {agent_id}")
        typer.echo(f"STATUS:     {result_status}")


@app.command()
def interrupt(
    target: str = typer.Argument(..., help="Agent ID or Job ID to interrupt."),
    server_url: str = typer.Option(None, help="CP URL."),
    purge: bool = typer.Option(False, "--purge", help="Also cancel all queued jobs for the agent."),
) -> None:
    """Halt active execution on an agent or cancel a specific job.

    TARGET can be an Agent ID (interrupts its active job) or a Job ID
    (cancels that specific job).  Use --purge with an agent target to
    also empty the agent's pending queue.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Detect target type: try agent first, fall back to job
        is_agent = True
        try:
            client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                is_agent = False
            else:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if is_agent:
            _interrupt_agent(client, target, purge=purge)
        else:
            if purge:
                typer.echo("Warning: --purge is ignored when targeting a job.", err=True)
            _interrupt_job(client, target)


def _interrupt_agent(client, agent_id: str, *, purge: bool) -> None:
    import httpx as _httpx

    typer.echo(f"[..] Locating agent {agent_id}...")

    try:
        result = client.agent_interrupt(agent_id, purge=purge)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)
        if exc.response.status_code == 404:
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: Agent not found: {agent_id}")
            raise typer.Exit(1)
        typer.echo(_format_http_error(exc), err=True)
        raise typer.Exit(1)

    halted = result.get("halted_job_id")
    dropped = result.get("dropped_job_ids", [])
    remaining = result.get("remaining_queue_size", 0)
    new_status = result.get("status", "idle").upper()

    if halted:
        typer.echo(f"[..] Requesting interrupt for active execution ({halted})...")

    if purge and dropped:
        typer.echo(f"[..] Purging {len(dropped)} pending jobs from the queue...")

    if purge and halted:
        _print_banner("SUCCESS", "Interrupt Requested and Queue Purged")
    elif purge:
        _print_banner("SUCCESS", "Agent Purged and Reset")
    else:
        _print_banner("SUCCESS", "Execution Interrupted")

    typer.echo(f"AGENT:        {agent_id}")
    if halted:
        typer.echo(f"HALTED JOB:   {halted} (interrupt requested)")
    else:
        typer.echo("HALTED JOB:   (none — no active execution)")

    if purge and dropped:
        typer.echo(f"DROPPED JOBS: {', '.join(dropped)}")

    typer.echo(f"NEW STATUS:   {new_status} ({remaining} jobs in queue)")
    if remaining > 0 and not purge:
        typer.echo("")
        typer.echo("Next queued job will be claimed on the runtime's next poll cycle.")
    elif purge and not halted:
        typer.echo("")
        typer.echo("Agent is completely reset and ready for immediate, fresh tasking.")
    elif purge and halted:
        typer.echo("")
        typer.echo("Queued backlog was purged. The active run will stop once the runtime processes the interrupt.")


def _interrupt_job(client, job_id: str) -> None:
    import httpx as _httpx

    typer.echo(f"[..] Locating job {job_id}...")

    try:
        result = client.interrupt(job_id)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: Not found: {job_id}")
            typer.echo("Neither an agent nor a job was found with this ID.")
            raise typer.Exit(1)
        if exc.response.status_code == 409:
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)
        typer.echo(_format_http_error(exc), err=True)
        raise typer.Exit(1)

    job_status = result.get("status", "cancelled")

    if job_status == "cancelled":
        _print_banner("SUCCESS", "Job Removed from Queue")
        typer.echo(f"JOB_ID:       {job_id}")
        typer.echo("STATUS:       CANCELLED")
        typer.echo("")
        typer.echo(
            "Notice: This job was in the queue and had not yet started execution."
            " The active job was not affected."
        )
    else:
        _print_banner("SUCCESS", "Job Interrupt Requested")
        typer.echo(f"JOB_ID:       {job_id}")
        typer.echo(f"STATUS:       {job_status.upper()}")
        typer.echo("")
        typer.echo(
            "Notice: The job is currently running. An interrupt signal has been sent."
            " The runtime will cancel execution at the next checkpoint."
        )


