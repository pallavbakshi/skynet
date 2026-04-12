"""Queue backend boundary for AGP.

Backend Contract
~~~~~~~~~~~~~~~~

Every queue backend implementation must provide the same semantic
guarantees, regardless of transport mechanism.  See individual backend
modules for implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Job, QueueDeliveryRecord, utc_now


def _new_delivery_id() -> str:
    return f"qdl_{uuid4().hex[:12]}"


def agent_queue_targets(*, agent_id: str) -> list[str]:
    return [f"agent:{agent_id}"]


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _peek_queued_jobs(db: Session, *, target_queues: list[str]) -> int:
    if not target_queues:
        return 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status == JobStatus.QUEUED.value,
                Job.target_queue.in_(target_queues),
            )
        )
        or 0
    )


def queue_depth(db: Session, *, target_queues: list[str]) -> int:
    return _peek_queued_jobs(db, target_queues=target_queues)


def queue_oldest_queued_at(db: Session, *, target_queues: list[str] | None = None) -> datetime | None:
    query = select(Job.updated_at).where(Job.status == JobStatus.QUEUED.value)
    if target_queues is not None:
        if not target_queues:
            return None
        query = query.where(Job.target_queue.in_(target_queues))
    return _normalize_timestamp(db.scalar(query.order_by(Job.updated_at.asc()).limit(1)))


def queue_backlogs_by_target_queue(db: Session, *, target_queues: list[str]) -> dict[str, dict[str, object]]:
    if not target_queues:
        return {}
    ordered_targets = list(dict.fromkeys(target_queues))
    items = {
        target_queue: {"queue_depth": 0, "oldest_queued_at": None}
        for target_queue in ordered_targets
    }
    rows = db.execute(
        select(
            Job.target_queue,
            func.count().label("queue_depth"),
            func.min(Job.updated_at).label("oldest_queued_at"),
        )
        .where(
            Job.status == JobStatus.QUEUED.value,
            Job.target_queue.in_(ordered_targets),
        )
        .group_by(Job.target_queue)
    ).all()
    for target_queue, queued_count, oldest_queued_at in rows:
        items[str(target_queue)] = {
            "queue_depth": int(queued_count),
            "oldest_queued_at": _normalize_timestamp(oldest_queued_at),
        }
    return items


def queue_backlog_info(db: Session, *, target_queues: list[str]) -> dict[str, object]:
    if not target_queues:
        return {"queue_depth": 0, "oldest_queued_at": None}
    backlog_by_queue = queue_backlogs_by_target_queue(db, target_queues=target_queues)
    queue_depth_total = 0
    oldest_queued_at: datetime | None = None
    for target_queue in list(dict.fromkeys(target_queues)):
        backlog = backlog_by_queue.get(target_queue, {"queue_depth": 0, "oldest_queued_at": None})
        queue_depth_total += int(backlog["queue_depth"])
        candidate = backlog["oldest_queued_at"]
        if candidate is not None and (oldest_queued_at is None or candidate < oldest_queued_at):
            oldest_queued_at = candidate
    return {"queue_depth": queue_depth_total, "oldest_queued_at": oldest_queued_at}


def db_dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return bind.dialect.name if bind is not None else ""

@dataclass(slots=True)
class QueueDelivery:
    """A candidate delivery returned by a queue backend."""

    delivery_id: str
    job_id: str
    target_queue: str
    delivery_attempt: int


@dataclass(slots=True)
class InMemoryInflightDelivery:
    """In-memory broker delivery reservation."""

    delivery_id: str
    job_id: str
    target_queue: str
    delivery_attempt: int
    delivered_at_ts: float


class QueueBackend(Protocol):
    """Transport abstraction for queue-backed delivery."""

    name: str

    def enqueue_job(self, db: Session, *, job: Job) -> None:
        """Publish or expose a queued job to its target queue."""

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:
        """Return the next candidate delivery for the given eligible queues."""

    def peek_queue(self, db: Session, *, target_queues: list[str]) -> int:
        """Return the count of queued jobs for the given eligible queues."""

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:
        """Acknowledge durable claim after control-plane run/lease creation."""

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:
        """Release a delivery that was not authoritatively claimed."""

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,
    ) -> dict[str, int]:
        """Return stale in-flight deliveries back to pending state or dead-letter them."""

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:
        """Best-effort transport cleanup for jobs removed from a queue."""



def reset_queue_backend_state(name: str | None = None) -> None:
    from agp.queue_backend._inmemory import _INMEMORY_BROKER
    from agp.queue_backend._redis import _REDIS_BACKENDS
    if name is None or name == "inmemory_broker":
        _INMEMORY_BROKER.reset()
    if name is None or name == "redis":
        for backend in _REDIS_BACKENDS.values():
            backend.reset()
        _REDIS_BACKENDS.clear()


def get_queue_backend(name: str = "db") -> QueueBackend:
    from agp.config import settings
    from agp.queue_backend._db import DbQueueBackend
    from agp.queue_backend._delivery_table import DeliveryTableQueueBackend
    from agp.queue_backend._inmemory import _INMEMORY_BROKER
    from agp.queue_backend._redis import RedisQueueBackend, _REDIS_BACKENDS

    if name == "db":
        return DbQueueBackend()
    if name == "delivery_table":
        return DeliveryTableQueueBackend()
    if name == "inmemory_broker":
        return _INMEMORY_BROKER
    if name == "redis":
        key = (settings.redis_url, settings.redis_queue_key_prefix)
        backend = _REDIS_BACKENDS.get(key)
        if backend is None:
            backend = RedisQueueBackend(redis_url=settings.redis_url, key_prefix=settings.redis_queue_key_prefix)
            _REDIS_BACKENDS[key] = backend
        return backend
    raise ValueError(f"unsupported queue backend: {name}")

# Re-export backend classes for external consumers
from agp.queue_backend._db import DbQueueBackend as DbQueueBackend  # noqa: F401,E402
from agp.queue_backend._delivery_table import DeliveryTableQueueBackend as DeliveryTableQueueBackend  # noqa: F401,E402
from agp.queue_backend._inmemory import InMemoryBrokerQueueBackend as InMemoryBrokerQueueBackend  # noqa: F401,E402
from agp.queue_backend._redis import RedisQueueBackend as RedisQueueBackend  # noqa: F401,E402
