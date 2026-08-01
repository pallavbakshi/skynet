"""Operational maintenance — log pruning, queue reconstruction, orphan detection."""

from __future__ import annotations

from sqlalchemy import delete, select

from agp.config import settings
from agp.db import SessionLocal
from agp.enums import JobStatus
from agp.logs import prune_rotated_jsonl_family
from agp.models import (
    Artifact,
    HandoffArtifact,
    Job,
    JobArtifact,
    QueueDeliveryRecord,
    RunArtifact,
)
from agp.queue_backend import get_queue_backend


def reconstruct_queue_from_state() -> dict:
    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        jobs = session.scalars(
            select(Job).where(
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
            )
        ).all()
        reconstructed = 0
        for job in jobs:
            session.execute(
                delete(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)
            )
            backend.enqueue_job(session, job=job)
            reconstructed += 1
        session.commit()
        return {
            "queue_backend": settings.queue_backend,
            "reconstructed_jobs": reconstructed,
        }
    finally:
        session.close()


def prune_observability_logs() -> dict:
    control_plane = prune_rotated_jsonl_family(
        settings.log_root / "control-plane.jsonl",
        retention_days=settings.observability_control_plane_log_retention_days,
    )
    runtime_deleted = 0
    runtime_kept = 0
    runtime_families = {
        settings.log_root / f"{path.stem.split('.', 1)[0]}.jsonl"
        for path in settings.log_root.glob("runtime-*.jsonl")
    }
    for path in runtime_families:
        outcome = prune_rotated_jsonl_family(
            path,
            retention_days=settings.observability_runtime_log_retention_days,
        )
        runtime_deleted += outcome["deleted"]
        runtime_kept += outcome["kept"]
    return {
        "control_plane": control_plane,
        "runtime_logs": {"deleted": runtime_deleted, "kept": runtime_kept},
        "retention_days": {
            "control_plane": settings.observability_control_plane_log_retention_days,
            "runtime": settings.observability_runtime_log_retention_days,
        },
    }


def detect_orphan_artifacts(*, limit: int | None = None) -> dict:
    """Find artifact records not referenced by any job, run, or handoff link.

    Orphans are artifacts in the ``artifacts`` table with no corresponding
    row in ``job_artifacts``, ``run_artifacts``, or ``handoff_artifacts``.
    """
    from sqlalchemy import exists

    session = SessionLocal()
    try:
        is_referenced = (
            exists().where(JobArtifact.artifact_id == Artifact.artifact_id)
            | exists().where(RunArtifact.artifact_id == Artifact.artifact_id)
            | exists().where(HandoffArtifact.artifact_id == Artifact.artifact_id)
        )
        query = (
            select(Artifact.artifact_id, Artifact.job_id, Artifact.run_id, Artifact.kind, Artifact.created_at)
            .where(~is_referenced)
            .order_by(Artifact.created_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        rows = session.execute(query).all()
        orphans = [
            {
                "artifact_id": r.artifact_id,
                "job_id": r.job_id,
                "run_id": r.run_id,
                "kind": r.kind,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"orphan_count": len(orphans), "orphans": orphans}
    finally:
        session.close()
