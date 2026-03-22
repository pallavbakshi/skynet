"""Run route handlers — thin HTTP layer delegating to services."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agp.api.helpers import _ok, _serialize
from agp.db import get_db
from agp.enums import (
    ArtifactKind,
    HealthStatus,
    JobStatus,
    RunStatus,
    RuntimeStatus,
)
from agp.models import Job, Run, utc_now
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
    _require_agent,
    _require_job,
    _require_runtime,
)
from agp.services.events import _create_event
from agp.services.runs import (
    _active_lease_for_run,
    _assert_lease_owner,
    _reject_if_terminal,
    _validate_artifact_store_refs,
    _validate_terminal_artifact_roles,
    complete_run_service,
    execute_claim,
    fail_run_service,
    resolve_claim_agent,
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

    agent, routing_decision = resolve_claim_agent(
        db, runtime=runtime, agent_id=request.agent_id, capability_id=request.capability_id,
    )
    if agent is None:
        return _ok({"claimed": False, "agent_id": request.agent_id})

    result = execute_claim(
        db, agent=agent, runtime=runtime,
        lease_ttl_seconds=request.lease_ttl_seconds,
        routing_decision=routing_decision,
    )
    if not result.claimed:
        return _ok({"claimed": False, "agent_id": agent.agent_id})

    return _ok({
        "claimed": True,
        "job": _serialize(result.job, ("job_id", "message_id", "target_queue", "status")),
        "message": {
            "message_id": result.message.message_id,
            "target_type": result.message.target_type,
            "target_id": result.message.target_id,
            "text": result.message.text,
            "metadata": result.message.metadata_json,
        },
        "run": _serialize(result.run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status")),
        "lease": _serialize(result.lease, ("lease_id", "run_id", "agent_id", "runtime_id", "fencing_token", "status", "expires_at")),
        "agent_id": agent.agent_id,
        "artifact_upload_policy": {
            "required_roles": ["prompt", "transcript_log", "exec_log", "result"],
            "allow_additional_roles": True,
        },
    })


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
        _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime.runtime_id, event_type="run.running", body={"started_by": runtime.runtime_id})
    lease.expires_at = utc_now() + timedelta(seconds=request.extend_seconds)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime.runtime_id, event_type="lease.heartbeat", body={"lease_id": lease.lease_id, "expires_at": lease.expires_at.isoformat()})
    db.commit()
    job = db.get(Job, run.job_id)
    interrupt_requested = job is not None and job.status == JobStatus.INTERRUPT_REQUESTED.value
    return _ok({"run_id": run_id, "lease_id": lease.lease_id, "status": run.status, "expires_at": lease.expires_at, "interrupt_requested": interrupt_requested})


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
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=request.runtime_id, event_type="run.progress", body={"message": request.message, "details": request.details})
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
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=request.runtime_id, event_type="run.recovering", body={"details": request.details, "expires_at": lease.expires_at.isoformat()})
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
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=request.runtime_id, event_type="run.resumed", body={"details": request.details})
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/cancel", response_model=dict)
def cancel_run(run_id: str, request: CancelRunRequest, db: Session = Depends(get_db)) -> dict:
    from agp.enums import LeaseStatus as LS
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _reject_if_terminal(run)
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    from agp.enums import AgentStatus as AS, JobStatus as JS, RuntimeStatus as RS
    run.status = RunStatus.CANCELLED.value
    run.finished_at = utc_now()
    lease.status = LS.RELEASED.value
    lease.released_at = utc_now()
    job.status = JS.CANCELLED.value
    job.updated_at = utc_now()
    agent.status = AS.IDLE.value if agent.status != AS.TERMINATED.value else agent.status
    runtime.status = RS.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="run.cancelled", body={"reason": request.reason})
    _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status})


@router.post("/runs/{run_id}/complete", response_model=dict)
def complete_run(run_id: str, request: CompleteRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(request.artifacts, {ArtifactKind.PROMPT.value, ArtifactKind.TRANSCRIPT_LOG.value, ArtifactKind.EXEC_LOG.value, ArtifactKind.RESULT.value})
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
    result_artifact_id = complete_run_service(db, run=run, job=job, agent=agent, runtime=runtime, lease=lease, artifacts=request.artifacts, summary=request.summary)
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status, "result_artifact_id": result_artifact_id})


@router.post("/runs/{run_id}/fail", response_model=dict)
def fail_run(run_id: str, request: FailRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(request.artifacts, {ArtifactKind.PROMPT.value, ArtifactKind.TRANSCRIPT_LOG.value, ArtifactKind.EXEC_LOG.value, ArtifactKind.FAILURE_EVIDENCE.value})
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
    fail_run_service(db, run=run, job=job, agent=agent, runtime=runtime, lease=lease, error=request.error, artifacts=request.artifacts, summary=request.summary)
    return _ok({"run_id": run_id, "job_id": job.job_id, "run_status": run.status, "job_status": job.status, "retry_count": job.retry_count})
