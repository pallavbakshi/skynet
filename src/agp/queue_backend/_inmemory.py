"""In-memory broker queue backend — process-local, for dev/test only."""

from __future__ import annotations

import time
from collections import deque
from typing import Deque

from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Job, utc_now
from agp.queue_backend import QueueDelivery, InMemoryInflightDelivery, _new_delivery_id, _peek_queued_jobs


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

    def peek_queue(self, db: Session, *, target_queues: list[str]) -> int:
        return _peek_queued_jobs(db, target_queues=target_queues)

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
        job = db.get(Job, inflight.job_id)
        if job is not None and job.status == JobStatus.QUEUED.value:
            job.updated_at = utc_now()

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
                job = db.get(Job, inflight.job_id)
                if job is not None and job.status == JobStatus.QUEUED.value:
                    job.status = JobStatus.FAILED.value
                    job.updated_at = utc_now()
                continue
            queue = self._queued(inflight.target_queue)
            if inflight.job_id not in queue:
                queue.appendleft(inflight.job_id)
            job = db.get(Job, inflight.job_id)
            if job is not None and job.status == JobStatus.QUEUED.value:
                job.updated_at = utc_now()
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


_INMEMORY_BROKER = InMemoryBrokerQueueBackend()
