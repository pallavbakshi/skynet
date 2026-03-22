"""Background sweep operations for lease expiry, agent idle timeout, and runtime staleness."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.config import settings
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import Agent, Job, Lease, Run, Runtime, utc_now
from agp.services._helpers import (
    _record_agent_binding,
    _record_health_transition,
    _require_agent,
    _require_job,
    _require_runtime,
)
from agp.services.events import _create_event


def sweep_expired_leases(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or utc_now()
    expired = db.scalars(
        select(Lease).where(
            Lease.status == LeaseStatus.ACTIVE.value,
            Lease.expires_at < now,
        )
    ).all()
    processed = 0
    requeued = 0
    failed = 0
    for lease in expired:
        run = db.get(Run, lease.run_id)
        if run is None:
            continue
        job = _require_job(db, run.job_id)
        agent = _require_agent(db, lease.agent_id)
        runtime = _require_runtime(db, lease.runtime_id)
        lease.status = LeaseStatus.EXPIRED.value
        run.status = RunStatus.ABANDONED.value
        run.finished_at = now
        if job.retry_count + 1 >= job.max_retries:
            job.retry_count += 1
            job.status = JobStatus.FAILED.value
            failed += 1
        else:
            job.retry_count += 1
            job.status = JobStatus.QUEUED.value
            requeued += 1
        job.updated_at = now
        if agent.status != AgentStatus.TERMINATED.value:
            agent.status = AgentStatus.IDLE.value
            if agent.assigned_runtime_id is not None:
                _record_agent_binding(db, agent_id=agent.agent_id, runtime_id=agent.assigned_runtime_id, status="released")
            agent.assigned_runtime_id = None
        active_runtime_runs = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.runtime_id == runtime.runtime_id,
                Lease.status == LeaseStatus.ACTIVE.value,
                Lease.lease_id != lease.lease_id,
            )
        ) or 0
        runtime.status = RuntimeStatus.BUSY.value if active_runtime_runs else RuntimeStatus.IDLE.value
        _create_event(
            db,
            job_id=job.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=run.runtime_id,
            event_type="lease.expired",
            body={"lease_id": lease.lease_id, "reason": "heartbeat_timeout", "fencing_token": lease.fencing_token},
        )
        _create_event(
            db,
            job_id=job.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=run.runtime_id,
            event_type="run.abandoned",
            body={"lease_id": lease.lease_id},
        )
        if job.status == JobStatus.QUEUED.value:
            _create_event(
                db,
                job_id=job.job_id,
                event_type="job.requeued",
                body={"reason": "lease_expiry", "retry_count": job.retry_count},
            )
        else:
            _create_event(
                db,
                job_id=job.job_id,
                event_type="job.failed",
                body={"reason": "lease_expiry_retry_exhausted", "retry_count": job.retry_count},
            )
        processed += 1
    if processed:
        db.commit()
    return {"expired_leases": processed, "requeued_jobs": requeued, "failed_jobs": failed}


def sweep_idle_agents(
    db: Session,
    *,
    now: datetime | None = None,
    idle_timeout_seconds: int | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    idle_timeout_seconds = idle_timeout_seconds or settings.agent_idle_timeout_seconds
    cutoff = now - timedelta(seconds=idle_timeout_seconds)
    agents = db.scalars(
        select(Agent).where(
            Agent.status == AgentStatus.IDLE.value,
            Agent.last_seen_at.is_not(None),
            Agent.last_seen_at < cutoff,
        )
    ).all()
    terminated = 0
    for agent in agents:
        has_queued_work = db.scalar(
            select(func.count()).select_from(Job).where(
                Job.target_agent_id == agent.agent_id,
                Job.status == JobStatus.QUEUED.value,
            )
        ) or 0
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        has_active_run = db.scalar(
            select(func.count()).select_from(Run).where(
                Run.agent_id == agent.agent_id,
                Run.status.in_([RunStatus.LEASED.value, RunStatus.RUNNING.value, RunStatus.RECOVERING.value]),
            )
        ) or 0
        if has_queued_work or has_active_lease or has_active_run:
            continue
        agent.status = AgentStatus.TERMINATED.value
        agent.updated_at = now
        _create_event(
            db,
            agent_id=agent.agent_id,
            runtime_id=agent.assigned_runtime_id,
            event_type="agent.terminated",
            body={"reason": "idle_timeout", "idle_timeout_seconds": idle_timeout_seconds},
        )
        terminated += 1
    if terminated:
        db.commit()
    return {"terminated_agents": terminated}


def sweep_stale_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
    stale_timeout_seconds: int | None = None,
    degraded_timeout_seconds: int | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    stale_timeout_seconds = stale_timeout_seconds or settings.runtime_stale_timeout_seconds
    degraded_timeout_seconds = degraded_timeout_seconds or settings.runtime_degraded_timeout_seconds
    degraded_cutoff = now - timedelta(seconds=degraded_timeout_seconds)
    offline_cutoff = now - timedelta(seconds=stale_timeout_seconds)

    degraded_candidates = db.scalars(
        select(Runtime).where(
            Runtime.status.notin_([RuntimeStatus.OFFLINE.value, RuntimeStatus.DEGRADED.value]),
            Runtime.last_heartbeat_at.is_not(None),
            Runtime.last_heartbeat_at < degraded_cutoff,
            Runtime.last_heartbeat_at >= offline_cutoff,
        )
    ).all()
    degraded_runtimes = 0
    for runtime in degraded_candidates:
        runtime.status = RuntimeStatus.DEGRADED.value
        runtime.health_status = HealthStatus.DEGRADED.value
        runtime.updated_at = now
        _record_health_transition(
            db, entity_type="runtime", entity_id=runtime.runtime_id,
            health_status=HealthStatus.DEGRADED.value, reason="heartbeat_timeout_degraded",
        )
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.degraded",
            body={"reason": "heartbeat_timeout", "degraded_timeout_seconds": degraded_timeout_seconds},
        )
        degraded_runtimes += 1

    offline_candidates = db.scalars(
        select(Runtime).where(
            Runtime.status != RuntimeStatus.OFFLINE.value,
            Runtime.last_heartbeat_at.is_not(None),
            Runtime.last_heartbeat_at < offline_cutoff,
        )
    ).all()
    offlined = 0
    detached_agents = 0
    degraded_agents = 0
    for runtime in offline_candidates:
        runtime.status = RuntimeStatus.OFFLINE.value
        runtime.health_status = HealthStatus.UNREACHABLE.value
        runtime.updated_at = now
        _record_health_transition(
            db, entity_type="runtime", entity_id=runtime.runtime_id,
            health_status=HealthStatus.UNREACHABLE.value, reason="heartbeat_timeout_offline",
        )
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.offline",
            body={"reason": "heartbeat_timeout", "stale_timeout_seconds": stale_timeout_seconds},
        )
        agents = db.scalars(
            select(Agent).where(Agent.assigned_runtime_id == runtime.runtime_id)
        ).all()
        for agent in agents:
            has_active_lease = db.scalar(
                select(func.count()).select_from(Lease).where(
                    Lease.agent_id == agent.agent_id,
                    Lease.runtime_id == runtime.runtime_id,
                    Lease.status == LeaseStatus.ACTIVE.value,
                )
            ) or 0
            if has_active_lease:
                if agent.status != AgentStatus.TERMINATED.value:
                    agent.status = AgentStatus.DEGRADED.value
                    agent.updated_at = now
                    _record_health_transition(
                        db, entity_type="agent", entity_id=agent.agent_id,
                        health_status="degraded", reason="runtime_offline",
                    )
                    _create_event(
                        db,
                        agent_id=agent.agent_id,
                        runtime_id=runtime.runtime_id,
                        event_type="agent.degraded",
                        body={"reason": "runtime_offline", "runtime_id": runtime.runtime_id},
                    )
                    degraded_agents += 1
                continue
            if agent.status in {AgentStatus.TERMINATED.value, AgentStatus.DRAINING.value}:
                continue
            _record_agent_binding(db, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, status="released")
            agent.assigned_runtime_id = None
            agent.status = AgentStatus.IDLE.value
            agent.updated_at = now
            _create_event(
                db,
                agent_id=agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="agent.idle",
                body={"reason": "runtime_rebind_required", "previous_runtime_id": runtime.runtime_id},
            )
            detached_agents += 1
        offlined += 1
    changed = degraded_runtimes + offlined
    if changed:
        db.commit()
    return {
        "degraded_runtimes": degraded_runtimes,
        "offline_runtimes": offlined,
        "detached_agents": detached_agents,
        "degraded_agents": degraded_agents,
    }


def sweep_draining_agents(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    agents = db.scalars(
        select(Agent).where(Agent.status == AgentStatus.DRAINING.value)
    ).all()
    terminated = 0
    for agent in agents:
        has_queued_work = db.scalar(
            select(func.count()).select_from(Job).where(
                Job.target_agent_id == agent.agent_id,
                Job.status == JobStatus.QUEUED.value,
            )
        ) or 0
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if has_queued_work or has_active_lease:
            continue
        agent.status = AgentStatus.TERMINATED.value
        agent.updated_at = now
        _create_event(
            db,
            agent_id=agent.agent_id,
            runtime_id=agent.assigned_runtime_id,
            event_type="agent.terminated",
            body={"reason": "drain_complete"},
        )
        terminated += 1
    if terminated:
        db.commit()
    return {"terminated_agents": terminated}


def sweep_draining_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    runtimes = db.scalars(
        select(Runtime).where(Runtime.status == RuntimeStatus.DRAINING.value)
    ).all()
    resumed = 0
    for runtime in runtimes:
        active_leases = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.runtime_id == runtime.runtime_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if active_leases:
            continue
        runtime.status = RuntimeStatus.IDLE.value
        if runtime.health_status == HealthStatus.DRAINING.value:
            runtime.health_status = HealthStatus.HEALTHY.value
            _record_health_transition(
                db, entity_type="runtime", entity_id=runtime.runtime_id,
                health_status=HealthStatus.HEALTHY.value, reason="drain_complete",
            )
        runtime.updated_at = now
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.idle",
            body={"reason": "drain_complete"},
        )
        resumed += 1
    if resumed:
        db.commit()
    return {"resumed_runtimes": resumed}
