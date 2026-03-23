"""Event creation and sequence management."""

from __future__ import annotations

from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.models import Event, EventJobLink
from agp.services._helpers import _append_control_plane_log, _new_id

_event_seq_lock = Lock()
_event_seq_counter: int | None = None


def _next_event_seq(db: Session) -> int:
    """Allocate the next monotonic event sequence number.

    On PostgreSQL the database sequence ``events_event_seq_seq`` is
    authoritative.  On SQLite the ``_sqlite_sequences`` table provides
    an equivalent atomic counter.

    Falls back to a process-local counter only when the database
    sequences are not yet initialized (pre-migration state).
    """
    from agp.db import next_event_seq_db

    db_seq = next_event_seq_db(db)
    if db_seq is not None:
        return db_seq

    # Fallback: process-local counter (pre-migration only)
    global _event_seq_counter
    value = db.scalar(select(func.max(Event.event_seq)))
    db_max = int(value or 0)
    if _event_seq_counter is None:
        _event_seq_counter = db_max
    else:
        _event_seq_counter = max(_event_seq_counter, db_max)
    _event_seq_counter += 1
    return _event_seq_counter


def reset_event_seq() -> None:
    """Reset the process-local event sequence counter (for tests)."""
    global _event_seq_counter
    _event_seq_counter = None


def _create_event(
    db: Session,
    *,
    event_type: str,
    body: dict,
    job_id: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    runtime_id: str | None = None,
    related_jobs: list[tuple[str, str]] | None = None,
) -> Event:
    with _event_seq_lock:
        event = Event(
            event_id=_new_id("evt"),
            event_seq=_next_event_seq(db),
            job_id=job_id,
            run_id=run_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            event_type=event_type,
            body_json=body,
        )
        db.add(event)
        db.flush()
    for linked_job_id, relation in related_jobs or []:
        db.add(EventJobLink(event_id=event.event_id, job_id=linked_job_id, relation=relation))
    _append_control_plane_log(
        {
            "kind": "control_plane_event",
            "created_at": event.created_at,
            "event_id": event.event_id,
            "event_seq": event.event_seq,
            "event_type": event.event_type,
            "job_id": event.job_id,
            "run_id": event.run_id,
            "agent_id": event.agent_id,
            "runtime_id": event.runtime_id,
            "body": event.body_json,
        }
    )
    return event
