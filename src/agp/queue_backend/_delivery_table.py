"""Delivery-table queue backend — SQL-only, fully durable."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Job, QueueDeliveryRecord, utc_now
from agp.queue_backend import QueueDelivery, _new_delivery_id, _peek_queued_jobs, db_dialect_name


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
        candidate = (
            select(QueueDeliveryRecord.delivery_id)
            .join(Job, Job.job_id == QueueDeliveryRecord.job_id)
            .where(
                QueueDeliveryRecord.state == "pending",
                QueueDeliveryRecord.available_at <= now,
                QueueDeliveryRecord.target_queue.in_(target_queues),
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
            )
            .order_by(QueueDeliveryRecord.available_at.asc(), QueueDeliveryRecord.created_at.asc())
            .limit(1)
        )
        if db_dialect_name(db) == "postgresql":
            candidate = candidate.with_for_update(skip_locked=True)
        row = db.execute(
            update(QueueDeliveryRecord)
            .where(
                QueueDeliveryRecord.delivery_id == candidate.scalar_subquery(),
                QueueDeliveryRecord.state == "pending",
            )
            .values(
                state="delivered",
                delivery_attempt=QueueDeliveryRecord.delivery_attempt + 1,
                last_delivered_at=now,
                updated_at=now,
            )
            .returning(
                QueueDeliveryRecord.delivery_id,
                QueueDeliveryRecord.job_id,
                QueueDeliveryRecord.target_queue,
                QueueDeliveryRecord.delivery_attempt,
            )
        ).mappings().first()
        if row is None:
            return None
        return QueueDelivery(
            delivery_id=row["delivery_id"],
            job_id=row["job_id"],
            target_queue=row["target_queue"],
            delivery_attempt=row["delivery_attempt"],
        )

    def peek_queue(self, db: Session, *, target_queues: list[str]) -> int:
        return _peek_queued_jobs(db, target_queues=target_queues)

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
        job = db.get(Job, delivery.job_id)
        now = utc_now()
        if job is not None and job.status == JobStatus.QUEUED.value:
            job.updated_at = now
        record.state = "pending"
        record.available_at = now
        record.updated_at = now

    def redrive_stale_deliveries(
        self,
        db: Session,
        *,
        visibility_timeout_seconds: int,
        max_delivery_attempts: int,
    ) -> dict[str, int]:
        now = utc_now()
        stale_cutoff = now.timestamp() - visibility_timeout_seconds
        stale_cutoff_db = datetime.fromtimestamp(stale_cutoff, tz=UTC)
        stale = db.scalars(
            select(QueueDeliveryRecord).where(
                QueueDeliveryRecord.state == "delivered",
                QueueDeliveryRecord.last_delivered_at.is_not(None),
                QueueDeliveryRecord.last_delivered_at <= stale_cutoff_db,
            )
        ).all()
        redriven = 0
        dead_lettered = 0
        for record in stale:
            if record.delivery_attempt >= max_delivery_attempts:
                record.state = "dead_lettered"
                record.dead_lettered_at = now
                record.updated_at = now
                dead_lettered += 1
                job = db.get(Job, record.job_id)
                if job is not None and job.status == JobStatus.QUEUED.value:
                    job.status = JobStatus.FAILED.value
                    job.updated_at = now
                continue
            job = db.get(Job, record.job_id)
            if job is not None and job.status == JobStatus.QUEUED.value:
                job.updated_at = now
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
