"""``skyops queue`` and ``skyops sweep`` — direct DB queue operations."""

from __future__ import annotations

import json

import typer

queue_app = typer.Typer(help="Queue management commands.")
sweep_app = typer.Typer(help="One-shot sweep operations.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@queue_app.command("reconstruct")
def queue_reconstruct() -> None:
    """Rebuild queue from current DB state."""
    from agp._ops_helpers import reconstruct_queue_from_state

    result = reconstruct_queue_from_state()
    _emit(result)


@sweep_app.command("leases")
def sweep_leases() -> None:
    """One-shot sweep of expired leases."""
    from agp.control_plane import sweep_expired_leases
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        result = sweep_expired_leases(session)
        _emit(result)
    finally:
        session.close()


@sweep_app.command("runtimes")
def sweep_runtimes(
    stale_timeout: int = typer.Option(90, "--timeout", help="Stale timeout in seconds."),
) -> None:
    """One-shot sweep of stale runtimes."""
    from agp.control_plane import sweep_stale_runtimes
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        result = sweep_stale_runtimes(session, stale_timeout_seconds=stale_timeout)
        _emit(result)
    finally:
        session.close()
