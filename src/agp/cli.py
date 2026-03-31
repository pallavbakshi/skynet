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

import json
import os
import sys
import time
import uuid
from pathlib import Path

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


def _format_http_error(exc) -> str:
    """Extract a clean error message from an httpx.HTTPStatusError.

    The CP returns ``{"ok": false, "error": {"code": ..., "message": ...}}``.
    """
    try:
        body = exc.response.json()
        err = body.get("error", {})
        message = err.get("message") or err.get("code") or str(body)
    except Exception:
        message = exc.response.text or str(exc)
    return f"[HTTP {exc.response.status_code}] {message}"


def _cli_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _extract_trailing_json_payload(text: str) -> dict | None:
    def _candidate_attempts(raw: str) -> list[str]:
        attempts = [
            raw,
            "".join(line.strip() for line in raw.splitlines()),
            " ".join(line.strip() for line in raw.splitlines()),
        ]
        stripped = raw.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            fence_end = stripped.find("\n")
            if fence_end != -1:
                fenced_body = stripped[fence_end + 1 : -3].strip()
                attempts.extend(
                    [
                        fenced_body,
                        "".join(line.strip() for line in fenced_body.splitlines()),
                        " ".join(line.strip() for line in fenced_body.splitlines()),
                    ]
                )
        return attempts

    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    for idx in range(len(stripped) - 1, -1, -1):
        if stripped[idx] not in "[{":
            continue
        suffix = stripped[idx:]
        fence_start = stripped.rfind("```", 0, idx)
        if fence_start != -1 and stripped.find("\n", fence_start, idx) != -1:
            suffix = stripped[fence_start:]
        for attempt in _candidate_attempts(suffix):
            try:
                payload, end = decoder.raw_decode(attempt)
            except json.JSONDecodeError:
                continue
            if attempt[end:].strip():
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _review_attachment_note(*, attachment_name: str, short_output_guidance: str) -> str:
    return (
        f"Source job result is attached as {attachment_name}. "
        f"AGP should also materialize that attachment under agp-attachments/ in the workspace before execution; "
        f"search by the attached filename if needed. "
        f"{short_output_guidance}"
    )


def _review_fix_attachment_note(*, attachment_name: str, short_output_guidance: str) -> str:
    return (
        f"Updated result is attached as {attachment_name}. "
        f"AGP should also materialize that attachment under agp-attachments/ in the workspace before execution; "
        f"search by the attached filename if needed. "
        f"{short_output_guidance}"
    )


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
    from agp.migrations import require_initialized_schema

    actual_host = host if host is not None else settings.host
    actual_port = port if port is not None else settings.port
    try:
        require_initialized_schema()
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    os.environ["AGP_ENFORCE_SQLITE_RUNTIME_GUARD"] = "1"
    uvicorn.run(build_app(), host=actual_host, port=actual_port)


@app.command(hidden=True)
def runtime_work_loop(
    runtime_id: str,
    server_url: str = typer.Option(None, help="CP base URL (default: AGP_HOST:AGP_PORT)."),
    hostname: str | None = None,
    agent_id: str | None = None,
    capability_id: str | None = None,
    capabilities: str | None = typer.Option(None, help="Comma-separated capability list (e.g. 'code,python')."),
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
    resolved_capabilities = [c.strip() for c in capabilities.split(",") if c.strip()] if capabilities else None
    payload: list[dict] = []
    restart_attempt = 0

    while True:
        stop_event = Event()
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
        try:
            batch = worker.run_forever(
                agent_id=agent_id,
                capability_id=capability_id,
                capabilities=resolved_capabilities,
                idle_sleep_seconds=idle_sleep_seconds,
                max_iterations=max_iterations,
                stop_event=stop_event,
                max_local_recoveries=max_local_recoveries,
            )
            payload.extend(batch)
            break
        except Exception as exc:  # noqa: BLE001
            restart_attempt += 1
            backoff_seconds = min(30.0, max(idle_sleep_seconds, 0.25) * (2 ** (restart_attempt - 1)))
            typer.echo(
                f"[runtime] fatal worker error: {type(exc).__name__}: {exc}; reinitializing in {backoff_seconds:.1f}s",
                err=True,
            )
            time.sleep(backoff_seconds)
        finally:
            stop_event.set()
            client.close()
    typer.echo(payload)


def _runtime_binding_warning(client, agent_id: str) -> str | None:
    runtime_id = f"rtm_{agent_id}"
    try:
        getter = getattr(client, "ops_get_runtime", None) or getattr(client, "get_runtime", None)
        runtime = getter(runtime_id) if getter is not None else None
    except Exception:  # noqa: BLE001
        runtime = None
    if not runtime:
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    if str(runtime.get("hostname") or "").strip().lower() in {"", "unknown"}:
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    agents = runtime.get("agents") or []
    if agents and not any(item.get("agent_id") == agent_id for item in agents):
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    return None


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


def _cli_client(server_url: str | None = None):
    """_make_client wrapper that converts transport errors to friendly messages.

    Use this in user-facing CLI commands so that connection-refused /
    DNS-failure / timeout errors produce a one-line message instead of a
    raw Python traceback.  Commands with their own retry logic (e.g. ``up``)
    should continue using ``_make_client`` directly.
    """
    from contextlib import contextmanager

    import httpx as _httpx

    @contextmanager
    def _ctx():
        try:
            with _make_client(server_url) as client:
                yield client
        except _httpx.TransportError as exc:
            typer.echo(f"connection error: control plane unreachable ({exc})", err=True)
            raise typer.Exit(1)

    return _ctx()


def _parse_attachment_option(value: str) -> tuple[Path, str]:
    path_text, sep, role = value.rpartition(":")
    if not sep or not path_text or not role:
        raise typer.BadParameter("--attach must be <path>:<role>")
    path = Path(path_text)
    if not path.is_file():
        raise typer.BadParameter(f"attachment file not found: {path}")
    return path, role


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


def _peek_tip(agent_id: str) -> str | None:
    """Return a terminal-specific tip for peeking at an agent's live output.

    Detects the local terminal host by probing for tmux sessions or
    wezterm panes.  Returns None when no local session is found (remote
    runtime or non-interactive environment).
    """
    import shutil
    import subprocess

    # Try tmux first
    if shutil.which("tmux"):
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", f"agp-{agent_id}"],
                capture_output=True, timeout=3,
            )
            if result.returncode == 0:
                return (
                    f"Tip: peek at live output with:\n"
                    f"  tmux capture-pane -t agp-{agent_id} -p -S -30"
                )
        except Exception:
            pass

    # Try wezterm
    if shutil.which("wezterm"):
        try:
            result = subprocess.run(
                ["wezterm", "cli", "list", "--format", "json"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                import json as _json
                for pane in _json.loads(result.stdout):
                    title = pane.get("title", "")
                    if f"AGP:{agent_id}" in title:
                        pane_id = pane.get("pane_id")
                        return (
                            f"Tip: peek at live output with:\n"
                            f"  wezterm cli get-text --pane-id {pane_id}"
                        )
        except Exception:
            pass

    return None


def _print_peek_tip(agent_id: str) -> None:
    """Print a peek tip if one is available."""
    tip = _peek_tip(agent_id)
    if tip:
        typer.echo(tip)


def _print_detached(job_id: str, agent_id: str) -> None:
    _print_banner("ACCEPTED", "Task Detached (Running Long)")
    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {agent_id}")
    typer.echo(f"STATUS:       IN_PROGRESS")
    typer.echo("")
    typer.echo("Notice: The CLI has detached to free your terminal.")
    typer.echo(f"- To check status manually:  agp status {job_id}")
    typer.echo(f"- To wait synchronously:     agp wait {job_id}")
    _print_peek_tip(agent_id)


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

        # Determine mode
        if agent_status in _HAS_ACTIVE_WORK:
            typer.echo(f"[..] WARNING: Agent is {agent_status.upper()}.")
            typer.echo("[..] Aborting active jobs and clearing queue...")
            mode = "force"
        elif agent_status == "idle":
            typer.echo("[..] Agent is IDLE. Proceeding with teardown...")
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

    with _cli_client(server_url) as client:
        # Detect target type: try agent first, fall back to job
        is_agent = True
        try:
            agent = client.get_agent(target)
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
    task: str | None = typer.Argument(None, help="Task text to send (reads from stdin when omitted)."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    detach: bool = typer.Option(False, "--detach", help="Fire and forget — skip the sync window."),
    timeout: int = typer.Option(90, help="Sync window in seconds before auto-detach (default: 90)."),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds", help="Per-job execution timeout hint in seconds."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (for detached tasks)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
    reply_to: str | None = typer.Option(None, "--reply-to", help="Parent message ID for a multi-turn reply."),
    attach: list[str] = typer.Option(None, "--attach", help="Attach a text file as <path>:<role>. Repeatable."),
) -> None:
    """Send a task to an agent with smart detach.

    Default: waits up to 90s for completion, then auto-detaches.
    Use --detach for fire-and-forget.  Use --timeout to adjust the sync window.
    Use --nudge <orc_id> to get a push notification when the task finishes.
    """
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    conversation_id: str | None = None
    attachments: list[dict[str, str]] = []
    if output_contract is not None:
        try:
            parsed_output_contract = json.loads(output_contract)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"invalid JSON for --output-contract: {exc.msg}") from exc
        if not isinstance(parsed_output_contract, dict):
            raise typer.BadParameter("--output-contract must decode to a JSON object")
    for item in attach or []:
        path, role = _parse_attachment_option(item)
        attachments.append({"name": path.name, "role": role, "content": path.read_text(encoding="utf-8")})
    if task is None:
        task = sys.stdin.read().strip()
    if not task:
        raise typer.BadParameter("task is required (pass as argument or pipe via stdin)")

    import httpx as _httpx

    with _cli_client(server_url) as client:
        typer.echo(f"[..] Dispatching to {agent_id}...")
        try:
            result = client.send(
                "agent", agent_id, task,
                metadata=metadata,
                output_contract=parsed_output_contract,
                conversation_id=conversation_id,
                reply_to_message_id=reply_to,
                timeout_seconds=timeout_seconds,
                attachments=attachments,
                idempotency_key=_cli_idempotency_key("cli"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        job_id = result["job_id"]

        # Fire-and-forget
        if detach:
            _print_detached(job_id, agent_id)
            return

        # Smart detach: sync window with heartbeat
        _print_peek_tip(agent_id)
        try:
            job, timed_out = _poll_until_done(client, job_id, timeout)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        # Auto-detach — job still running
        _print_detached(job_id, agent_id)


@app.command()
def reply(
    job_id: str = typer.Argument(..., help="Source job ID to reply to."),
    task: str = typer.Argument(..., help="Reply text to send."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    detach: bool = typer.Option(False, "--detach", help="Fire and forget — skip the sync window."),
    timeout: int = typer.Option(90, help="Sync window in seconds before auto-detach (default: 90)."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (for detached tasks)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
) -> None:
    """Reply to an existing job, preserving its conversation context."""
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    if output_contract is not None:
        try:
            parsed_output_contract = json.loads(output_contract)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"invalid JSON for --output-contract: {exc.msg}") from exc
        if not isinstance(parsed_output_contract, dict):
            raise typer.BadParameter("--output-contract must decode to a JSON object")

    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            source_job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        message_id = source_job.get("message_id")
        if not message_id:
            typer.echo("source job is missing message_id", err=True)
            raise typer.Exit(1)
        conversation_id = source_job.get("conversation_id") or job_id
        agent_id = source_job.get("target_agent_id")
        if not agent_id:
            typer.echo("source job is missing target_agent_id", err=True)
            raise typer.Exit(1)

        # Fetch parent job's prompt + result artifacts to provide conversation context
        context_task = task
        prompt_text = ""
        result_text = ""
        try:
            latest_run_id = source_job.get("latest_run_id")
            if latest_run_id:
                prompt_arts = client.list_run_artifacts(latest_run_id, role="prompt").get("items", [])
                if prompt_arts:
                    p_art = client.fetch_artifact(prompt_arts[0]["artifact_id"], content=True)
                    prompt_text = p_art.get("content", "")
            result_artifact_id = source_job.get("result_artifact_id")
            if result_artifact_id:
                r_art = client.fetch_artifact(result_artifact_id, content=True)
                result_text = r_art.get("content", "")
        except Exception:
            pass  # proceed without context if artifact fetch fails
        if prompt_text or result_text:
            parts = ["Previous exchange:\n---"]
            if prompt_text:
                parts.append(f"Prompt: {prompt_text}")
            if result_text:
                parts.append(f"Response: {result_text}")
            parts.append(f"---\nFollow-up: {task}")
            context_task = "\n".join(parts)

        typer.echo(f"[..] Replying to {job_id} via {agent_id}...")
        try:
            result = client.send(
                "agent",
                agent_id,
                context_task,
                metadata=metadata,
                output_contract=parsed_output_contract,
                conversation_id=conversation_id,
                reply_to_message_id=message_id,
                idempotency_key=_cli_idempotency_key("cli-reply"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        new_job_id = result["job_id"]

        if detach:
            _print_detached(new_job_id, agent_id)
            return

        try:
            job, timed_out = _poll_until_done(client, new_job_id, timeout)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        _print_detached(new_job_id, agent_id)


# ── 1c. review ──────────────────────────────────────────────────────────


@app.command(name="review")
def review_cmd(
    job_id: str = typer.Argument(..., help="Source job ID whose result should be reviewed."),
    reviewer_id: str = typer.Argument(..., help="Agent ID of the reviewer."),
    max_rounds: int = typer.Option(3, "--max-rounds", help="Maximum review rounds."),
    dev_id: str = typer.Option(None, "--dev", help="Agent ID of the developer (defaults to the source job's agent)."),
    prompt: str = typer.Option(
        "Review the following output for correctness, edge cases, and security. "
        "Respond with a JSON object: {\"verdict\": \"approved\" or \"changes_requested\", \"summary\": \"...\", \"findings\": [{\"severity\": \"high|medium|low\", \"description\": \"...\"}]}. "
        "Also write findings to /tmp/review-findings.md for reference.",
        "--prompt", help="Review prompt template.",
    ),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout_per_round: int = typer.Option(120, "--timeout", help="Seconds to wait per round."),
) -> None:
    """Run an automated review loop: reviewer reviews, dev fixes, reviewer re-reviews.

    Uses conversation threading and output contracts to structure the loop.
    Terminates when the reviewer approves or max_rounds is reached.
    """
    import json
    import time
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            source_job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        short_output_guidance = (
            "The attached result may legitimately be short, single-line, or an exact-output-only reply. "
            "Do not infer staging failure or incompleteness from short length alone; review the content that was actually delivered."
        )
        source_agent = source_job.get("target_agent_id") or source_job.get("target_queue", "")
        dev_agent = dev_id or source_agent
        if not dev_id and dev_agent == reviewer_id:
            typer.echo(
                "[review] Error: source job's agent is the reviewer itself. "
                "Use --dev to specify which agent should apply fixes.",
                err=True,
            )
            raise typer.Exit(1)
        conversation_id = source_job.get("conversation_id")
        review_attempt_id = uuid.uuid4().hex[:12]

        for round_num in range(1, max_rounds + 1):
            typer.echo(f"[review] Round {round_num}/{max_rounds}")

            # Build attachments list for the review send
            review_attachments: list[dict[str, str]] = []

            if round_num == 1:
                # First round: send source job result to reviewer
                result_artifact_id = source_job.get("result_artifact_id")
                review_text = prompt
                if result_artifact_id:
                    try:
                        artifact = client.fetch_artifact(result_artifact_id, content=True)
                        artifact_content = artifact.get("content", "")
                        if artifact_content:
                            attachment_name = f"agp-review-{job_id}-source.txt"
                            review_attachments.append({"name": attachment_name, "role": "source-output", "content": artifact_content})
                            review_text = f"{prompt}\n\n" + _review_attachment_note(
                                attachment_name=attachment_name,
                                short_output_guidance=short_output_guidance,
                            )
                    except Exception:
                        review_text = f"{prompt}\n\n(Could not fetch source job artifact.)"
            else:
                # Subsequent rounds: send dev's fixes to reviewer
                fix_artifact_id = source_job.get("result_artifact_id")
                if fix_artifact_id:
                    try:
                        fix_artifact = client.fetch_artifact(fix_artifact_id, content=True)
                        fix_content = fix_artifact.get("content", "")
                        attachment_note = ""
                        if fix_content:
                            attachment_name = f"agp-review-{job_id}-fix-r{round_num}.txt"
                            review_attachments.append({"name": attachment_name, "role": "fix-output", "content": fix_content})
                            attachment_note = _review_fix_attachment_note(
                                attachment_name=attachment_name,
                                short_output_guidance=short_output_guidance,
                            )
                        review_text = (
                            f"{prompt}\n\n"
                            f"[Round {round_num}] The developer addressed issues from the previous review.\n"
                            f"{attachment_note or 'Updated result is attached and should also be materialized into the workspace.'}"
                        )
                    except Exception:
                        review_text = f"[Round {round_num}] Please re-review the changes. The developer was asked to fix issues from the previous review."
                else:
                    review_text = f"[Round {round_num}] Please re-review the changes. The developer was asked to fix issues from the previous review."

            output_contract = {
                "format": "json",
                "json_schema": {
                    "type": "object",
                    "required": ["verdict", "summary"],
                    "properties": {
                        "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
                        "summary": {"type": "string"},
                        "findings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "description": {"type": "string"},
                                    "file": {"type": "string"},
                                    "line": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            }

            typer.echo(f"[review] Sending to reviewer {reviewer_id}...")
            try:
                review_result = client.send(
                    "agent", reviewer_id, review_text,
                    conversation_id=conversation_id,
                    output_contract=output_contract,
                    attachments=review_attachments,
                    idempotency_key=f"review-{job_id}-r{round_num}-{review_attempt_id}",
                )
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)
            review_job_id = review_result["job_id"]
            review_job, timed_out = _poll_until_done(client, review_job_id, timeout_per_round)

            if timed_out:
                typer.echo(f"[review] Round {round_num} timed out waiting for reviewer.")
                _print_detached(review_job_id, reviewer_id)
                return

            if review_job["status"] == "failed":
                typer.echo(f"[review] Round {round_num} reviewer job failed.")
                _print_job_result(review_job, client)
                raise typer.Exit(1)

            # Parse reviewer output
            review_artifact_id = review_job.get("result_artifact_id")
            if not review_artifact_id:
                typer.echo("[review] No result artifact from reviewer.")
                raise typer.Exit(1)

            review_artifact = client.fetch_artifact(review_artifact_id, content=True)
            content = review_artifact.get("content", "")

            # Try to parse structured output
            verdict = "changes_requested"
            try:
                structured = json.loads(content)
            except json.JSONDecodeError:
                structured = _extract_trailing_json_payload(content)
                if structured is None:
                    summary = content[:500]
                else:
                    verdict = structured.get("verdict", "changes_requested")
                    summary = structured.get("summary", "")
            else:
                verdict = structured.get("verdict", "changes_requested")
                summary = structured.get("summary", "")

            typer.echo(f"[review] Verdict: {verdict}")
            typer.echo(f"[review] Summary: {summary[:200]}")

            if verdict == "approved":
                typer.echo(f"[review] ✅ Approved after {round_num} round(s).")
                return

            if round_num < max_rounds:
                # Send findings to dev for fixing
                typer.echo(f"[review] Sending findings to dev {dev_agent}...")
                fix_text = (
                    f"The reviewer found issues that need fixing (round {round_num}):\n\n"
                    f"{content}\n\n"
                    f"Please address all findings and ensure the code is correct."
                )
                try:
                    fix_result = client.send(
                        "agent", dev_agent, fix_text,
                        conversation_id=conversation_id,
                        idempotency_key=f"fix-{job_id}-r{round_num}-{review_attempt_id}",
                    )
                except _httpx.HTTPStatusError as exc:
                    typer.echo(_format_http_error(exc), err=True)
                    raise typer.Exit(1)
                fix_job_id = fix_result["job_id"]
                fix_job, fix_timed_out = _poll_until_done(client, fix_job_id, timeout_per_round)
                if fix_timed_out:
                    typer.echo("[review] Dev fix timed out.")
                    _print_detached(fix_job_id, dev_agent)
                    return
                if fix_job["status"] == "failed":
                    typer.echo("[review] Dev fix job failed.")
                    _print_job_result(fix_job, client)
                    raise typer.Exit(1)

                # Update source_job reference for next round's context
                source_job = fix_job
                job_id = fix_job_id

        typer.echo(f"[review] Max rounds ({max_rounds}) reached without approval.")


# ── 2. wait ──────────────────────────────────────────────────────────


@app.command(name="wait")
def wait_cmd(
    job_id: str = typer.Argument(..., help="Job ID to re-attach to."),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout: int = typer.Option(300, help="Wait timeout in seconds (default: 300)."),
) -> None:
    """Re-attach to a running job and wait for its result."""
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Quick check — maybe it already finished
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        if job["status"] in ("completed", "failed", "cancelled"):
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        agent_id = job.get("target_agent_id", "?")
        typer.echo(f"[..] Re-attaching to {job_id} (agent={agent_id})...")
        _print_peek_tip(agent_id)
        try:
            job, timed_out = _poll_until_done(client, job_id, timeout)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

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
    import httpx as _httpx

    def _list_all_agents(client) -> list[dict]:
        agents: list[dict] = []
        cursor: str | None = None
        while True:
            page = client.list_agents(limit=200, cursor=cursor)
            items = page.get("items", [])
            agents.extend(items)
            cursor = (page.get("page") or {}).get("next_cursor")
            if not cursor:
                return agents

    try:
        with _make_client(server_url) as client:
            data = client.health()
            summary = None
            agents: list[dict] = []
            try:
                summary = client.ops_health()
                agents = _list_all_agents(client)
            except (_httpx.HTTPStatusError, _httpx.RequestError, RuntimeError):
                pass
        typer.echo(f"status: {data.get('status', 'ok')}")
        for k, v in data.get("components", {}).items():
            typer.echo(f"  {k}: {v}")
        if summary is not None:
            total_pending = int(((summary.get("queue") or {}).get("depth")) or 0)
            typer.echo(f"queue_depth_total: {total_pending}")
            if agents:
                busiest = max(agents, key=lambda agent: int(agent.get("queue_depth", 0) or 0))
                oldest = max(
                    (agent for agent in agents if agent.get("oldest_queue_age_seconds") is not None),
                    key=lambda agent: float(agent.get("oldest_queue_age_seconds") or 0.0),
                    default=None,
                )
                if int(busiest.get("queue_depth", 0) or 0) > 0:
                    typer.echo(f"direct_queue_busiest: {busiest['agent_id']}={int(busiest.get('queue_depth', 0) or 0)}")
                if oldest is not None:
                    typer.echo(
                        f"direct_queue_oldest_age: {oldest['agent_id']}={_format_duration(float(oldest.get('oldest_queue_age_seconds') or 0.0))}"
                    )
    except Exception as e:
        typer.echo(f"unreachable: {e}", err=True)
        raise typer.Exit(1)


def _status_job(job_id: str, server_url: str | None) -> None:
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
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

        if job["status"] in ("queued", "accepted", "leased", "running"):
            _print_peek_tip(job.get("target_agent_id", ""))

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
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            data = client.list_jobs(
                limit=limit,
                target_agent_id=agent,
                status=filter_status,
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
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
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            agents_data = client.list_agents()
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        agents = agents_data.get("items", [])

        try:
            caps_data = client.list_capabilities()
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        caps = caps_data.get("items", [])

        # Build capability name lookup
        cap_names: dict[str, str] = {}
        for c in caps:
            cap_names[c["capability_id"]] = c.get("name", c["capability_id"])

        # Build agent → runtime lookup (1:1 binding)
        agent_runtime: dict[str, str] = {}
        try:
            runtimes_data = client.ops_list_runtimes(limit=200)
            for rt in runtimes_data.get("items", []):
                aid = rt.get("agent_id")
                if aid:
                    agent_runtime[aid] = rt["runtime_id"]
        except Exception:
            pass  # ops endpoint may not be available

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
        active = list(agents)  # All agents in DB are live

        typer.echo("[ACTIVE AGENTS]")
        if not active:
            typer.echo("(none)")
        else:
            # Column headers
            typer.echo(
                f"{'ID':<20s} {'ROLE':<18s} {'STATUS':<8s} {'RUNTIME':<16s} "
                f"{'JOB_ID':<14s} {'TIME_ON_JOB':<12s} {'PENDING':<7s} {'QUEUE_AGE':<10s} {'WORKSPACE'}"
            )
            typer.echo("-" * 142)

            now = datetime.now(timezone.utc)
            for a in active:
                agent_id = a["agent_id"]
                role = ", ".join(a.get("capabilities", [])) or "-"
                agent_status = a.get("status", "?").upper()
                runtime_id = agent_runtime.get(agent_id, "-")
                workspace = a.get("workspace_ref") or "-"
                pending = str(a.get("queue_depth", 0))
                queue_age_seconds = a.get("oldest_queue_age_seconds")
                queue_age = _format_duration(queue_age_seconds) if isinstance(queue_age_seconds, (int, float)) else "-"

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
                    f"{job_id:<14s} {time_on_job:<12s} {pending:<7s} {queue_age:<10s} {workspace}"
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
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Try agent first, fall back to capability
        agent = None
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if agent is not None:
            _info_agent(agent, client)
        else:
            _info_capability(target, client)


def _info_agent(agent: dict, client) -> None:
    from datetime import datetime, timezone

    agent_id = agent["agent_id"]
    cap_id = ", ".join(agent.get("capabilities", [])) or "-"

    typer.echo(_SEPARATOR)
    typer.echo(f"      AGENT INFO: {agent_id}")
    typer.echo(_SEPARATOR)
    typer.echo("Logical agent view. Use `skyops runtime ...` to inspect the bound runtime directly.")

    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"CAPABILITIES: {', '.join(agent.get('capabilities', [])) or '-'}")

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

    # Capability blueprint section removed — agents now declare capabilities
    # as string arrays via /agents/up, not from the capabilities table.


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

    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            result = client.create_nudge(target, payload, priority=priority, source=source)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
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
