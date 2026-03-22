"""Run domain operations."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.enums import ArtifactKind, LeaseStatus, RunStatus
from agp.models import Artifact, JobArtifact, Lease, Run, RunArtifact
from agp.services._helpers import _artifact_store, _new_id
from agp.services.events import _create_event

_TERMINAL_RUN_STATES = frozenset({
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.ABANDONED.value,
})


def _reject_if_terminal(run: Run) -> None:
    if run.status in _TERMINAL_RUN_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"run {run.run_id} is already terminal (status={run.status})",
        )


def _active_lease_for_run(db: Session, run_id: str, lease_id: str) -> Lease:
    lease = db.scalar(
        select(Lease).where(
            Lease.lease_id == lease_id,
            Lease.run_id == run_id,
            Lease.status == LeaseStatus.ACTIVE.value,
        )
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="active lease not found")
    return lease


def _assert_lease_owner(lease: Lease, runtime_id: str, fencing_token: int) -> None:
    if lease.runtime_id != runtime_id:
        raise HTTPException(status_code=409, detail="lease runtime mismatch")
    if lease.fencing_token != fencing_token:
        raise HTTPException(status_code=409, detail="stale fencing token")


def _validate_terminal_artifact_roles(artifacts: list, required_roles: set[str]) -> None:
    seen = {item.role for item in artifacts}
    missing = sorted(required_roles - seen)
    if missing:
        raise HTTPException(status_code=400, detail=f"missing required artifact roles: {', '.join(missing)}")


def _validate_artifact_store_refs(artifacts: list) -> None:
    missing_refs = [item.storage_ref for item in artifacts if not _artifact_store().exists(storage_ref=item.storage_ref)]
    if missing_refs:
        raise HTTPException(status_code=400, detail=f"missing durable artifacts: {', '.join(missing_refs)}")


def _store_terminal_artifacts(
    db: Session,
    *,
    job_id: str,
    run_id: str,
    artifacts: list,
) -> tuple[str | None, str | None]:
    result_artifact_id: str | None = None
    failure_artifact_id: str | None = None
    for item in artifacts:
        artifact_id = _new_id("art")
        artifact = Artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            run_id=run_id,
            kind=item.role,
            content_type=item.content_type,
            storage_ref=item.storage_ref,
            checksum=item.checksum,
            size_bytes=item.size_bytes,
        )
        db.add(artifact)
        db.add(JobArtifact(job_id=job_id, artifact_id=artifact_id, role=item.role))
        db.add(RunArtifact(run_id=run_id, artifact_id=artifact_id, role=item.role))
        _create_event(
            db,
            job_id=job_id,
            run_id=run_id,
            event_type="artifact.created",
            body={"artifact_id": artifact_id, "role": item.role, "storage_ref": item.storage_ref},
        )
        if item.role == ArtifactKind.RESULT.value:
            result_artifact_id = artifact_id
        if item.role == ArtifactKind.FAILURE_EVIDENCE.value:
            failure_artifact_id = artifact_id
    return result_artifact_id, failure_artifact_id
