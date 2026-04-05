"""Artifact route handlers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _ok, _serialize, _serialize_artifact_with_role
from agp.db import get_db
from agp.models import Artifact, JobArtifact, Run, RunArtifact
from agp.schemas import ArtifactUploadRequest
from agp.services._helpers import _artifact_store, _require_job

router = APIRouter()


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, db: Session = Depends(get_db)) -> dict:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {artifact_id}")
    return _ok(
        _serialize(artifact, ("artifact_id", "job_id", "run_id", "kind", "content_type", "storage_ref", "checksum", "size_bytes", "created_at"))
    )


@router.get("/jobs/{job_id}/artifacts")
def list_job_artifacts(job_id: str, role: str | None = Query(default=None), db: Session = Depends(get_db)) -> dict:
    _require_job(db, job_id)
    rows = db.execute(
        select(JobArtifact, Artifact)
        .join(Artifact, Artifact.artifact_id == JobArtifact.artifact_id)
        .where(JobArtifact.job_id == job_id)
        .order_by(JobArtifact.role.asc(), Artifact.created_at.asc())
    ).all()
    items = [
        _serialize_artifact_with_role(artifact, link.role)
        for link, artifact in rows
        if role is None or link.role == role
    ]
    return _ok({"items": items, "job_id": job_id, "role": role})


@router.get("/runs/{run_id}/artifacts")
def list_run_artifacts(run_id: str, role: str | None = Query(default=None), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    rows = db.execute(
        select(RunArtifact, Artifact)
        .join(Artifact, Artifact.artifact_id == RunArtifact.artifact_id)
        .where(RunArtifact.run_id == run_id)
        .order_by(RunArtifact.role.asc(), Artifact.created_at.asc())
    ).all()
    items = [
        _serialize_artifact_with_role(artifact, link.role)
        for link, artifact in rows
        if role is None or link.role == role
    ]
    return _ok({"items": items, "run_id": run_id, "role": role})


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(
    artifact_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {artifact_id}")
    try:
        content = _artifact_store().read_text(storage_ref=artifact.storage_ref)
    except Exception:
        content = None
    payload: dict = {
        "artifact_id": artifact.artifact_id,
        "storage_ref": artifact.storage_ref,
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
    }
    if content is not None:
        total_length = len(content)
        payload["total_length"] = total_length
        if offset > 0 or limit > 0:
            end = offset + limit if limit > 0 else total_length
            payload["content"] = content[offset:end]
            payload["offset"] = offset
            payload["limit"] = limit
            payload["has_more"] = end < total_length
        else:
            payload["content"] = content
            payload["has_more"] = False
    return _ok(payload)


@router.post("/artifacts/upload")
def upload_artifact(request: ArtifactUploadRequest, db: Session = Depends(get_db)) -> dict:
    if not request.content_type.strip():
        raise HTTPException(status_code=400, detail="content_type must not be empty")
    store = _artifact_store()
    stored = store.write_text(
        namespace=request.namespace,
        job_id=request.job_id,
        name=request.name,
        content=request.content,
        role=request.role,
        content_type=request.content_type,
    )
    result = {
        "storage_ref": stored.storage_ref,
        "checksum": stored.checksum,
        "size_bytes": stored.size_bytes,
        "role": stored.role,
        "content_type": stored.content_type,
    }
    if request.register_artifact:
        from agp.services._helpers import _new_id

        artifact_id = _new_id("art")
        db.add(Artifact(
            artifact_id=artifact_id,
            job_id=request.job_id,
            run_id=None,
            kind=request.role,
            content_type=stored.content_type,
            storage_ref=stored.storage_ref,
            checksum=stored.checksum,
            size_bytes=stored.size_bytes,
        ))
        db.add(JobArtifact(job_id=request.job_id, artifact_id=artifact_id, role=request.role))
        db.commit()
        result["artifact_id"] = artifact_id
    return _ok(result)
