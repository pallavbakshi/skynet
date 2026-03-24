"""CLI entrypoint for the AGP scaffold.

Primarily exposes the agent-facing client surface (send, wait, status,
ls, info, nudge, etc.) that talks to a running control plane over HTTP.
Operational commands still exist here as hidden compatibility shims so
older scripts keep working, but the intended operator entrypoint is the
``skyops`` CLI.

All server-side imports are deferred to command bodies so that
``pip install agp`` (without ``[server]``) can still import
``agp.client`` without pulling in uvicorn/sqlalchemy/pydantic-settings.
"""

import os

import typer

app = typer.Typer(help="AGP agent CLI.")


def _require_server_extra() -> None:
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
    except ImportError:
        typer.echo(
            "This command requires server dependencies.\n"
            "Install with: pip install 'agp[server]'",
            err=True,
        )
        raise typer.Exit(1)


def _connectable_host(host: str) -> str:
    """Replace 0.0.0.0 with 127.0.0.1 for client connections."""
    return "127.0.0.1" if host == "0.0.0.0" else host


def _default_server_url() -> str:
    """Derive server URL from AGP_HOST/AGP_PORT env or settings."""
    host = os.environ.get("AGP_HOST") or "127.0.0.1"
    port = os.environ.get("AGP_PORT") or "7860"
    return f"http://{_connectable_host(host)}:{port}"


@app.command(hidden=True)
def initdb() -> None:
    """Initialize or migrate the database schema."""
    _require_server_extra()

    from agp.db import init_db

    init_db()
    typer.echo("Initialized database schema.")


@app.command(name="db-status", hidden=True)
def db_status() -> None:
    """Show current schema version and pending migrations."""
    _require_server_extra()

    from agp.migrations import schema_status

    info = schema_status()
    typer.echo(f"Schema version:  {info['current_version']}")
    typer.echo(f"Engine:          {info['engine']}")
    typer.echo(f"Release version: {info['release_version']}")
    if info["pending_migrations"]:
        typer.echo(f"Pending:         {', '.join(info['pending_migrations'])}")
    else:
        typer.echo("Pending:         (none)")


@app.command(name="db-migrate", hidden=True)
def db_migrate() -> None:
    """Apply pending schema migrations."""
    _require_server_extra()

    from agp.migrations import apply_migrations

    result = apply_migrations()
    if result["applied"]:
        for tag in result["applied"]:
            typer.echo(f"  Applied: {tag}")
    else:
        typer.echo("No pending migrations.")
    typer.echo(f"Current version: {result['current_version']}")


@app.command(hidden=True)
def serve(
    host: str = typer.Option(None, help="Bind host (default: AGP_HOST or 127.0.0.1)."),
    port: int = typer.Option(None, help="Bind port (default: AGP_PORT or 7860)."),
) -> None:
    """Run the AGP control plane API server."""
    _require_server_extra()

    import uvicorn
    from agp.config import settings
    from agp.control_plane import build_app

    actual_host = host if host is not None else settings.host
    actual_port = port if port is not None else settings.port
    uvicorn.run(build_app(), host=actual_host, port=actual_port)


@app.command(hidden=True)
def runtime_work_loop(
    runtime_id: str,
    server_url: str = typer.Option(None, help="CP base URL (default: AGP_HOST:AGP_PORT)."),
    hostname: str | None = None,
    agent_id: str | None = None,
    capability_id: str | None = None,
    artifact_root: str = ".agp-artifacts",
    idle_sleep_seconds: float = 0.25,
    max_iterations: int | None = None,
    max_local_recoveries: int = 1,
    host_kind: str = typer.Option(None, help="Terminal host kind (default: AGP_RUNTIME_TERMINAL_HOST_KIND or inprocess)."),
    adapter_kind: str = typer.Option(None, help="Agent adapter kind (default: AGP_RUNTIME_AGENT_ADAPTER_KIND or default)."),
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""
    _require_server_extra()

    import socket as _socket
    from threading import Event

    from agp.config import settings
    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.plugins import build_terminal_host, build_agent_adapter
    from agp.runtime import RuntimeSupervisor

    actual_server_url = server_url if server_url is not None else _default_server_url()
    actual_host_kind = host_kind if host_kind is not None else settings.runtime_terminal_host_kind
    actual_adapter_kind = adapter_kind if adapter_kind is not None else settings.runtime_agent_adapter_kind

    actual_hostname = hostname or _socket.gethostname()
    runtime_token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN") or None
    client = RuntimeClient(
        RuntimeIdentity(
            runtime_id=runtime_id,
            hostname=actual_hostname,
            server_url=actual_server_url,
            token=runtime_token,
        )
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(actual_host_kind, workspace=settings.wezterm_workspace),
        adapter=build_agent_adapter(actual_adapter_kind),
        artifact_root=artifact_root,
    )
    stop_event = Event()
    try:
        payload = worker.run_forever(
            agent_id=agent_id,
            capability_id=capability_id,
            idle_sleep_seconds=idle_sleep_seconds,
            max_iterations=max_iterations,
            stop_event=stop_event,
            max_local_recoveries=max_local_recoveries,
        )
    finally:
        stop_event.set()
        client.close()
    typer.echo(payload)


@app.command(hidden=True)
def sweep_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
) -> None:
    """Continuously expire stale leases on a fixed interval."""
    _require_server_extra()

    from agp.control_plane import sweep_expired_leases
    from agp.db import SessionLocal
    from agp.sweeper import LeaseSweeperService

    service = LeaseSweeperService(
        session_factory=SessionLocal,
        sweep_fn=sweep_expired_leases,
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


@app.command(hidden=True)
def sweep_runtimes_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
    stale_timeout_seconds: int = typer.Option(None, help="Override AGP_RUNTIME_STALE_TIMEOUT_SECONDS."),
) -> None:
    """Continuously mark stale runtimes offline and detach or degrade bound agents."""
    _require_server_extra()

    from agp.config import settings
    from agp.control_plane import sweep_stale_runtimes
    from agp.db import SessionLocal
    from agp.sweeper import SweeperService

    actual_timeout = stale_timeout_seconds if stale_timeout_seconds is not None else settings.runtime_stale_timeout_seconds

    service = SweeperService(
        session_factory=SessionLocal,
        sweep_fn=lambda session: sweep_stale_runtimes(
            session,
            stale_timeout_seconds=actual_timeout,
        ),
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


# ── SDK client commands (no server deps needed) ─────────────────────

_SEPARATOR = "========================================="


def _make_client(server_url: str | None = None):
    """Build an AgpClient that honours profile/env auth.

    If *server_url* is explicitly passed (e.g. via ``--server-url``), it
    overrides the URL from the profile/env, but the token is still loaded
    from the profile resolution chain (env → file → fallback).
    """
    from agp.client import AgpClient, AgpProfile

    profile = AgpProfile.load()
    if server_url:
        profile.server_url = server_url
    return AgpClient(profile=profile)


def _print_banner(label: str, subtitle: str) -> None:
    typer.echo(_SEPARATOR)
    typer.echo(f"[{label}] {subtitle}")
    typer.echo(_SEPARATOR)


def _print_job_result(job: dict, client) -> None:
    """Print structured terminal output for a completed/failed job."""
    job_status = job["status"]
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)

    if job_status == "completed":
        _print_banner("COMPLETED", "Task Finished")
    else:
        suffix = " with Errors" if retry_count > 0 else ""
        _print_banner("COMPLETED", f"Task Failed{suffix}")

    typer.echo(f"JOB_ID:       {job['job_id']}")
    typer.echo(f"AGENT:        {job.get('target_agent_id', 'unknown')}")
    typer.echo(f"STATUS:       {'SUCCESS' if job_status == 'completed' else 'FAILED'}")
    if retry_count > 0:
        typer.echo(f"RETRIES:      {retry_count}/{max_retries}")

    # Print result artifact content
    if job.get("result_artifact_id"):
        try:
            art = client.fetch_artifact(job["result_artifact_id"], content=True)
            typer.echo(f"RESULT:       artifact {job['result_artifact_id']}")
            typer.echo("---")
            typer.echo(art.get("content", "(no content)"))
        except Exception:
            typer.echo(f"RESULT:       artifact {job['result_artifact_id']} (fetch failed)")
    elif job_status == "failed":
        # Try failure_evidence artifact
        try:
            artifacts = client.list_job_artifacts(job["job_id"], role="failure_evidence")
            items = artifacts.get("items", [])
            if items:
                art = client.fetch_artifact(items[0]["artifact_id"], content=True)
                typer.echo("---")
                typer.echo(art.get("content", "(no content)"))
            else:
                typer.echo("(no artifact)")
        except Exception:
            typer.echo("(no artifact)")

    if job_status == "failed" and retry_count > 0:
        typer.echo("")
        typer.echo(
            f"Notice: System exhausted best-effort retries ({retry_count} attempts). "
            "Review the error log and pivot your strategy."
        )


def _print_detached(job_id: str, agent_id: str) -> None:
    _print_banner("ACCEPTED", "Task Detached (Running Long)")
    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {agent_id}")
    typer.echo(f"STATUS:       IN_PROGRESS")
    typer.echo("")
    typer.echo("Notice: The CLI has detached to free your terminal.")
    typer.echo(f"- To check status manually:  agp status {job_id}")
    typer.echo(f"- To wait synchronously:     agp wait {job_id}")


def _poll_until_done(client, job_id: str, timeout: float, heartbeat_interval: float = 10.0):
    """Poll job until terminal or timeout.  Returns (job_dict, timed_out)."""
    import time

    start = time.monotonic()
    deadline = start + timeout
    last_heartbeat = start

    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return job, False

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start)
            typer.echo(f"[..] Agent working... ({elapsed}s elapsed)")
            last_heartbeat = now

        time.sleep(2)

    return client.get_job(job_id), True  # last check before giving up


# ── 0a. up ──────────────────────────────────────────────────────────


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
                return {"status": "error", "detail": str(exc)}
            # 5xx or other — keep polling
        except _httpx.TransportError:
            # Network-level failures (timeout, DNS, connection reset) — keep polling
            pass
        else:
            status = agent.get("status")
            if status == "idle":
                return agent
            # Terminal statuses will never become idle — exit early
            if status in ("terminated", "error", "failed"):
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
    runtime_id: str = typer.Option(None, "--runtime-id", help="Pin to a specific runtime."),
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
        # Resolve capability name → ID
        typer.echo(f"[..] Provisioning capability '{capability_name}'...")
        try:
            cap = client.resolve_capability_by_name(capability_name)
        except ValueError as exc:
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Could not reach control plane: {exc}")
            raise typer.Exit(1)
        if cap is None:
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Unknown capability '{capability_name}'.")
            typer.echo("ACTION: Run `agp ls` to see available capabilities.")
            raise typer.Exit(1)

        capability_id = cap["capability_id"]

        # Retry loop for provisioning
        data: dict | None = None
        for attempt in range(1, max_retries + 1):
            typer.echo(f"[..] Registering agent... (Attempt {attempt}/{max_retries})")
            try:
                data = client.register_agent(
                    agent_id=agent_id,
                    capability_id=capability_id,
                    assigned_runtime_id=runtime_id,
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
        typer.echo("-----------------------------------------")
        typer.echo("Ready. You may now route tasks using:")
        typer.echo(f"  agp send {resolved_agent_id} \"your prompt here\"")


# ── 0b. down ────────────────────────────────────────────────────────


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

    with _make_client(server_url) as client:
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

        # Already terminated — nothing to do
        if agent_status == "terminated":
            _print_banner("ERROR", "Teardown Failed")
            typer.echo(f"Agent {agent_id} is already terminated.")
            raise typer.Exit(1)

        # Statuses that may have active work — require --force
        _HAS_ACTIVE_WORK = ("busy", "draining", "degraded")

        if agent_status in _HAS_ACTIVE_WORK and not force:
            _print_banner("ERROR", "Teardown Blocked")
            typer.echo(f"Agent {agent_id} is {agent_status.upper()} (may have active work).")
            typer.echo("Use --force to cancel active jobs and destroy it:")
            typer.echo(f"  agp down {agent_id} --force")
            raise typer.Exit(1)

        # Determine mode
        if agent_status in _HAS_ACTIVE_WORK:
            typer.echo(f"[..] WARNING: Agent is {agent_status.upper()}.")
            typer.echo("[..] Aborting active jobs and clearing queue...")
            mode = "force"
        elif agent_status == "idle":
            typer.echo("[..] Agent is IDLE. Proceeding with teardown...")
            mode = "terminate"
        else:
            typer.echo(f"[..] Agent is {agent_status.upper()}. Proceeding with teardown...")
            mode = "force" if force else "terminate"

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

        result_status = result.get("status", "terminated").upper()
        if mode == "force":
            _print_banner("SUCCESS", "Agent Forcefully Destroyed")
        else:
            _print_banner("SUCCESS", "Agent Destroyed")

        typer.echo(f"AGENT_ID:   {agent_id}")
        typer.echo(f"STATUS:     {result_status}")


# ── 0c. interrupt ────────────────────────────────────────────────────


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

    with _make_client(server_url) as client:
        # Detect target type: try agent first, fall back to job
        is_agent = True
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                is_agent = False
            else:
                raise

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
        raise

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
        raise

    job_status = result.get("status", "cancelled")

    if job_status == "cancelled":
        _print_banner("SUCCESS", "Job Removed from Queue")
        typer.echo(f"JOB_ID:       {job_id}")
        typer.echo(f"STATUS:       CANCELLED")
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


# ── 1. send ──────────────────────────────────────────────────────────


@app.command()
def send(
    agent_id: str = typer.Argument(..., help="Target agent ID."),
    task: str = typer.Argument(..., help="Task text to send."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    detach: bool = typer.Option(False, "--detach", help="Fire and forget — skip the sync window."),
    timeout: int = typer.Option(90, help="Sync window in seconds before auto-detach (default: 90)."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (for detached tasks)."),
) -> None:
    """Send a task to an agent with smart detach.

    Default: waits up to 90s for completion, then auto-detaches.
    Use --detach for fire-and-forget.  Use --timeout to adjust the sync window.
    Use --nudge <orc_id> to get a push notification when the task finishes.
    """
    import time

    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    with _make_client(server_url) as client:
        typer.echo(f"[..] Dispatching to {agent_id}...")
        result = client.send(
            "agent", agent_id, task,
            metadata=metadata,
            idempotency_key=f"cli-{int(time.time())}",
        )
        job_id = result["job_id"]

        # Fire-and-forget
        if detach:
            _print_detached(job_id, agent_id)
            return

        # Smart detach: sync window with heartbeat
        job, timed_out = _poll_until_done(client, job_id, timeout)

        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        # Auto-detach — job still running
        _print_detached(job_id, agent_id)


# ── 2. wait ──────────────────────────────────────────────────────────


@app.command(name="wait")
def wait_cmd(
    job_id: str = typer.Argument(..., help="Job ID to re-attach to."),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout: int = typer.Option(300, help="Wait timeout in seconds (default: 300)."),
) -> None:
    """Re-attach to a running job and wait for its result."""
    with _make_client(server_url) as client:
        # Quick check — maybe it already finished
        job = client.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        typer.echo(f"[..] Re-attaching to {job_id} (agent={job.get('target_agent_id', '?')})...")
        job, timed_out = _poll_until_done(client, job_id, timeout)

        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        typer.echo("timeout — job still running", err=True)
        typer.echo(f"Check again with: agp status {job_id}")
        raise typer.Exit(1)


# ── 3. status ────────────────────────────────────────────────────────


@app.command()
def status(
    job_id: str = typer.Argument(None, help="Optional job ID for job-specific status."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Check agent-facing job state.

    With a job ID: shows full job details + artifacts.
    With no arguments: performs a lightweight control-plane reachability check.
    """
    if job_id is not None:
        _status_job(job_id, server_url)
    else:
        _status_health(server_url)


def _status_health(server_url: str | None) -> None:
    try:
        with _make_client(server_url) as client:
            data = client.health()
        typer.echo(f"status: {data.get('status', 'ok')}")
        for k, v in data.get("components", {}).items():
            typer.echo(f"  {k}: {v}")
    except Exception as e:
        typer.echo(f"unreachable: {e}", err=True)
        raise typer.Exit(1)


def _status_job(job_id: str, server_url: str | None) -> None:
    with _make_client(server_url) as client:
        job = client.get_job(job_id)
        retry_count = job.get("retry_count", 0)
        max_retries = job.get("max_retries", 3)

        typer.echo(f"JOB_ID:       {job['job_id']}")
        typer.echo(f"AGENT:        {job.get('target_agent_id', 'unknown')}")
        typer.echo(f"STATUS:       {job['status'].upper()}")
        if retry_count > 0:
            typer.echo(f"RETRIES:      {retry_count}/{max_retries}")
        if job.get("latest_run_id"):
            typer.echo(f"RUN:          {job['latest_run_id']}")
        typer.echo(f"CREATED:      {job.get('created_at', '?')}")
        typer.echo(f"UPDATED:      {job.get('updated_at', '?')}")

        # Show result/failure artifact if terminal
        if job["status"] in ("completed", "failed") and job.get("result_artifact_id"):
            try:
                art = client.fetch_artifact(job["result_artifact_id"], content=True)
                typer.echo("---")
                typer.echo(art.get("content", "(no content)"))
            except Exception:
                pass
        elif job["status"] == "failed":
            try:
                artifacts = client.list_job_artifacts(job_id, role="failure_evidence")
                items = artifacts.get("items", [])
                if items:
                    art = client.fetch_artifact(items[0]["artifact_id"], content=True)
                    typer.echo("---")
                    typer.echo(art.get("content", "(no content)"))
            except Exception:
                pass


# ── 4. jobs ──────────────────────────────────────────────────────────


@app.command()
def jobs(
    server_url: str = typer.Option(None, help="CP URL."),
    limit: int = typer.Option(10, help="Max jobs to show."),
    agent: str = typer.Option(None, "--agent", help="Filter by agent ID."),
    filter_status: str = typer.Option(None, "--status", help="Filter by status (queued, running, completed, failed)."),
) -> None:
    """List recent jobs."""
    with _make_client(server_url) as client:
        data = client.list_jobs(
            limit=limit,
            target_agent_id=agent,
            status=filter_status,
        )
        items = data.get("items", [])
        if not items:
            typer.echo("(no jobs)")
            return
        for j in items:
            retry = f" retry={j['retry_count']}/{j['max_retries']}" if j.get("retry_count", 0) > 0 else ""
            typer.echo(
                f"  {j['job_id']}  {j['status']:10s}  agent={j.get('target_agent_id', '?')}{retry}"
            )


# ── 5. ls ────────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Format seconds into Xm:XXs or Xh:XXm."""
    if seconds < 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h:{minutes:02d}m"
    return f"{minutes:02d}m:{secs:02d}s"


@app.command()
def ls(
    server_url: str = typer.Option(None, help="CP URL."),
    all_agents: bool = typer.Option(False, "--all", help="Include terminated/offline agents."),
) -> None:
    """List logical agents and available capabilities."""
    from datetime import datetime, timezone

    with _make_client(server_url) as client:
        agents_data = client.list_agents()
        agents = agents_data.get("items", [])

        caps_data = client.list_capabilities()
        caps = caps_data.get("items", [])

        # Build capability name lookup
        cap_names: dict[str, str] = {}
        for c in caps:
            cap_names[c["capability_id"]] = c.get("name", c["capability_id"])

        # For busy agents, fetch their running job
        agent_jobs: dict[str, dict] = {}
        busy_agents = [a for a in agents if a.get("status") == "busy"]
        if busy_agents:
            running_jobs = client.list_jobs(status="running", limit=100)
            for j in running_jobs.get("items", []):
                tid = j.get("target_agent_id")
                if tid:
                    agent_jobs[tid] = j

        # ── Header
        typer.echo(_SEPARATOR)
        typer.echo("      AGP SERVICE DISCOVERY (agp ls)")
        typer.echo(_SEPARATOR)
        typer.echo("Logical agent view only. Use `skyops runtime ...` for runtime and machine health.")
        typer.echo("")

        # ── Active Agents section
        active = [a for a in agents if all_agents or a.get("status") not in ("terminated",)]

        typer.echo("[ACTIVE AGENTS]")
        if not active:
            typer.echo("(none)")
        else:
            # Column headers
            typer.echo(
                f"{'ID':<20s} {'ROLE':<18s} {'STATUS':<8s} {'RUNTIME':<16s} "
                f"{'JOB_ID':<14s} {'TIME_ON_JOB':<12s} {'WORKSPACE'}"
            )
            typer.echo("-" * 120)

            now = datetime.now(timezone.utc)
            for a in active:
                agent_id = a["agent_id"]
                role = cap_names.get(a.get("capability_id", ""), a.get("capability_id", "-"))
                agent_status = a.get("status", "?").upper()
                runtime_id = a.get("assigned_runtime_id") or "-"
                workspace = a.get("workspace_ref") or "-"

                job = agent_jobs.get(agent_id)
                if job:
                    job_id = job["job_id"]
                    # Compute time on job
                    try:
                        created = datetime.fromisoformat(job["created_at"])
                        elapsed = (now - created).total_seconds()
                        time_on_job = _format_duration(elapsed)
                    except Exception:
                        time_on_job = "-"
                else:
                    job_id = "-"
                    time_on_job = "-"

                typer.echo(
                    f"{agent_id:<20s} {role:<18s} {agent_status:<8s} {runtime_id:<16s} "
                    f"{job_id:<14s} {time_on_job:<12s} {workspace}"
                )

        typer.echo("")

        # ── Available Capabilities section
        typer.echo("[AVAILABLE CAPABILITIES (On-Demand)]")
        if not caps:
            typer.echo("(none)")
        else:
            typer.echo(
                f"{'CAPABILITY':<20s} {'MODEL':<20s} {'TIER':<10s} {'VERSION'}"
            )
            typer.echo("-" * 70)

            for c in caps:
                cap_name = c.get("name", c["capability_id"])
                model = c.get("model_ref", "-") or "-"
                tier = c.get("resource_tier", "-") or "-"
                version = c.get("version", "-") or "-"
                typer.echo(
                    f"{cap_name:<20s} {model:<20s} {tier:<10s} {version}"
                )


# ── 6. info ──────────────────────────────────────────────────────────


def _print_capability_blueprint(cap: dict, *, indent: str = "") -> None:
    """Print capability blueprint fields."""
    typer.echo(f"{indent}MODEL:        {cap.get('model_ref') or '-'}")
    tier = cap.get("resource_tier") or "-"
    typer.echo(f"{indent}TIER:         {tier}")
    typer.echo(f"{indent}PERMISSION:   {cap.get('permission_profile') or 'default'}")

    reqs = cap.get("runtime_requirements_json") or {}

    network = reqs.get("network")
    filesystem = reqs.get("filesystem")
    if network or filesystem:
        typer.echo(f"{indent}ACCESS:")
        if network:
            typer.echo(f"{indent}  Network:    {network}")
        if filesystem:
            typer.echo(f"{indent}  Filesystem: {filesystem}")

    tools = reqs.get("tools")
    if tools and isinstance(tools, list):
        typer.echo(f"{indent}PRE-INSTALLED TOOLS:")
        for t in tools:
            typer.echo(f"{indent}  - {t}")

    restrictions = reqs.get("restrictions")
    if restrictions and isinstance(restrictions, list):
        typer.echo(f"{indent}RESTRICTIONS:")
        for r in restrictions:
            typer.echo(f"{indent}  - {r}")


@app.command()
def info(
    target: str = typer.Argument(..., help="Agent ID or capability name/ID."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Deep-dive context for an agent or capability.

    Accepts an agent ID (e.g. agt_local) or capability ID/name (e.g. cap_python).
    """
    from datetime import datetime, timezone

    with _make_client(server_url) as client:
        # Try agent first, fall back to capability
        agent = None
        try:
            agent = client.get_agent(target)
        except Exception:
            pass

        if agent is not None:
            _info_agent(agent, client)
        else:
            _info_capability(target, client)


def _info_agent(agent: dict, client) -> None:
    from datetime import datetime, timezone

    agent_id = agent["agent_id"]
    cap_id = agent.get("capability_id", "")

    typer.echo(_SEPARATOR)
    typer.echo(f"      AGENT INFO: {agent_id}")
    typer.echo(_SEPARATOR)
    typer.echo("Logical agent view. Use `skyops runtime ...` to inspect the bound runtime directly.")

    # Resolve capability name
    cap = None
    cap_name = cap_id
    try:
        cap = client.get_capability(cap_id)
        cap_name = cap.get("name", cap_id)
    except Exception:
        pass

    typer.echo(f"CAPABILITY:   {cap_name}")
    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"RUNTIME:      {agent.get('assigned_runtime_id') or '-'}")

    # Current job for busy agents
    now = datetime.now(timezone.utc)
    if agent.get("status") == "busy":
        try:
            running = client.list_jobs(status="running", target_agent_id=agent_id, limit=1)
            items = running.get("items", [])
            if items:
                job = items[0]
                try:
                    created = datetime.fromisoformat(job["created_at"])
                    elapsed = (now - created).total_seconds()
                    duration = _format_duration(elapsed)
                except Exception:
                    duration = "?"
                typer.echo(f"CURRENT_JOB:  {job['job_id']} (Running for {duration})")
        except Exception:
            pass

    # Uptime
    created_at = agent.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            uptime = (now - created).total_seconds()
            typer.echo(f"UPTIME:       {_format_duration(uptime)}")
        except Exception:
            pass

    workspace = agent.get("workspace_ref") or "-"
    typer.echo(f"WORKSPACE:    {workspace}")

    # Capability blueprint summary
    if cap:
        typer.echo("")
        typer.echo("[CAPABILITY BLUEPRINT]")
        _print_capability_blueprint(cap)


def _info_capability(target: str, client) -> None:
    # Try by ID first, then search by name
    cap = None
    try:
        cap = client.get_capability(target)
    except Exception:
        pass

    if cap is None:
        try:
            results = client.list_capabilities(name=target)
            items = results.get("items", [])
            if items:
                cap = items[0]
        except Exception:
            pass

    if cap is None:
        typer.echo(f"Not found: {target} (not an agent ID or capability name)", err=True)
        raise typer.Exit(1)

    cap_name = cap.get("name", cap.get("capability_id", target))
    typer.echo(_SEPARATOR)
    typer.echo(f"      CAPABILITY INFO: {cap_name}")
    typer.echo(_SEPARATOR)
    _print_capability_blueprint(cap)


# ── 7. nudge ─────────────────────────────────────────────────────────


def _format_human_nudge(message: str) -> str:
    return (
        f"{_SEPARATOR}\n"
        f"[SYSTEM NUDGE] Human Co-Pilot Override\n"
        f"{_SEPARATOR}\n"
        f"SOURCE:       User / Lead Developer\n"
        f"PRIORITY:     CRITICAL OVERRIDE\n"
        f"\n"
        f'HUMAN MESSAGE: "{message}"\n'
        f"\n"
        f"ACTION REQUIRED: Acknowledge this pivot immediately. "
        f"Pause your current goals, use `agp ls` to find an available worker, "
        f"and execute the human's exact request."
    )


@app.command()
def nudge(
    target: str = typer.Argument(..., help="Target orchestrator agent ID."),
    message: str = typer.Argument(..., help="Message to inject."),
    server_url: str = typer.Option(None, help="CP URL."),
    priority: int = typer.Option(1, help="Priority (1=human, 2=job, 3=agenda, 4=system)."),
    source: str = typer.Option("human", help="Nudge source label."),
) -> None:
    """Send a nudge to an orchestrator's terminal.

    The nudge is queued and delivered by the nudge-loop daemon when
    the orchestrator's shell is idle.
    """
    if source == "human" and priority == 1:
        payload = _format_human_nudge(message)
    else:
        payload = (
            f"{_SEPARATOR}\n"
            f"[SYSTEM NUDGE] {source.replace('_', ' ').title()}\n"
            f"{_SEPARATOR}\n"
            f"{message}"
        )

    with _make_client(server_url) as client:
        result = client.create_nudge(target, payload, priority=priority, source=source)
        typer.echo(f"nudge queued: {result['nudge_id']} (priority={priority}, target={target})")


# ── 8. nudge-loop ────────────────────────────────────────────────────


@app.command(hidden=True)
def nudge_loop(
    target: str = typer.Argument(..., help="Orchestrator agent ID to deliver nudges to."),
    session: str = typer.Option(None, help="Tmux session name (default: agp-<target>)."),
    server_url: str = typer.Option(None, help="CP URL."),
    poll_seconds: float = typer.Option(2.0, help="Poll interval for new nudges."),
    idle_polls: int = typer.Option(3, help="Consecutive stable polls before injecting."),
    max_iterations: int | None = typer.Option(None, help="Stop after N deliveries (for testing)."),
) -> None:
    """Daemon: deliver queued nudges into an orchestrator's tmux session.

    Monitors the nudge queue and the tmux session.  Only injects when
    the session output has stabilised (shell is idle).
    """
    _require_server_extra()

    import subprocess
    import time

    session_name = session or f"agp-{target}"
    delivered = 0

    typer.echo(f"nudge-loop: target={target}  session={session_name}  poll={poll_seconds}s")

    with _make_client(server_url) as client:
        while True:
            # Check for pending nudges
            nudge = client.next_nudge(target)
            if nudge is None:
                time.sleep(poll_seconds)
                continue

            # Wait for tmux session to be idle
            typer.echo(f"[nudge] pending: {nudge['nudge_id']} (priority={nudge['priority']}, source={nudge['source']})")
            idle = _wait_for_tmux_idle(session_name, poll_seconds=poll_seconds, idle_after=idle_polls)
            if not idle:
                typer.echo(f"[nudge] session {session_name} not idle, delivering anyway")

            # Inject into tmux
            payload = nudge["payload"]
            try:
                # Use tmux load-buffer + paste-buffer for clean multi-line injection
                subprocess.run(
                    ["tmux", "set-buffer", "-b", "agp-nudge", payload],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "paste-buffer", "-b", "agp-nudge", "-t", session_name],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "", "Enter"],
                    check=True, capture_output=True,
                )
                typer.echo(f"[nudge] delivered: {nudge['nudge_id']}")
            except subprocess.CalledProcessError as e:
                typer.echo(f"[nudge] delivery failed: {e}", err=True)

            delivered += 1
            if max_iterations is not None and delivered >= max_iterations:
                typer.echo(f"[nudge] reached max_iterations={max_iterations}, stopping")
                return

            time.sleep(poll_seconds)


def _wait_for_tmux_idle(
    session_name: str,
    *,
    poll_seconds: float = 2.0,
    idle_after: int = 3,
    timeout_seconds: float = 30.0,
) -> bool:
    """Wait until tmux session output stabilises.  Returns True if idle detected."""
    import subprocess
    import time

    last_output = ""
    stable_count = 0
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, text=True, timeout=5,
            )
            current = result.stdout.rstrip()
        except Exception:
            time.sleep(poll_seconds)
            continue

        if current == last_output:
            stable_count += 1
            if stable_count >= idle_after:
                return True
        else:
            stable_count = 0
            last_output = current

        time.sleep(poll_seconds)

    return False


if __name__ == "__main__":
    app()
