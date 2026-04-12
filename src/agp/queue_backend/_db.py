"""DB-poll queue backend — no external transport, queries jobs table directly."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Job, QueueDeliveryRecord, utc_now
from agp.queue_backend import QueueDelivery, _new_delivery_id, _peek_queued_jobs, db_dialect_name


class DbQueueBackend:
    """State-store-backed queue transport used for the MVP."""

    name = "db"

    def enqueue_job(self, db: Session, *, job: Job) -> None:  # noqa: ARG002
        return None

    def _candidate_query(self, db: Session, *, target_queues: list[str]):
        query = (
            select(Job.job_id)
            .where(
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
                Job.target_queue.in_(target_queues),
            )
            .order_by(Job.created_at.asc())
            .limit(1)
        )
        if db_dialect_name(db) == "postgresql":
            query = query.with_for_update(skip_locked=True)
        return query

    def dequeue_candidate(self, db: Session, *, target_queues: list[str]) -> QueueDelivery | None:
        now = utc_now()
        candidate = self._candidate_query(db, target_queues=target_queues)
        row = db.execute(
            update(Job)
            .where(
                Job.job_id == candidate.scalar_subquery(),
                Job.status == JobStatus.QUEUED.value,
            )
            .values(status=JobStatus.ACCEPTED.value, updated_at=now)
            .returning(Job.job_id, Job.target_queue)
        ).mappings().first()
        if row is None:
            return None
        return QueueDelivery(
            delivery_id=f"legacy_{row['job_id']}",
            job_id=row["job_id"],
            target_queue=row["target_queue"],
            delivery_attempt=0,
        )

    def peek_queue(self, db: Session, *, target_queues: list[str]) -> int:
        return _peek_queued_jobs(db, target_queues=target_queues)

    def ack_claim(self, db: Session, *, delivery: QueueDelivery, job: Job) -> None:  # noqa: ARG002
        return None

    def release_unclaimed(self, db: Session, *, delivery: QueueDelivery | None) -> None:
        if delivery is None:
            return
        db.execute(
            update(Job)
            .where(
                Job.job_id == delivery.job_id,
                Job.status == JobStatus.ACCEPTED.value,
            )
            .values(status=JobStatus.QUEUED.value, updated_at=utc_now())
        )

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,  # noqa: ARG002
    ) -> dict[str, int]:
        from datetime import timedelta

        now = utc_now()
        cutoff = now - timedelta(seconds=visibility_timeout_seconds)
        result = db.execute(
            update(Job)
            .where(
                Job.status == JobStatus.ACCEPTED.value,
                Job.updated_at < cutoff,
            )
            .values(status=JobStatus.QUEUED.value, updated_at=now)
        )
        redriven = result.rowcount
        return {"redriven_deliveries": redriven, "dead_lettered_deliveries": 0}

    def remove_jobs(self, db: Session, *, target_queue: str, job_ids: list[str]) -> None:  # noqa: ARG002
        return None
