"""Run route handlers — thin HTTP layer delegating to services."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agp.api.helpers import _ok, _serialize
from agp.db import get_db
from agp.enums import ArtifactKind, HealthStatus, RuntimeStatus
from agp.models import Agent, Run, utc_now
from agp.schemas import (
    CancelRunRequest,
    ClaimRunRequest,
    CompleteRunRequest,
    FailRunRequest,
    HeartbeatRequest,
    OkResponse,
    ProgressRequest,
    RecoveryRequest,
)
from agp.services._helpers import _require_agent, _require_job, _require_runtime
from agp.services.exceptions import BadRequestError
from agp.services.runs import (
    _active_lease_for_run,
    _assert_lease_owner,
    _reject_if_terminal,
    _validate_artifact_store_refs,
    _validate_terminal_artifact_roles,
    cancel_run_service,
    complete_run_service,
    execute_claim,
    fail_run_service,
    heartbeat_run_service,
    progress_run_service,
    recovering_run_service,
    resolve_claim_agent,
    resumed_run_service,
    validate_output_contract_completion,
)

router = APIRouter()


@router.post("/runs/claim")
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

    # Idle polling is still a liveness signal. Refresh runtime freshness
    # before we try to claim work so the sweeper does not mark an active
    # poller stale merely because it is between jobs.
    now = utc_now()
    runtime.last_seen_at = now
    runtime.last_heartbeat_at = now
    runtime.updated_at = now

    agent, routing_decision = resolve_claim_agent(db, runtime=runtime, agent_id=request.agent_id, capability=request.capability)
    if agent is None:
        db.commit()
        return _ok({"claimed": False, "agent_id": request.agent_id})

    result = execute_claim(db, agent=agent, runtime=runtime, lease_ttl_seconds=request.lease_ttl_seconds, routing_decision=routing_decision)
    if not result.claimed:
        db.commit()
        return _ok({"claimed": False, "agent_id": agent.agent_id})

    return _ok({
        "claimed": True,
        "job": _serialize(result.job, ("job_id", "message_id", "target_queue", "status", "output_contract_json")),
        "message": {"message_id": result.message.message_id, "target_type": result.message.target_type, "target_id": result.message.target_id, "text": result.message.text, "metadata": result.message.metadata_json},
        "run": _serialize(result.run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status")),
        "lease": _serialize(result.lease, ("lease_id", "run_id", "agent_id", "runtime_id", "fencing_token", "status", "expires_at")),
        "agent_id": agent.agent_id,
        "artifact_upload_policy": {"required_roles": ["prompt", "transcript_log", "exec_log", "result"], "allow_additional_roles": True},
    })


@router.post("/runs/{run_id}/heartbeat")
def heartbeat_run(run_id: str, request: HeartbeatRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, request.runtime_id)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return _ok(heartbeat_run_service(db, run=run, lease=lease, runtime=runtime, extend_seconds=request.extend_seconds))


@router.post("/runs/{run_id}/progress")
def progress_run(run_id: str, request: ProgressRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return _ok(progress_run_service(db, run=run, runtime_id=request.runtime_id, message=request.message, details=request.details))


@router.post("/runs/{run_id}/recovering")
def recovering_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    runtime = _require_runtime(db, request.runtime_id)
    return _ok(recovering_run_service(db, run=run, lease=lease, runtime=runtime, details=request.details))


@router.post("/runs/{run_id}/resumed")
def resumed_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return _ok(resumed_run_service(db, run=run, runtime_id=request.runtime_id, details=request.details))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: CancelRunRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    _reject_if_terminal(run)
    job = _require_job(db, run.job_id)
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    runtime = _require_runtime(db, run.runtime_id)
    return _ok(cancel_run_service(db, run=run, job=job, agent=agent, runtime=runtime, lease=lease, reason=request.reason))


@router.post("/runs/{run_id}/complete")
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
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    runtime = _require_runtime(db, run.runtime_id)
    try:
        validate_output_contract_completion(job=job, artifacts=request.artifacts)
    except BadRequestError as exc:
        failure_artifacts = [
            *request.artifacts,
            SimpleNamespace(
                role=ArtifactKind.FAILURE_EVIDENCE.value,
                storage_ref=f"inline://validation-error/{run_id}",
                content_type="text/plain",
                checksum="",
                size_bytes=0,
            ),
        ]
        fail_run_service(
            db,
            run=run,
            job=job,
            agent=agent,
            runtime=runtime,
            lease=lease,
            error=exc.detail,
            artifacts=failure_artifacts,
            summary={"validation_error": exc.detail},
        )
        raise
    result_artifact_id = complete_run_service(db, run=run, job=job, agent=agent, runtime=runtime, lease=lease, artifacts=request.artifacts, summary=request.summary)
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status, "result_artifact_id": result_artifact_id})


@router.post("/runs/{run_id}/fail")
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
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    runtime = _require_runtime(db, run.runtime_id)
    fail_run_service(db, run=run, job=job, agent=agent, runtime=runtime, lease=lease, error=request.error, artifacts=request.artifacts, summary=request.summary)
    return _ok({"run_id": run_id, "job_id": job.job_id, "run_status": run.status, "job_status": job.status, "retry_count": job.retry_count})
