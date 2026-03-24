"""Queue backend boundary for AGP.

Backend Contract
~~~~~~~~~~~~~~~~

Every queue backend implementation must provide the same semantic
guarantees, regardless of transport mechanism:

**Source of truth**: SQL ``queue_deliveries`` table is the durable
authority for delivery state.  External transports (Redis lists, in-memory
deques) are *acceleration layers* — operator-visible state and crash
recovery are always driven from the SQL records.

**Durability expectations**:

- ``db``: No separate transport; polls the jobs table directly.
- ``delivery_table``: SQL-only.  Fully durable.  Ordering via timestamps.
- ``inmemory_broker``: Process-local.  Lost on restart.  For dev/test only.
- ``redis``: Redis lists for fast dequeue; SQL shadow records for
  durability.  On crash, SQL records are the recovery source.

**Reconciliation**: ``redrive_stale_deliveries()`` resets in-flight
deliveries that exceeded the visibility timeout back to pending, or
dead-letters them after ``max_delivery_attempts``.

**Operator visibility**: All backends that create ``QueueDeliveryRecord``
rows expose state through the ``/queue/deliveries`` API endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import json
from typing import Deque, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Job, QueueDeliveryRecord, utc_now


def _new_delivery_id() -> str:
    return f"qdl_{uuid4().hex[:12]}"


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


class DbQueueBackend:
    """State-store-backed queue transport used for the MVP."""

    name = "db"

    def enqueue_job(self, db: Session, *, job: Job) -> None:  # noqa: ARG002
        return None

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:
        job = db.scalar(
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
                Job.target_queue.in_(target_queues),
            )
            .order_by(Job.created_at.asc())
        )
        if job is None:
            return None
        return QueueDelivery(
            delivery_id=f"legacy_{job.job_id}",
            job_id=job.job_id,
            target_queue=job.target_queue,
            delivery_attempt=0,
        )

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:  # noqa: ARG002
        return None

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:  # noqa: ARG002
        return None

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,  # noqa: ARG002
        max_delivery_attempts: int,  # noqa: ARG002
    ) -> dict[str, int]:
        return {"redriven_deliveries": 0, "dead_lettered_deliveries": 0}

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:  # noqa: ARG002
        return None


class DeliveryTableQueueBackend:
    """Broker-like queue transport persisted in the state store."""

    name = "delivery_table"

    def enqueue_job(self, db: Session, *, job: Job) -> None:
        existing = db.scalar(
            select(QueueDeliveryRecord).where(
                QueueDeliveryRecord.job_id == job.job_id,
                QueueDeliveryRecord.state.in_(("pending", "delivered")),
            )
        )
        now = utc_now()
        if existing is not None:
            existing.target_queue = job.target_queue
            existing.state = "pending"
            existing.available_at = now
            existing.updated_at = now
            return
        db.add(
            QueueDeliveryRecord(
                delivery_id=_new_delivery_id(),
                job_id=job.job_id,
                target_queue=job.target_queue,
                state="pending",
                delivery_attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:
        now = utc_now()
        delivery = db.scalar(
            select(QueueDeliveryRecord)
            .join(Job, Job.job_id == QueueDeliveryRecord.job_id)
            .where(
                QueueDeliveryRecord.state == "pending",
                QueueDeliveryRecord.available_at <= now,
                QueueDeliveryRecord.target_queue.in_(target_queues),
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
            )
            .order_by(QueueDeliveryRecord.available_at.asc(), QueueDeliveryRecord.created_at.asc())
        )
        if delivery is None:
            return None
        delivery.state = "delivered"
        delivery.delivery_attempt += 1
        delivery.last_delivered_at = now
        delivery.updated_at = now
        return QueueDelivery(
            delivery_id=delivery.delivery_id,
            job_id=delivery.job_id,
            target_queue=delivery.target_queue,
            delivery_attempt=delivery.delivery_attempt,
        )

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:  # noqa: ARG002
        record = db.get(QueueDeliveryRecord, delivery.delivery_id)
        if record is None:
            return
        record.state = "acked"
        record.acked_at = utc_now()
        record.updated_at = utc_now()

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:
        if delivery is None:
            return
        record = db.get(QueueDeliveryRecord, delivery.delivery_id)
        if record is None:
            return
        record.state = "pending"
        record.available_at = utc_now()
        record.updated_at = utc_now()

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,
    ) -> dict[str, int]:
        now = utc_now()
        stale_cutoff = now.timestamp() - visibility_timeout_seconds
        stale = db.scalars(
            select(QueueDeliveryRecord).where(
                QueueDeliveryRecord.state == "delivered",
                QueueDeliveryRecord.last_delivered_at.is_not(None),
            )
        ).all()
        redriven = 0
        dead_lettered = 0
        for record in stale:
            if record.last_delivered_at is None:
                continue
            if record.last_delivered_at.timestamp() > stale_cutoff:
                continue
            if record.delivery_attempt >= max_delivery_attempts:
                record.state = "dead_lettered"
                record.dead_lettered_at = now
                record.updated_at = now
                dead_lettered += 1
                continue
            record.state = "pending"
            record.available_at = now
            record.updated_at = now
            redriven += 1
        return {
            "redriven_deliveries": redriven,
            "dead_lettered_deliveries": dead_lettered,
        }

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:  # noqa: ARG002
        if not job_ids:
            return
        now = utc_now()
        rows = db.scalars(
            select(QueueDeliveryRecord).where(
                QueueDeliveryRecord.job_id.in_(job_ids),
                QueueDeliveryRecord.target_queue == target_queue,
                QueueDeliveryRecord.state.in_(("pending", "delivered")),
            )
        ).all()
        for row in rows:
            row.state = "acked"
            row.acked_at = now
            row.updated_at = now


class InMemoryBrokerQueueBackend:
    """In-process broker-style queue transport without external dependencies."""

    name = "inmemory_broker"

    def __init__(self) -> None:
        self._queues: dict[str, Deque[str]] = {}
        self._inflight: dict[str, InMemoryInflightDelivery] = {}
        self._job_attempts: dict[str, int] = {}
        self._dead_lettered_jobs: set[str] = set()

    def reset(self) -> None:
        self._queues.clear()
        self._inflight.clear()
        self._job_attempts.clear()
        self._dead_lettered_jobs.clear()

    def _queued(self, target_queue: str) -> Deque[str]:
        return self._queues.setdefault(target_queue, deque())

    def _job_in_queue(self, target_queue: str, job_id: str) -> bool:
        return job_id in self._queued(target_queue)

    def enqueue_job(self, db: Session, *, job: Job) -> None:  # noqa: ARG002
        if job.job_id in self._dead_lettered_jobs:
            self._dead_lettered_jobs.remove(job.job_id)
        if any(item.job_id == job.job_id for item in self._inflight.values()):
            return
        queue = self._queued(job.target_queue)
        if job.job_id not in queue:
            queue.append(job.job_id)

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:  # noqa: ARG002
        now_ts = utc_now().timestamp()
        for target_queue in target_queues:
            queue = self._queued(target_queue)
            while queue:
                job_id = queue.popleft()
                if job_id in self._dead_lettered_jobs:
                    continue
                job = db.get(Job, job_id)
                if job is None or job.status != JobStatus.QUEUED.value or job.target_queue != target_queue:
                    continue
                attempt = self._job_attempts.get(job_id, 0) + 1
                self._job_attempts[job_id] = attempt
                delivery_id = _new_delivery_id()
                self._inflight[delivery_id] = InMemoryInflightDelivery(
                    delivery_id=delivery_id,
                    job_id=job_id,
                    target_queue=target_queue,
                    delivery_attempt=attempt,
                    delivered_at_ts=now_ts,
                )
                return QueueDelivery(
                    delivery_id=delivery_id,
                    job_id=job_id,
                    target_queue=target_queue,
                    delivery_attempt=attempt,
                )
        return None

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:  # noqa: ARG002
        self._inflight.pop(delivery.delivery_id, None)

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:  # noqa: ARG002
        if delivery is None:
            return
        inflight = self._inflight.pop(delivery.delivery_id, None)
        if inflight is None:
            return
        queue = self._queued(inflight.target_queue)
        if inflight.job_id not in queue:
            queue.appendleft(inflight.job_id)

    def redrive_stale_deliveries(
        self,
        db: Session,  # noqa: ARG002
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,
    ) -> dict[str, int]:
        now_ts = utc_now().timestamp()
        stale_before = now_ts - visibility_timeout_seconds
        redriven = 0
        dead_lettered = 0
        for delivery_id, inflight in list(self._inflight.items()):
            if inflight.delivered_at_ts > stale_before:
                continue
            self._inflight.pop(delivery_id, None)
            if inflight.delivery_attempt >= max_delivery_attempts:
                self._dead_lettered_jobs.add(inflight.job_id)
                dead_lettered += 1
                continue
            queue = self._queued(inflight.target_queue)
            if inflight.job_id not in queue:
                queue.appendleft(inflight.job_id)
            redriven += 1
        return {
            "redriven_deliveries": redriven,
            "dead_lettered_deliveries": dead_lettered,
        }

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:  # noqa: ARG002
        if not job_ids:
            return
        job_id_set = set(job_ids)
        queue = self._queued(target_queue)
        self._queues[target_queue] = deque(job_id for job_id in queue if job_id not in job_id_set)
        for delivery_id, inflight in list(self._inflight.items()):
            if inflight.target_queue == target_queue and inflight.job_id in job_id_set:
                self._inflight.pop(delivery_id, None)


_INMEMORY_BROKER = InMemoryBrokerQueueBackend()
_REDIS_BACKENDS: dict[tuple[str, str], "RedisQueueBackend"] = {}
_REDIS_CLIENT_FACTORY = None


def _load_redis_client(url: str):
    global _REDIS_CLIENT_FACTORY
    if _REDIS_CLIENT_FACTORY is not None:
        return _REDIS_CLIENT_FACTORY(url)
    try:
        from redis import Redis
    except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
        raise RuntimeError("redis queue backend requires the 'redis' package") from exc
    return Redis.from_url(url, decode_responses=True)


class RedisQueueBackend:
    """Redis-backed transport with SQL as authoritative delivery state.

    **Authority model**: SQL ``queue_deliveries`` is the source of truth
    for delivery lifecycle (pending → delivered → acked / dead_lettered).
    Redis lists and sets are the *acceleration transport* — they provide
    fast O(1) enqueue/dequeue but are rebuildable from SQL.

    On crash between Redis and SQL operations, the SQL state wins:

    - If Redis dequeued but SQL was not updated → redrive recovers it.
    - If SQL was acked but Redis still has inflight metadata → harmless;
      next ``redrive_stale_deliveries`` cleans up.
    """

    name = "redis"

    def __init__(self, *, redis_url: str, key_prefix: str) -> None:
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.client = _load_redis_client(redis_url)

    def _queue_key(self, target_queue: str) -> str:
        return f"{self.key_prefix}:queue:{target_queue}"

    def _pending_set_key(self, target_queue: str) -> str:
        return f"{self.key_prefix}:queue:{target_queue}:pending"

    def _inflight_jobs_key(self) -> str:
        return f"{self.key_prefix}:inflight_jobs"

    def _inflight_hash_key(self) -> str:
        return f"{self.key_prefix}:inflight"

    def _dead_lettered_jobs_key(self) -> str:
        return f"{self.key_prefix}:dead_lettered_jobs"

    def reset(self) -> None:
        flushdb = getattr(self.client, "flushdb", None)
        if callable(flushdb):
            flushdb()

    def enqueue_job(self, db: Session, *, job: Job) -> None:
        existing = db.scalar(select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id))
        now = utc_now()
        if existing is None:
            existing = QueueDeliveryRecord(
                delivery_id=_new_delivery_id(),
                job_id=job.job_id,
                target_queue=job.target_queue,
                state="pending",
                delivery_attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(existing)
        else:
            existing.target_queue = job.target_queue
            existing.state = "pending"
            existing.available_at = now
            existing.updated_at = now
            existing.dead_lettered_at = None

        self.client.srem(self._dead_lettered_jobs_key(), job.job_id)
        if self.client.sismember(self._inflight_jobs_key(), job.job_id):
            return
        pending_set = self._pending_set_key(job.target_queue)
        if self.client.sismember(pending_set, job.job_id):
            return
        self.client.rpush(self._queue_key(job.target_queue), job.job_id)
        self.client.sadd(pending_set, job.job_id)

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:
        """Dequeue the next candidate delivery.

        SQL is the durable authority.  The sequence is:
        1. ``LPOP`` from Redis (necessary — Redis lists don't support peek)
        2. Immediately mark the SQL delivery record as "delivered" and
           ``flush()`` so the state is durable in the DB transaction.
        3. Write the Redis inflight hash for fast visibility.

        If the process dies between step 1 and step 2, the job vanishes
        from the Redis list while SQL still says "pending".  The phase-2
        orphan scan in ``redrive_stale_deliveries`` recovers these by
        checking for SQL "pending" records whose job is no longer in the
        Redis pending set and re-enqueuing them.
        """
        now = utc_now()
        for target_queue in target_queues:
            while True:
                job_id = self.client.lpop(self._queue_key(target_queue))
                if job_id is None:
                    break
                self.client.srem(self._pending_set_key(target_queue), job_id)
                if self.client.sismember(self._dead_lettered_jobs_key(), job_id):
                    continue
                job = db.get(Job, job_id)
                if job is None or job.status != JobStatus.QUEUED.value or job.retry_count >= job.max_retries:
                    continue
                # Ensure SQL delivery record exists
                record = db.scalar(select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job_id))
                if record is None:
                    record = QueueDeliveryRecord(
                        delivery_id=_new_delivery_id(),
                        job_id=job_id,
                        target_queue=target_queue,
                        state="pending",
                        delivery_attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(record)
                    db.flush()
                # Transition to "delivered" in SQL immediately and flush —
                # this is the durable claim marker.
                record.target_queue = target_queue
                record.state = "delivered"
                record.delivery_attempt += 1
                record.last_delivered_at = now
                record.updated_at = now
                db.flush()
                payload = {
                    "delivery_id": record.delivery_id,
                    "job_id": job_id,
                    "target_queue": target_queue,
                    "delivery_attempt": record.delivery_attempt,
                    "delivered_at_ts": now.timestamp(),
                }
                self.client.hset(self._inflight_hash_key(), record.delivery_id, json.dumps(payload))
                self.client.sadd(self._inflight_jobs_key(), job_id)
                return QueueDelivery(
                    delivery_id=record.delivery_id,
                    job_id=job_id,
                    target_queue=target_queue,
                    delivery_attempt=record.delivery_attempt,
                )
        return None

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:  # noqa: ARG002
        record = db.get(QueueDeliveryRecord, delivery.delivery_id)
        if record is not None:
            record.state = "acked"
            record.acked_at = utc_now()
            record.updated_at = utc_now()
        payload = self.client.hget(self._inflight_hash_key(), delivery.delivery_id)
        self.client.hdel(self._inflight_hash_key(), delivery.delivery_id)
        if payload is not None:
            job_id = json.loads(payload)["job_id"]
            self.client.srem(self._inflight_jobs_key(), job_id)

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:
        if delivery is None:
            return
        payload = self.client.hget(self._inflight_hash_key(), delivery.delivery_id)
        if payload is None:
            return
        item = json.loads(payload)
        self.client.hdel(self._inflight_hash_key(), delivery.delivery_id)
        self.client.srem(self._inflight_jobs_key(), item["job_id"])
        if self.client.sismember(self._dead_lettered_jobs_key(), item["job_id"]):
            return
        pending_set = self._pending_set_key(item["target_queue"])
        if not self.client.sismember(pending_set, item["job_id"]):
            self.client.rpush(self._queue_key(item["target_queue"]), item["job_id"])
            self.client.sadd(pending_set, item["job_id"])
        record = db.get(QueueDeliveryRecord, delivery.delivery_id)
        if record is not None:
            record.state = "pending"
            record.available_at = utc_now()
            record.updated_at = utc_now()

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,
    ) -> dict[str, int]:
        now_ts = utc_now().timestamp()
        stale_before = now_ts - visibility_timeout_seconds
        redriven = 0
        dead_lettered = 0

        # Phase 1: scan Redis inflight hash (normal path)
        for delivery_id in list(self.client.hkeys(self._inflight_hash_key())):
            payload = self.client.hget(self._inflight_hash_key(), delivery_id)
            if payload is None:
                continue
            item = json.loads(payload)
            record = db.get(QueueDeliveryRecord, delivery_id)
            if record is not None and record.last_delivered_at is not None:
                if record.last_delivered_at.timestamp() > stale_before:
                    continue
            elif item["delivered_at_ts"] > stale_before:
                continue
            self.client.hdel(self._inflight_hash_key(), delivery_id)
            self.client.srem(self._inflight_jobs_key(), item["job_id"])
            now = utc_now()
            if record is not None and record.delivery_attempt >= max_delivery_attempts:
                record.state = "dead_lettered"
                record.dead_lettered_at = now
                record.updated_at = now
                self.client.sadd(self._dead_lettered_jobs_key(), item["job_id"])
                dead_lettered += 1
                continue
            if record is not None:
                record.state = "pending"
                record.available_at = now
                record.updated_at = now
            pending_set = self._pending_set_key(item["target_queue"])
            if not self.client.sismember(pending_set, item["job_id"]):
                self.client.rpush(self._queue_key(item["target_queue"]), item["job_id"])
                self.client.sadd(pending_set, item["job_id"])
            redriven += 1

        # Phase 2: scan SQL "delivered" records that have no Redis inflight
        # entry.  This handles the crash-between case where SQL was flushed
        # but the process died before writing the Redis inflight hash.
        # Re-query to exclude records already moved by phase 1.
        db.flush()
        orphaned = db.scalars(
            select(QueueDeliveryRecord).where(
                QueueDeliveryRecord.state == "delivered",
                QueueDeliveryRecord.last_delivered_at.is_not(None),
            )
        ).all()
        for record in orphaned:
            if record.last_delivered_at is None:
                continue
            if record.last_delivered_at.timestamp() > stale_before:
                continue
            # Check if Redis still tracks this delivery
            if self.client.hget(self._inflight_hash_key(), record.delivery_id) is not None:
                continue  # already handled in phase 1
            now = utc_now()
            if record.delivery_attempt >= max_delivery_attempts:
                record.state = "dead_lettered"
                record.dead_lettered_at = now
                record.updated_at = now
                self.client.sadd(self._dead_lettered_jobs_key(), record.job_id)
                dead_lettered += 1
            else:
                record.state = "pending"
                record.available_at = now
                record.updated_at = now
                pending_set = self._pending_set_key(record.target_queue)
                if not self.client.sismember(pending_set, record.job_id):
                    self.client.rpush(self._queue_key(record.target_queue), record.job_id)
                    self.client.sadd(pending_set, record.job_id)
                redriven += 1

        # Phase 3: recover SQL "pending" records whose job vanished from
        # the Redis pending set (crash between LPOP and SQL flush).
        pending_sql = db.scalars(
            select(QueueDeliveryRecord).where(QueueDeliveryRecord.state == "pending")
        ).all()
        for record in pending_sql:
            pending_set = self._pending_set_key(record.target_queue)
            if self.client.sismember(pending_set, record.job_id):
                continue  # already in Redis
            # Job was removed from Redis but SQL still says pending — re-enqueue
            self.client.rpush(self._queue_key(record.target_queue), record.job_id)
            self.client.sadd(pending_set, record.job_id)
            redriven += 1

        return {"redriven_deliveries": redriven, "dead_lettered_deliveries": dead_lettered}

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:  # noqa: ARG002
        if not job_ids:
            return
        job_id_set = set(job_ids)
        pending_set = self._pending_set_key(target_queue)
        for job_id in job_ids:
            self.client.srem(pending_set, job_id)
            self.client.srem(self._inflight_jobs_key(), job_id)
        queue_key = self._queue_key(target_queue)
        existing = list(getattr(self.client, "lrange", lambda key, start, end: [])(queue_key, 0, -1))
        if existing:
            filtered = [job_id for job_id in existing if job_id not in job_id_set]
            delete = getattr(self.client, "delete", None)
            if callable(delete):
                delete(queue_key)
            else:
                self.client.lists[queue_key] = []  # type: ignore[attr-defined]
            for job_id in filtered:
                self.client.rpush(queue_key, job_id)
        for delivery_id in list(self.client.hkeys(self._inflight_hash_key())):
            payload = self.client.hget(self._inflight_hash_key(), delivery_id)
            if payload is None:
                continue
            item = json.loads(payload)
            if item.get("job_id") in job_id_set:
                self.client.hdel(self._inflight_hash_key(), delivery_id)


def reset_queue_backend_state(name: str | None = None) -> None:
    if name is None or name == "inmemory_broker":
        _INMEMORY_BROKER.reset()
    if name is None or name == "redis":
        for backend in _REDIS_BACKENDS.values():
            backend.reset()
        _REDIS_BACKENDS.clear()


def get_queue_backend(name: str = "db") -> QueueBackend:
    from agp.config import settings

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
