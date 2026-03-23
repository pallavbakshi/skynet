"""Run domain operations — claim, complete, fail, and supporting logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from agp.enums import (
    AgentStatus,
    ArtifactKind,
    HealthStatus,
    JobStatus,
    LeaseStatus,
    RunStatus,
    RuntimeStatus,
)
from agp.models import (
    Agent,
    Artifact,
    CapabilityPool,
    Job,
    JobArtifact,
    Lease,
    Message,
    Run,
    RunArtifact,
    Runtime,
    utc_now,
)
from agp.services._helpers import (
    _artifact_store,
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
from agp.services.exceptions import BadRequestError, ConflictError, InternalError
from agp.services.jobs import _fail_exhausted_queued_jobs

_TERMINAL_RUN_STATES = frozenset({
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.ABANDONED.value,
})


def _reject_if_terminal(run: Run) -> None:
    if run.status in _TERMINAL_RUN_STATES:
        raise ConflictError(f"run {run.run_id} is already terminal (status={run.status})")


def _active_lease_for_run(db: Session, run_id: str, lease_id: str) -> Lease:
    lease = db.scalar(
        select(Lease).where(
            Lease.lease_id == lease_id,
            Lease.run_id == run_id,
            Lease.status == LeaseStatus.ACTIVE.value,
        )
    )
    if lease is None:
        raise ConflictError("active lease not found")
    return lease


def _assert_lease_owner(lease: Lease, runtime_id: str, fencing_token: int) -> None:
    if lease.runtime_id != runtime_id:
        raise ConflictError("lease runtime mismatch")
    if lease.fencing_token != fencing_token:
        raise ConflictError("stale fencing token")


def _validate_terminal_artifact_roles(artifacts: list, required_roles: set[str]) -> None:
    seen = {item.role for item in artifacts}
    missing = sorted(required_roles - seen)
    if missing:
        raise BadRequestError(f"missing required artifact roles: {', '.join(missing)}")


def _validate_artifact_store_refs(artifacts: list) -> None:
    missing_refs = [item.storage_ref for item in artifacts if not _artifact_store().exists(storage_ref=item.storage_ref)]
    if missing_refs:
        raise BadRequestError(f"missing durable artifacts: {', '.join(missing_refs)}")


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


# ── Protocol orchestration: claim, complete, fail ────────────────────


@dataclass
class ClaimResult:
    """Result of a claim attempt."""
    claimed: bool
    job: Job | None = None
    message: Message | None = None
    run: Run | None = None
    lease: Lease | None = None
    agent: Agent | None = None
    routing_decision: dict | None = None


def resolve_claim_agent(
    db: Session,
    *,
    runtime: Runtime,
    agent_id: str | None,
    capability_id: str | None,
) -> tuple[Agent | None, dict | None]:
    """Resolve the target agent for a claim — by direct ID or capability routing.

    Returns (agent, routing_decision) or (None, None) if no eligible agent.
    """
    if agent_id is not None:
        agent = _require_agent(db, agent_id)
        if agent.status != AgentStatus.IDLE.value:
            return None, None
        if agent.assigned_runtime_id is not None and agent.assigned_runtime_id != runtime.runtime_id:
            return None, None
        return agent, None

    if capability_id is not None:
        pool = db.get(CapabilityPool, capability_id)
        routing_policy = pool.routing_policy if pool else "least_recent"
        candidate_query = (
            select(Agent)
            .outerjoin(Runtime, Agent.assigned_runtime_id == Runtime.runtime_id)
            .where(
                Agent.capability_id == capability_id,
                Agent.status == AgentStatus.IDLE.value,
                or_(Agent.assigned_runtime_id.is_(None), Agent.assigned_runtime_id == runtime.runtime_id),
                or_(Agent.assigned_runtime_id.is_(None), Runtime.status.notin_([RuntimeStatus.DRAINING.value, RuntimeStatus.OFFLINE.value])),
                or_(Agent.assigned_runtime_id.is_(None), Runtime.health_status.notin_([HealthStatus.UNREACHABLE.value])),
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
        if not candidates:
            return None, None
        agent = candidates[0]
        routing_decision = {
            "policy": routing_policy,
            "candidate_count": len(candidates),
            "selected_agent_id": agent.agent_id,
            "candidate_agent_ids": [c.agent_id for c in candidates],
        }
        return agent, routing_decision

    raise BadRequestError("claim requires agent_id or capability_id")


def execute_claim(
    db: Session,
    *,
    agent: Agent,
    runtime: Runtime,
    lease_ttl_seconds: int,
    routing_decision: dict | None = None,
) -> ClaimResult:
    """Execute the claim protocol: dequeue → create run/lease → transition states.

    Owns all ORM mutations for the claim path. Returns a ClaimResult with
    the created entities, or claimed=False if no work available.
    """
    capability_queue = _capability_queue_for(db, agent.capability_id)
    target_queues = [f"agent:{agent.agent_id}", capability_queue]
    exhausted_count = _fail_exhausted_queued_jobs(db, target_queues=target_queues)

    delivery = _queue_backend().dequeue_candidate(db, target_queues=target_queues)
    if delivery is None:
        if exhausted_count:
            db.commit()
        return ClaimResult(claimed=False, agent=agent)
    job = db.get(Job, delivery.job_id)
    if job is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise InternalError(f"job missing for delivery {delivery.job_id}")
    if job.status != JobStatus.QUEUED.value or job.retry_count >= job.max_retries:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        if exhausted_count:
            db.commit()
        return ClaimResult(claimed=False, agent=agent)
    message = db.get(Message, job.message_id)
    if message is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise InternalError(f"message missing for job {job.job_id}")

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
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="run.created", body={"attempt": attempt})
    run.status = RunStatus.LEASED.value
    lease = Lease(
        lease_id=_new_id("lease"),
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        fencing_token=attempt,
        status=LeaseStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(seconds=lease_ttl_seconds),
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
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="agent.busy", body={"run_id": run.run_id})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="lease.acquired", body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()})
    if routing_decision is not None:
        _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="routing.decision", body=routing_decision)
    _queue_backend().ack_claim(db, delivery=delivery, job=job)
    db.commit()
    return ClaimResult(claimed=True, job=job, message=message, run=run, lease=lease, agent=agent, routing_decision=routing_decision)


def complete_run_service(
    db: Session,
    *,
    run: Run,
    job: Job,
    agent: Agent,
    runtime: Runtime,
    lease: Lease,
    artifacts: list,
    summary: dict[str, Any],
) -> str | None:
    """Execute run completion protocol — artifacts, state transitions, events, nudge.

    Returns the result_artifact_id.
    """
    result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=artifacts)
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
        body={"summary": summary, "artifact_ids": [a.artifact_id for a in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
    message = db.get(Message, job.message_id)
    nudge_target = (message.metadata_json or {}).get("nudge_target") if message else None
    if nudge_target:
        _enqueue_nudge(db, target_agent_id=nudge_target, priority=2, source="job_completion", payload=_format_job_nudge(job, "SUCCESS"), job_id=job.job_id)
    db.commit()
    return result_artifact_id


def fail_run_service(
    db: Session,
    *,
    run: Run,
    job: Job,
    agent: Agent,
    runtime: Runtime,
    lease: Lease,
    error: str,
    artifacts: list,
    summary: dict[str, Any],
) -> str | None:
    """Execute run failure protocol — artifacts, state transitions, events, nudge.

    Returns the failure_artifact_id.
    """
    _, failure_artifact_id = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=artifacts)
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
        body={"error": error, "summary": summary, "artifact_ids": [a.artifact_id for a in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.failed", body={"status": job.status})
    message = db.get(Message, job.message_id)
    nudge_target = (message.metadata_json or {}).get("nudge_target") if message else None
    if nudge_target:
        _enqueue_nudge(db, target_agent_id=nudge_target, priority=2, source="job_completion", payload=_format_job_nudge(job, "FAILED"), job_id=job.job_id)
    db.commit()
    return failure_artifact_id


def heartbeat_run_service(
    db: Session, *, run: Run, lease: Lease, runtime: Runtime, extend_seconds: int,
) -> dict:
    """Heartbeat a run — extend lease, promote leased→running, record event."""
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
        _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime.runtime_id, event_type="run.running", body={"started_by": runtime.runtime_id})
    lease.expires_at = utc_now() + timedelta(seconds=extend_seconds)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime.runtime_id, event_type="lease.heartbeat", body={"lease_id": lease.lease_id, "expires_at": lease.expires_at.isoformat()})
    db.commit()
    job = db.get(Job, run.job_id)
    return {
        "run_id": run.run_id,
        "lease_id": lease.lease_id,
        "status": run.status,
        "expires_at": lease.expires_at,
        "interrupt_requested": job is not None and job.status == JobStatus.INTERRUPT_REQUESTED.value,
    }


def progress_run_service(
    db: Session, *, run: Run, runtime_id: str, message: str, details: dict,
) -> dict:
    """Record progress — promote leased→running if needed, emit event."""
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime_id, event_type="run.progress", body={"message": message, "details": details})
    db.commit()
    return {"run_id": run.run_id, "event_id": event.event_id, "status": run.status}


def recovering_run_service(
    db: Session, *, run: Run, lease: Lease, runtime: Runtime, details: dict,
) -> dict:
    """Transition running→recovering, extend lease, emit event."""
    if run.status != RunStatus.RUNNING.value:
        raise ConflictError(f"run cannot enter recovering from state {run.status}")
    run.status = RunStatus.RECOVERING.value
    lease.expires_at = utc_now() + timedelta(seconds=30)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime.runtime_id, event_type="run.recovering", body={"details": details, "expires_at": lease.expires_at.isoformat()})
    db.commit()
    return {"run_id": run.run_id, "event_id": event.event_id, "status": run.status}


def resumed_run_service(db: Session, *, run: Run, runtime_id: str, details: dict) -> dict:
    """Transition recovering→running, emit event."""
    if run.status != RunStatus.RECOVERING.value:
        raise ConflictError(f"run cannot resume from state {run.status}")
    run.status = RunStatus.RUNNING.value
    event = _create_event(db, job_id=run.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=runtime_id, event_type="run.resumed", body={"details": details})
    db.commit()
    return {"run_id": run.run_id, "event_id": event.event_id, "status": run.status}


def cancel_run_service(
    db: Session, *, run: Run, job: Job, agent: Agent, runtime: Runtime, lease: Lease, reason: str,
) -> dict:
    """Cancel a run — release lease, transition states, emit events."""
    run.status = RunStatus.CANCELLED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.CANCELLED.value
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=run.agent_id, runtime_id=run.runtime_id, event_type="run.cancelled", body={"reason": reason})
    _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    db.commit()
    return {"run_id": run.run_id, "job_id": job.job_id, "status": run.status}
