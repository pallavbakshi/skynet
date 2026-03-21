"""CLI entrypoint for the AGP scaffold — service commands only.

Operator, debug, and API-client commands have moved to ``skyops``.
SDK client functions live in ``agp.client``.
Operator helper functions live in ``agp._ops_helpers``.

All server-side imports are deferred to command bodies so that
``pip install agp`` (without ``[server]``) can still import
``agp.client`` without pulling in uvicorn/sqlalchemy/pydantic-settings.
"""

import os

import typer

app = typer.Typer(help="AGP control plane scaffold")


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


@app.command()
def initdb() -> None:
    """Initialize the local database schema."""
    _require_server_extra()

    from agp.db import init_db

    init_db()
    typer.echo("Initialized database schema.")


@app.command()
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


@app.command()
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
    client = RuntimeClient(
        RuntimeIdentity(runtime_id=runtime_id, hostname=actual_hostname, server_url=actual_server_url)
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


@app.command()
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


@app.command()
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


if __name__ == "__main__":
    app()
