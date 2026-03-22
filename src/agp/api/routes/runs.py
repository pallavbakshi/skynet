"""Run route handlers."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from agp.api.helpers import _ok, _serialize
from agp.db import get_db
from agp.enums import (
    AgentStatus,
    ArtifactKind,
    HealthStatus,
    JobStatus,
    LeaseStatus,
    RunStatus,
    RuntimeStatus,
)
from agp.models import Agent, Artifact, CapabilityPool, Job, Lease, Message, Run, Runtime, utc_now
from agp.schemas import (
    CancelRunRequest,
    ClaimRunRequest,
    CompleteRunRequest,
    FailRunRequest,
    HeartbeatRequest,
    ProgressRequest,
    RecoveryRequest,
)
from agp.services._helpers import (
    _capability_queue_for,
    _enqueue_nudge,
    _format_job_nudge,
    _new_id,
    _queue_backend,
    _record_agent_binding,
    _require_agent,
    _require_job,
    _require_runtime,
)
from agp.services.events import _create_event
from agp.services.jobs import _fail_exhausted_queued_jobs
from agp.services.runs import (
    _active_lease_for_run,
    _assert_lease_owner,
    _reject_if_terminal,
    _store_terminal_artifacts,
    _validate_artifact_store_refs,
    _validate_terminal_artifact_roles,
)

router = APIRouter()


@router.post("/runs/claim", response_model=dict)
def claim_run(request: ClaimRunRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, request.runtime_id)
    if runtime.health_status == HealthStatus.UNREACHABLE.value:
        raise HTTPException(status_code=409, detail="runtime is unreachable")
    if runtime.health_status == HealthStatus.DEGRADED.value:
        raise HTTPException(status_code=409, detail="runtime is degraded")
    if runtime.status == RuntimeStatus.DRAINING.value:
        raise HTTPException(status_code=409, detail="runtime is draining")
    if runtime.status == RuntimeStatus.DEGRADED.value:
        raise HTTPException(status_code=409, detail="runtime is degraded")
    routing_decision: dict | None = None
    if request.agent_id is not None:
        agent = _require_agent(db, request.agent_id)
        if agent.status != AgentStatus.IDLE.value:
            return _ok({"claimed": False, "agent_id": agent.agent_id})
        if agent.assigned_runtime_id is not None and agent.assigned_runtime_id != runtime.runtime_id:
            return _ok({"claimed": False, "agent_id": agent.agent_id})
    elif request.capability_id is not None:
        pool = db.get(CapabilityPool, request.capability_id)
        routing_policy = pool.routing_policy if pool else "least_recent"
        candidate_query = (
            select(Agent)
            .outerjoin(Runtime, Agent.assigned_runtime_id == Runtime.runtime_id)
            .where(
                Agent.capability_id == request.capability_id,
                Agent.status == AgentStatus.IDLE.value,
                or_(Agent.assigned_runtime_id.is_(None), Agent.assigned_runtime_id == runtime.runtime_id),
                or_(
                    Agent.assigned_runtime_id.is_(None),
                    Runtime.status.notin_([RuntimeStatus.DRAINING.value, RuntimeStatus.OFFLINE.value]),
                ),
                or_(
                    Agent.assigned_runtime_id.is_(None),
                    Runtime.health_status.notin_([HealthStatus.UNREACHABLE.value]),
                ),
            )
        )
        if routing_policy == "least_recent":
            candidate_query = candidate_query.order_by(
                case(
                    (Runtime.health_status == HealthStatus.HEALTHY.value, 0),
                    (Runtime.health_status == HealthStatus.DEGRADED.value, 1),
                    else_=2,
                ).asc(),
                Agent.last_seen_at.asc().nulls_first(),
                Agent.agent_id.asc(),
            )
        else:
            candidate_query = candidate_query.order_by(Agent.agent_id.asc())
        candidates = db.scalars(candidate_query).all()
        agent = candidates[0] if candidates else None
        if agent is None:
            return _ok({"claimed": False})
        routing_decision = {
            "policy": routing_policy,
            "candidate_count": len(candidates),
            "selected_agent_id": agent.agent_id,
            "candidate_agent_ids": [c.agent_id for c in candidates],
        }
    else:
        raise HTTPException(status_code=400, detail="claim requires agent_id or capability_id")

    capability_queue = _capability_queue_for(db, agent.capability_id)
    target_queues = [f"agent:{agent.agent_id}", capability_queue]
    exhausted_count = _fail_exhausted_queued_jobs(db, target_queues=target_queues)

    delivery = _queue_backend().dequeue_candidate(db, target_queues=target_queues)
    if delivery is None:
        if exhausted_count:
            db.commit()
        return _ok({"claimed": False, "agent_id": agent.agent_id})
    job = db.get(Job, delivery.job_id)
    if job is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise HTTPException(status_code=500, detail=f"job missing for delivery {delivery.job_id}")
    if job.status != JobStatus.QUEUED.value or job.retry_count >= job.max_retries:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        if exhausted_count:
            db.commit()
        return _ok({"claimed": False, "agent_id": agent.agent_id})
    message = db.get(Message, job.message_id)
    if message is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise HTTPException(status_code=500, detail=f"message missing for job {job.job_id}")

    attempt = (db.scalar(select(func.max(Run.attempt)).where(Run.job_id == job.job_id)) or 0) + 1
    run = Run(
        run_id=_new_id("run"),
        job_id=job.job_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        attempt=attempt,
        status=RunStatus.CREATED.value,
    )
    db.add(run)
    db.flush()
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="run.created",
        body={"attempt": attempt},
    )
    run.status = RunStatus.LEASED.value
    lease = Lease(
        lease_id=_new_id("lease"),
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        fencing_token=attempt,
        status=LeaseStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(seconds=request.lease_ttl_seconds),
    )
    db.add(lease)
    job.status = JobStatus.RUNNING.value
    job.latest_run_id = run.run_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.BUSY.value
    _record_agent_binding(db, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, status="active")
    agent.assigned_runtime_id = runtime.runtime_id
    runtime.status = RuntimeStatus.BUSY.value
    runtime.last_heartbeat_at = utc_now()
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="agent.busy",
        body={"run_id": run.run_id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="lease.acquired",
        body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()},
    )
    if routing_decision is not None:
        _create_event(
            db,
            job_id=job.job_id,
            run_id=run.run_id,
            agent_id=agent.agent_id,
            runtime_id=runtime.runtime_id,
            event_type="routing.decision",
            body=routing_decision,
        )
    _queue_backend().ack_claim(db, delivery=delivery, job=job)
    db.commit()
    return _ok(
        {
            "claimed": True,
            "job": _serialize(job, ("job_id", "message_id", "target_queue", "status")),
            "message": {
                "message_id": message.message_id,
                "target_type": message.target_type,
                "target_id": message.target_id,
                "text": message.text,
                "metadata": message.metadata_json,
            },
            "run": _serialize(run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status")),
            "lease": _serialize(lease, ("lease_id", "run_id", "agent_id", "runtime_id", "fencing_token", "status", "expires_at")),
            "agent_id": agent.agent_id,
            "artifact_upload_policy": {
                "required_roles": ["prompt", "transcript_log", "exec_log", "result"],
                "allow_additional_roles": True,
            },
        }
    )


@router.post("/runs/{run_id}/heartbeat", response_model=dict)
def heartbeat_run(run_id: str, request: HeartbeatRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, request.runtime_id)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
        _create_event(
            db,
            job_id=run.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=runtime.runtime_id,
            event_type="run.running",
            body={"started_by": runtime.runtime_id},
        )
    lease.expires_at = utc_now() + timedelta(seconds=request.extend_seconds)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="lease.heartbeat",
        body={"lease_id": lease.lease_id, "expires_at": lease.expires_at.isoformat()},
    )
    db.commit()
    job = db.get(Job, run.job_id)
    interrupt_requested = job is not None and job.status == JobStatus.INTERRUPT_REQUESTED.value
    return _ok({
        "run_id": run_id,
        "lease_id": lease.lease_id,
        "status": run.status,
        "expires_at": lease.expires_at,
        "interrupt_requested": interrupt_requested,
    })


@router.post("/runs/{run_id}/progress", response_model=dict)
def progress_run(run_id: str, request: ProgressRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.progress",
        body={"message": request.message, "details": request.details},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/recovering", response_model=dict)
def recovering_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    runtime = _require_runtime(db, request.runtime_id)
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail=f"run cannot enter recovering from state {run.status}")
    run.status = RunStatus.RECOVERING.value
    lease.expires_at = utc_now() + timedelta(seconds=30)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.recovering",
        body={"details": request.details, "expires_at": lease.expires_at.isoformat()},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/resumed", response_model=dict)
def resumed_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status != RunStatus.RECOVERING.value:
        raise HTTPException(status_code=409, detail=f"run cannot resume from state {run.status}")
    run.status = RunStatus.RUNNING.value
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.resumed",
        body={"details": request.details},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/cancel", response_model=dict)
def cancel_run(run_id: str, request: CancelRunRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _reject_if_terminal(run)
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    run.status = RunStatus.CANCELLED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.CANCELLED.value
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="run.cancelled", body={"reason": request.reason})
    _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status})


@router.post("/runs/{run_id}/complete", response_model=dict)
def complete_run(run_id: str, request: CompleteRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(
        request.artifacts,
        {ArtifactKind.PROMPT.value, ArtifactKind.TRANSCRIPT_LOG.value, ArtifactKind.EXEC_LOG.value, ArtifactKind.RESULT.value},
    )
    _validate_artifact_store_refs(request.artifacts)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _reject_if_terminal(run)
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=request.artifacts)
    run.status = RunStatus.COMPLETED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.COMPLETED.value
    job.result_artifact_id = result_artifact_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(
        db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id,
        event_type="run.completed",
        body={"summary": request.summary, "artifact_ids": [a.artifact_id for a in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
    message = db.get(Message, job.message_id)
    nudge_target = (message.metadata_json or {}).get("nudge_target") if message else None
    if nudge_target:
        _enqueue_nudge(db, target_agent_id=nudge_target, priority=2, source="job_completion", payload=_format_job_nudge(job, "SUCCESS"), job_id=job.job_id)
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status, "result_artifact_id": result_artifact_id})


@router.post("/runs/{run_id}/fail", response_model=dict)
def fail_run(run_id: str, request: FailRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(
        request.artifacts,
        {ArtifactKind.PROMPT.value, ArtifactKind.TRANSCRIPT_LOG.value, ArtifactKind.EXEC_LOG.value, ArtifactKind.FAILURE_EVIDENCE.value},
    )
    _validate_artifact_store_refs(request.artifacts)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _reject_if_terminal(run)
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    _, failure_artifact_id = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=request.artifacts)
    run.status = RunStatus.FAILED.value
    run.finished_at = utc_now()
    run.error_artifact_id = failure_artifact_id
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.FAILED.value
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(
        db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id,
        event_type="run.failed",
        body={"error": request.error, "summary": request.summary, "artifact_ids": [a.artifact_id for a in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.failed", body={"status": job.status})
    message = db.get(Message, job.message_id)
    nudge_target = (message.metadata_json or {}).get("nudge_target") if message else None
    if nudge_target:
        _enqueue_nudge(db, target_agent_id=nudge_target, priority=2, source="job_completion", payload=_format_job_nudge(job, "FAILED"), job_id=job.job_id)
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "run_status": run.status, "job_status": job.status, "retry_count": job.retry_count})
