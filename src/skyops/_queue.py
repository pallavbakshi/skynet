"""``skyops queue``, ``skyops job``, ``skyops sweep`` — direct DB operations."""

from __future__ import annotations

import json

import typer

queue_app = typer.Typer(help="Queue management commands.")
job_app = typer.Typer(help="Job management commands.")
sweep_app = typer.Typer(help="One-shot sweep operations.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@queue_app.command("reconstruct")
def queue_reconstruct() -> None:
    """Rebuild queue from current DB state."""
    from agp._ops_helpers import reconstruct_queue_from_state

    result = reconstruct_queue_from_state()
    _emit(result)


@queue_app.command("redrive")
def queue_redrive(
    visibility_timeout: int = typer.Option(30, "--visibility-timeout", help="Visibility timeout seconds."),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Max delivery attempts."),
) -> None:
    """Redrive stale in-flight deliveries."""
    from agp.config import settings
    from agp.db import SessionLocal
    from agp.queue_backend import get_queue_backend

    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        result = backend.redrive_stale_deliveries(
            session,
            visibility_timeout_seconds=visibility_timeout,
            max_delivery_attempts=max_attempts,
        )
        session.commit()
        _emit(result)
    finally:
        session.close()


@job_app.command("block")
def job_block(
    job_id: str = typer.Argument(help="Job ID to block."),
    reason: str = typer.Option("operator-block", "--reason", "-r", help="Block reason."),
) -> None:
    """Block a queued job."""
    from agp.control_plane import _block_job, _require_job
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        job = _require_job(session, job_id=job_id)
        _block_job(session, job=job, reason=reason)
        session.commit()
        typer.echo(f"Job {job_id} blocked.")
    finally:
        session.close()


@job_app.command("unblock")
def job_unblock(
    job_id: str = typer.Argument(help="Job ID to unblock."),
    reason: str = typer.Option("operator-unblock", "--reason", "-r", help="Unblock reason."),
) -> None:
    """Unblock a blocked job."""
    from agp.control_plane import _unblock_job, _require_job
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        job = _require_job(session, job_id=job_id)
        _unblock_job(session, job=job, reason=reason)
        session.commit()
        typer.echo(f"Job {job_id} unblocked.")
    finally:
        session.close()


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


@sweep_app.command("idle")
def sweep_idle(
    timeout: int | None = typer.Option(None, "--timeout", help="Idle timeout in seconds (uses config default if omitted)."),
) -> None:
    """One-shot sweep of idle agents."""
    from agp.control_plane import sweep_idle_agents
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        kwargs = {}
        if timeout is not None:
            kwargs["idle_timeout_seconds"] = timeout
        result = sweep_idle_agents(session, **kwargs)
        _emit(result)
    finally:
        session.close()


@sweep_app.command("draining")
def sweep_draining() -> None:
    """One-shot sweep of draining agents."""
    from agp.control_plane import sweep_draining_agents
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        result = sweep_draining_agents(session)
        _emit(result)
    finally:
        session.close()
