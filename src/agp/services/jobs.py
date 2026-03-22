"""Job domain operations."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.enums import JobStatus
from agp.models import Handoff, HandoffJob, Job, utc_now
from agp.services._helpers import _require_job
from agp.services.events import _create_event


def _block_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.QUEUED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be blocked from state {job.status}")
    job.status = JobStatus.BLOCKED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.blocked", body={"reason": reason})


def _unblock_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.BLOCKED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be unblocked from state {job.status}")
    job.status = JobStatus.QUEUED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.queued", body={"target_queue": job.target_queue, "reason": reason})


def _fail_exhausted_queued_jobs(db: Session, *, target_queues: list[str]) -> int:
    jobs = db.scalars(
        select(Job).where(
            Job.status == JobStatus.QUEUED.value,
            Job.retry_count >= Job.max_retries,
            Job.target_queue.in_(target_queues),
        )
    ).all()
    for job in jobs:
        job.status = JobStatus.FAILED.value
        job.updated_at = utc_now()
        _create_event(
            db,
            job_id=job.job_id,
            event_type="job.failed",
            body={"reason": "retry_budget_exhausted", "retry_count": job.retry_count},
        )
    return len(jobs)


def _handoff_ancestor_job_ids(db: Session, job_id: str, max_depth: int = 50) -> set[str]:
    """Walk the handoff chain upward to collect all ancestor job IDs."""
    ancestors: set[str] = set()
    current = job_id
    for _ in range(max_depth):
        link = db.scalar(
            select(HandoffJob.handoff_id).where(HandoffJob.job_id == current)
        )
        if link is None:
            break
        parent = db.scalar(
            select(Handoff.source_job_id).where(Handoff.handoff_id == link)
        )
        if parent is None or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    return ancestors
