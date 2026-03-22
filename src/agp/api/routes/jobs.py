"""Job route handlers."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.enums import AgentStatus, JobStatus
from agp.models import (
    Artifact,
    Handoff,
    HandoffArtifact,
    HandoffJob,
    Job,
    JobArtifact,
    Message,
    utc_now,
)
from agp.schemas import HandoffRequest, SendMessageRequest
from agp.services._helpers import (
    _capability_queue_for,
    _ensure_inline_runtime,
    _enqueue_nudge,
    _format_job_nudge,
    _new_id,
    _queue_backend,
    _queue_for_target,
    _record_agent_binding,
    _require_agent,
    _require_capability,
    _require_job,
    _write_control_plane_artifact,
)
from agp.services.events import _create_event
from agp.services.jobs import _block_job, _fail_exhausted_queued_jobs, _handoff_ancestor_job_ids, _unblock_job
from agp.services.runs import _store_terminal_artifacts

from fastapi import Header
from agp.enums import ArtifactKind, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import Lease, Run

router = APIRouter()


@router.post("/messages/send", response_model=dict)
def send_message(
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    from agp.models import IdempotencyKey as IdempotencyKeyModel

    request_hash = str(hash(request.model_dump_json()))
    if idempotency_key is not None:
        existing = db.get(IdempotencyKeyModel, {"idempotency_key": idempotency_key, "endpoint": "/messages/send"})
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
            return existing.response_json

    if request.target.type == "agent":
        target_agent = _require_agent(db, request.target.id)
        if target_agent.status == AgentStatus.TERMINATED.value:
            raise HTTPException(status_code=409, detail=f"agent is terminated: {request.target.id}")
        if target_agent.status == AgentStatus.DRAINING.value:
            raise HTTPException(status_code=409, detail=f"agent is draining: {request.target.id}")
    elif request.target.type == "capability":
        _require_capability(db, request.target.id)
        target_agent = None
    else:
        raise HTTPException(status_code=400, detail="target.type must be agent or capability")

    message = Message(
        message_id=_new_id("msg"),
        target_type=request.target.type,
        target_id=request.target.id,
        text=request.message.text,
        metadata_json=request.message.metadata,
    )
    db.add(message)
    db.flush()

    job = Job(
        job_id=_new_id("job"),
        message_id=message.message_id,
        target_agent_id=request.target.id if request.target.type == "agent" else None,
        target_queue=(
            _queue_for_target(request.target.type, request.target.id)
            if request.target.type == "agent"
            else _capability_queue_for(db, request.target.id)
        ),
        status=JobStatus.QUEUED.value,
        max_retries=3,
    )
    db.add(job)
    db.flush()

    _create_event(
        db,
        job_id=job.job_id,
        event_type="job.accepted",
        body={"message_id": message.message_id, "target_type": request.target.type, "target_id": request.target.id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        event_type="job.queued",
        body={"target_queue": job.target_queue},
    )
    _queue_backend().enqueue_job(db, job=job)

    if request.target.type == "agent" and target_agent is not None:
        detach_mode = request.detach_policy.get("mode", "auto")
        if detach_mode == "inline" and target_agent.status == AgentStatus.IDLE.value:
            runtime = _ensure_inline_runtime(db)
            attempt = 1
            run = Run(
                run_id=_new_id("run"),
                job_id=job.job_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                attempt=attempt,
                status=RunStatus.RUNNING.value,
                started_at=utc_now(),
            )
            db.add(run)
            db.flush()
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.created",
                body={"attempt": attempt},
            )
            lease = Lease(
                lease_id=_new_id("lease"),
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                fencing_token=attempt,
                status=LeaseStatus.ACTIVE.value,
                expires_at=utc_now() + timedelta(seconds=30),
            )
            db.add(lease)
            job.status = JobStatus.RUNNING.value
            job.latest_run_id = run.run_id
            job.updated_at = utc_now()
            target_agent.status = AgentStatus.BUSY.value
            _record_agent_binding(db, agent_id=target_agent.agent_id, runtime_id=runtime.runtime_id, status="active")
            target_agent.assigned_runtime_id = runtime.runtime_id
            runtime.status = RuntimeStatus.BUSY.value
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="lease.acquired",
                body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()},
            )
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.running",
                body={"started_by": runtime.runtime_id},
            )
            artifacts = [
                _write_control_plane_artifact(job_id=job.job_id, name="prompt.txt", content=request.message.text),
                _write_control_plane_artifact(job_id=job.job_id, name="transcript.txt", content=f"inline\nmessage={request.message.text}\n"),
                _write_control_plane_artifact(job_id=job.job_id, name="exec.txt", content="inline-exec\n"),
                _write_control_plane_artifact(job_id=job.job_id, name="result.txt", content=f"inline result for {request.message.text}\n"),
            ]
            result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=artifacts)
            run.status = RunStatus.COMPLETED.value
            run.finished_at = utc_now()
            lease.status = LeaseStatus.RELEASED.value
            lease.released_at = utc_now()
            job.status = JobStatus.COMPLETED.value
            job.result_artifact_id = result_artifact_id
            job.updated_at = utc_now()
            target_agent.status = AgentStatus.IDLE.value
            runtime.status = RuntimeStatus.IDLE.value
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="lease.released",
                body={"lease_id": lease.lease_id},
            )
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.completed",
                body={"artifact_ids": [artifact.artifact_id for artifact in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
            )
            _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
            response = _ok(
                {
                    "kind": "inline_result",
                    "job_id": job.job_id,
                    "result_artifact_id": result_artifact_id,
                    "status": JobStatus.COMPLETED.value,
                }
            )
            if idempotency_key is not None:
                db.add(
                    IdempotencyKeyModel(
                        idempotency_key=idempotency_key,
                        endpoint="/messages/send",
                        request_hash=request_hash,
                        response_json=response,
                        expires_at=utc_now() + timedelta(days=1),
                    )
                )
            db.commit()
            return response

    response = _ok(
        {
            "kind": "accepted_async",
            "job_id": job.job_id,
            "status": JobStatus.QUEUED.value,
            "message_id": message.message_id,
            "target": request.target.model_dump(),
        }
    )
    if idempotency_key is not None:
        from agp.models import IdempotencyKey as IdempotencyKeyModel
        db.add(
            IdempotencyKeyModel(
                idempotency_key=idempotency_key,
                endpoint="/messages/send",
                request_hash=request_hash,
                response_json=response,
                expires_at=utc_now() + timedelta(days=1),
            )
        )
    db.commit()
    return response


@router.get("/jobs", response_model=dict)
def list_jobs(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    target_agent_id: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Job)
    if status is not None:
        query = query.where(Job.status == status)
    if target_agent_id is not None:
        query = query.where(Job.target_agent_id == target_agent_id)
    if created_after is not None:
        query = query.where(Job.created_at >= created_after)
    if cursor_payload is not None:
        created_at = datetime.fromisoformat(str(cursor_payload["created_at"]))
        job_id = str(cursor_payload["job_id"])
        query = query.where(
            (Job.created_at < created_at) | ((Job.created_at == created_at) & (Job.job_id < job_id))
        )
    jobs = db.scalars(query.order_by(Job.created_at.desc(), Job.job_id.desc()).limit(limit + 1)).all()
    page_items = jobs[:limit]
    next_cursor = None
    if len(jobs) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"created_at": last.created_at.isoformat(), "job_id": last.job_id})
    return _ok(
        _page(
            [
                _serialize(
                    job,
                    (
                        "job_id",
                        "message_id",
                        "target_agent_id",
                        "target_queue",
                        "status",
                        "retry_count",
                        "max_retries",
                        "latest_run_id",
                        "result_artifact_id",
                    ),
                )
                for job in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.get("/jobs/{job_id}", response_model=dict)
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    return _ok(
        _serialize(
            job,
            (
                "job_id",
                "message_id",
                "target_agent_id",
                "target_queue",
                "status",
                "retry_count",
                "max_retries",
                "latest_run_id",
                "result_artifact_id",
                "created_at",
                "updated_at",
            ),
        )
    )


@router.get("/jobs/{job_id}/events", response_model=dict)
def get_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from agp.models import Event

    _require_job(db, job_id)
    cursor_payload = _decode_cursor(cursor)
    query = select(Event).where(Event.job_id == job_id)
    if cursor_payload is not None:
        query = query.where(Event.event_seq > int(cursor_payload["event_seq"]))
    events = db.scalars(query.order_by(Event.event_seq).limit(limit + 1)).all()
    page_items = events[:limit]
    next_cursor = None
    if len(events) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"event_seq": last.event_seq})
    return _ok(
        _page(
            [
                {
                    "event_id": event.event_id,
                    "event_seq": event.event_seq,
                    "event_type": event.event_type,
                    "body": event.body_json,
                    "created_at": event.created_at.isoformat(),
                }
                for event in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.post("/jobs/{job_id}/interrupt", response_model=dict)
def interrupt_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    if job.status == JobStatus.QUEUED.value:
        job.status = JobStatus.CANCELLED.value
        event_type = "job.cancelled"
    elif job.status == JobStatus.RUNNING.value:
        job.status = JobStatus.INTERRUPT_REQUESTED.value
        event_type = "job.interrupt_requested"
    else:
        raise HTTPException(status_code=409, detail=f"job cannot be interrupted from state {job.status}")
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type=event_type, body={"status": job.status})
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status})


@router.post("/jobs/{job_id}/block", response_model=dict)
def block_job(job_id: str, reason: str = Query(default="operator_blocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _block_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/unblock", response_model=dict)
def unblock_job(job_id: str, reason: str = Query(default="operator_unblocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _unblock_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/handoff", response_model=dict)
def handoff_job(job_id: str, request: HandoffRequest, db: Session = Depends(get_db)) -> dict:
    source_job = _require_job(db, job_id)
    ancestor_ids = _handoff_ancestor_job_ids(db, source_job.job_id)
    if ancestor_ids:
        for target in request.targets:
            if target.type == "agent":
                agent_ancestor_jobs = db.scalars(
                    select(Job.job_id).where(
                        Job.target_agent_id == target.id,
                        Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value]),
                        Job.job_id.in_(ancestor_ids),
                    )
                ).all()
                if agent_ancestor_jobs:
                    raise HTTPException(
                        status_code=409,
                        detail=f"handoff cycle detected: agent {target.id} has ancestor job {agent_ancestor_jobs[0]} in its chain",
                    )
    handoff = Handoff(handoff_id=_new_id("hnd"), source_job_id=source_job.job_id)
    db.add(handoff)
    db.flush()
    child_job_ids: list[str] = []
    for target in request.targets:
        if target.type == "agent":
            _require_agent(db, target.id)
        else:
            _require_capability(db, target.id)
        message = Message(
            message_id=_new_id("msg"),
            target_type=target.type,
            target_id=target.id,
            text=request.message.text,
            metadata_json=request.message.metadata,
        )
        db.add(message)
        db.flush()
        child = Job(
            job_id=_new_id("job"),
            message_id=message.message_id,
            target_agent_id=target.id if target.type == "agent" else None,
            target_queue=(
                _queue_for_target(target.type, target.id)
                if target.type == "agent"
                else _capability_queue_for(db, target.id)
            ),
            status=JobStatus.QUEUED.value,
            max_retries=3,
        )
        db.add(child)
        db.flush()
        db.add(HandoffJob(handoff_id=handoff.handoff_id, job_id=child.job_id))
        child_job_ids.append(child.job_id)
        _queue_backend().enqueue_job(db, job=child)
    validated_artifact_ids: list[str] = []
    for artifact_id in request.artifact_ids:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=400, detail=f"handoff artifact not found: {artifact_id}")
        if artifact.job_id != source_job.job_id:
            raise HTTPException(
                status_code=400,
                detail=f"artifact {artifact_id} does not belong to source job {source_job.job_id}",
            )
        db.add(HandoffArtifact(handoff_id=handoff.handoff_id, artifact_id=artifact_id))
        validated_artifact_ids.append(artifact_id)
    for child_job_id in child_job_ids:
        for artifact_id in validated_artifact_ids:
            artifact = db.get(Artifact, artifact_id)
            if artifact is not None:
                db.add(JobArtifact(job_id=child_job_id, artifact_id=artifact_id, role=artifact.kind))
    _create_event(
        db,
        job_id=source_job.job_id,
        event_type="handoff.created",
        body={
            "handoff_id": handoff.handoff_id,
            "source_job_id": source_job.job_id,
            "source_artifact_ids": request.artifact_ids,
            "created_job_ids": child_job_ids,
        },
        related_jobs=[(source_job.job_id, "source"), *[(child_id, "child") for child_id in child_job_ids]],
    )
    db.commit()
    return _ok({"handoff_id": handoff.handoff_id, "source_job_id": job_id, "child_job_ids": child_job_ids})
