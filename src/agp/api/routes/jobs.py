"""Job route handlers — thin HTTP layer delegating to services."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.enums import AgentStatus, JobStatus
from agp.models import Event, IdempotencyKey, Job, utc_now
from agp.schemas import HandoffRequest, JobResponse, OkResponse, PagedData, SendMessageRequest
from agp.services._helpers import _require_agent, _require_job
from agp.services.events import _create_event
from agp.services.jobs import (
    _block_job,
    _unblock_job,
    create_and_enqueue_job,
    execute_handoff,
    execute_inline,
)

router = APIRouter()


@router.post("/messages/send")
def send_message(
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    request_hash = str(hash(request.model_dump_json()))
    if idempotency_key is not None:
        existing = db.get(IdempotencyKey, {"idempotency_key": idempotency_key, "endpoint": "/messages/send"})
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
            return existing.response_json

    message, job = create_and_enqueue_job(
        db,
        target_type=request.target.type,
        target_id=request.target.id,
        text=request.message.text,
        metadata=request.message.metadata,
        output_contract=request.message.output_contract,
        conversation_id=request.message.conversation_id,
        reply_to_message_id=request.message.reply_to_message_id,
        timeout_seconds=request.message.timeout_seconds,
        attachments=request.message.attachments,
    )

    # Inline execution path
    if request.target.type == "agent":
        target_agent = _require_agent(db, request.target.id)
        detach_mode = request.detach_policy.get("mode", "auto")
        if detach_mode == "inline" and target_agent.status == AgentStatus.IDLE.value:
            result = execute_inline(db, job=job, agent=target_agent, message=message)
            response = _ok({
                "kind": result.kind,
                "job_id": result.job_id,
                "result_artifact_id": result.result_artifact_id,
                "status": result.status,
            })
            if idempotency_key is not None:
                db.add(IdempotencyKey(
                    idempotency_key=idempotency_key, endpoint="/messages/send",
                    request_hash=request_hash, response_json=response,
                    expires_at=utc_now() + timedelta(days=1),
                ))
            db.commit()
            return response

    # Async path
    response = _ok({
        "kind": "accepted_async",
        "job_id": job.job_id,
        "status": JobStatus.QUEUED.value,
        "message_id": message.message_id,
        "target": request.target.model_dump(),
    })
    if idempotency_key is not None:
        db.add(IdempotencyKey(
            idempotency_key=idempotency_key, endpoint="/messages/send",
            request_hash=request_hash, response_json=response,
            expires_at=utc_now() + timedelta(days=1),
        ))
    db.commit()
    return response


@router.get("/jobs", response_model=OkResponse[PagedData[JobResponse]])
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
        query = query.where((Job.created_at < created_at) | ((Job.created_at == created_at) & (Job.job_id < job_id)))
    jobs = db.scalars(query.order_by(Job.created_at.desc(), Job.job_id.desc()).limit(limit + 1)).all()
    page_items = jobs[:limit]
    next_cursor = None
    if len(jobs) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"created_at": last.created_at.isoformat(), "job_id": last.job_id})
    return _ok(_page(
        [_serialize(j, ("job_id", "message_id", "target_agent_id", "target_queue", "status", "retry_count", "max_retries", "latest_run_id", "result_artifact_id", "output_contract_json", "conversation_id", "timeout_seconds", "deadline_at")) for j in page_items],
        limit=limit, next_cursor=next_cursor,
    ))


@router.get("/jobs/{job_id}", response_model=OkResponse[JobResponse])
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    return _ok(_serialize(job, ("job_id", "message_id", "target_agent_id", "target_queue", "status", "retry_count", "max_retries", "latest_run_id", "result_artifact_id", "output_contract_json", "conversation_id", "timeout_seconds", "deadline_at", "created_at", "updated_at")))


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, db: Session = Depends(get_db), cursor: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200)) -> dict:
    _require_job(db, job_id)
    cursor_payload = _decode_cursor(cursor)
    query = select(Event).where(Event.job_id == job_id)
    if cursor_payload is not None:
        query = query.where(Event.event_seq > int(cursor_payload["event_seq"]))
    events = db.scalars(query.order_by(Event.event_seq).limit(limit + 1)).all()
    page_items = events[:limit]
    next_cursor = _encode_cursor({"event_seq": page_items[-1].event_seq}) if len(events) > limit else None
    return _ok(_page(
        [{"event_id": e.event_id, "event_seq": e.event_seq, "event_type": e.event_type, "body": e.body_json, "created_at": e.created_at.isoformat()} for e in page_items],
        limit=limit, next_cursor=next_cursor,
    ))


@router.post("/jobs/{job_id}/interrupt")
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


@router.post("/jobs/{job_id}/block")
def block_job(job_id: str, reason: str = Query(default="operator_blocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _block_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/unblock")
def unblock_job(job_id: str, reason: str = Query(default="operator_unblocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _unblock_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/handoff")
def handoff_job(job_id: str, request: HandoffRequest, db: Session = Depends(get_db)) -> dict:
    source_job = _require_job(db, job_id)
    result = execute_handoff(db, source_job=source_job, targets=request.targets, message_payload=request.message, artifact_ids=request.artifact_ids)
    return _ok(result)
