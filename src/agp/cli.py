"""CLI entrypoint for the AGP scaffold — service commands only.

Operator, debug, and API-client commands have moved to ``skyops``.
SDK client functions live in ``agp.client``.
Operator helper functions live in ``agp._ops_helpers``.
"""

from threading import Event

import uvicorn
import typer

from agp.config import settings
from agp.client import RuntimeClient, RuntimeIdentity
from agp.control_plane import (
    build_app,
    sweep_expired_leases,
    sweep_stale_runtimes,
)
from agp.db import SessionLocal, init_db
from agp.plugins import build_terminal_host, build_agent_adapter
from agp.runtime import RuntimeSupervisor
from agp.sweeper import LeaseSweeperService, SweeperService

app = typer.Typer(help="AGP control plane scaffold")


@app.command()
def initdb() -> None:
    """Initialize the local database schema."""

    init_db()
    typer.echo("Initialized database schema.")


@app.command()
def serve(
    host: str = settings.host,
    port: int = settings.port,
) -> None:
    """Run the AGP control plane API server."""

    uvicorn.run(build_app(), host=host, port=port)


@app.command()
def runtime_work_loop(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    hostname: str | None = None,
    agent_id: str | None = None,
    capability_id: str | None = None,
    artifact_root: str = ".agp-artifacts",
    idle_sleep_seconds: float = 0.25,
    max_iterations: int | None = None,
    max_local_recoveries: int = 1,
    host_kind: str = settings.runtime_terminal_host_kind,
    adapter_kind: str = settings.runtime_agent_adapter_kind,
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""

    import socket as _socket

    actual_hostname = hostname or _socket.gethostname()
    client = RuntimeClient(
        RuntimeIdentity(runtime_id=runtime_id, hostname=actual_hostname, server_url=server_url)
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(host_kind, workspace=settings.wezterm_workspace),
        adapter=build_agent_adapter(adapter_kind),
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
    stale_timeout_seconds: int = settings.runtime_stale_timeout_seconds,
) -> None:
    """Continuously mark stale runtimes offline and detach or degrade bound agents."""

    service = SweeperService(
        session_factory=SessionLocal,
        sweep_fn=lambda session: sweep_stale_runtimes(
            session,
            stale_timeout_seconds=stale_timeout_seconds,
        ),
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


if __name__ == "__main__":
    app()
