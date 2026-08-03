"""Hidden infrastructure commands — initdb, serve, runtime loops, sweeper."""

from __future__ import annotations

import json
import logging
import os
import time

import typer

from agp.cli import app
from agp.cli._helpers import _default_server_url


@app.command(hidden=True)
def initdb() -> None:
    """Initialize or migrate the database schema."""


    from agp.db import init_db

    init_db()
    typer.echo("Initialized database schema.")


@app.command(name="db-status", hidden=True)
def db_status() -> None:
    """Show current schema version and pending migrations."""


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


def _runtime_debug_log(runtime_id: str, entry: dict) -> None:
    """Write a structured entry to the runtime JSONL log if debug logging is on."""
    _rtl = logging.getLogger("agp")
    if not _rtl.isEnabledFor(logging.DEBUG):
        return
    try:
        from datetime import UTC, datetime

        from agp.config import settings
        from agp.logs import append_jsonl_log
        path = settings.log_root / f"runtime-{runtime_id}.jsonl"
        payload = {"created_at": datetime.now(UTC).isoformat(), "kind": "runtime_lifecycle", **entry}
        append_jsonl_log(path, payload, rotation_bytes=settings.observability_log_rotation_bytes)
    except Exception:
        pass


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
    workspace: str | None = typer.Option(None, help="Working directory for the agent's terminal session (default: runtime's cwd)."),
    log_level: str = typer.Option("WARNING", help="Python log level (DEBUG, INFO, WARNING, ERROR)."),
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""

    if isinstance(log_level, str):
        level = getattr(logging, log_level.upper(), logging.WARNING)
        # Scope to agp loggers only — avoid flooding stderr with httpcore/httpx transport noise
        logging.basicConfig(level=logging.WARNING, force=True)
        logging.getLogger("agp").setLevel(level)

    import socket as _socket
    from threading import Event

    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.config import settings
    from agp.plugins import build_agent_adapter, build_terminal_host
    from agp.runtime import RuntimeSupervisor

    actual_server_url = server_url if server_url is not None else _default_server_url()
    actual_host_kind = host_kind if host_kind is not None else settings.runtime_terminal_host_kind
    actual_adapter_kind = adapter_kind if adapter_kind is not None else settings.runtime_agent_adapter_kind

    actual_hostname = hostname or _socket.gethostname()
    runtime_token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN") or None
    resolved_capabilities = [
        "".join(ch for ch in c.strip() if ch.isprintable())
        for c in capabilities.split(",")
        if c.strip()
    ] if capabilities else None
    payload: list[dict] = []
    restart_attempt = 0
    max_restart_attempts = int(os.environ.get("AGP_MAX_RUNTIME_RESTARTS", "3"))
    _runtime_debug_log(runtime_id, {"action": "startup", "host_kind": actual_host_kind, "adapter_kind": actual_adapter_kind, "agent_id": agent_id})

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
            workspace_ref=workspace,
        )
        import httpx as _httpx
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
            _runtime_debug_log(runtime_id, {"action": "shutdown_clean", "iterations": len(payload)})
            break
        except _httpx.HTTPStatusError as exc:
            # 4xx errors are non-retryable (auth failure, bad config, etc.)
            if 400 <= exc.response.status_code < 500:
                _runtime_debug_log(runtime_id, {"action": "fatal_http_error", "status": exc.response.status_code, "error": str(exc)})
                typer.echo(
                    f"[runtime] fatal HTTP {exc.response.status_code}: {exc}; exiting",
                    err=True,
                )
                raise typer.Exit(1) from exc
            # 5xx — transient, fall through to retry
            _runtime_debug_log(runtime_id, {"action": "transient_http_error", "status": exc.response.status_code, "error": str(exc), "attempt": restart_attempt + 1})
            restart_attempt += 1
        except (ValueError, TypeError) as exc:
            # Config/setup errors — non-retryable
            _runtime_debug_log(runtime_id, {"action": "fatal_config_error", "error": f"{type(exc).__name__}: {exc}"})
            typer.echo(
                f"[runtime] fatal config error: {type(exc).__name__}: {exc}; exiting",
                err=True,
            )
            raise typer.Exit(1) from exc
        except Exception as exc:
            _runtime_debug_log(runtime_id, {"action": "worker_crash", "error": f"{type(exc).__name__}: {exc}", "attempt": restart_attempt + 1})
            restart_attempt += 1
        finally:
            stop_event.set()
            client.close()
        if restart_attempt > max_restart_attempts:
            _runtime_debug_log(runtime_id, {"action": "shutdown_max_restarts", "attempts": restart_attempt})
            typer.echo(
                f"[runtime] giving up after {restart_attempt} restart attempts; exiting",
                err=True,
            )
            raise typer.Exit(1)
        backoff_seconds = min(30.0, max(idle_sleep_seconds, 0.25) * (2 ** (restart_attempt - 1)))
        typer.echo(
            f"[runtime] worker error (attempt {restart_attempt}/{max_restart_attempts}); "
            f"reinitializing in {backoff_seconds:.1f}s",
            err=True,
        )
        time.sleep(backoff_seconds)
    typer.echo(json.dumps(payload))


def _runtime_binding_warning(client, agent_id: str) -> str | None:
    runtime_id = f"rtm_{agent_id}"
    try:
        getter = getattr(client, "ops_get_runtime", None) or getattr(client, "get_runtime", None)
        runtime = getter(runtime_id) if getter is not None else None
    except Exception:
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


