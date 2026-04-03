"""Background sweep operations for lease expiry and agent liveness."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from agp.config import settings
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import Agent, Job, Lease, QueueDeliveryRecord, Run, Runtime, utc_now
from agp.queue_backend import get_queue_backend
from agp.services._helpers import (
    _record_health_transition,
    _require_job,
)
from agp.services.events import _create_event


def _nullify_agent_references(db: Session, agent_id: str) -> None:
    """Unlink FK references before agent deletion.

    Runs and leases retain agent_id as audit history (no FK constraint in migration 0002).
    Events are nullified as belt-and-suspenders for the initial-migration FK.
    Runtimes are unlinked (physical process should not reference a deleted agent).
    """
    db.execute(text("UPDATE events SET agent_id = NULL WHERE agent_id = :aid"), {"aid": agent_id})
    db.execute(text("UPDATE runtimes SET agent_id = NULL WHERE agent_id = :aid"), {"aid": agent_id})


def _ack_queue_deliveries(db: Session, *, job_ids: list[str], now: datetime) -> None:
    if not job_ids:
        return
    rows = db.scalars(
        select(QueueDeliveryRecord).where(
            QueueDeliveryRecord.job_id.in_(job_ids),
            QueueDeliveryRecord.state.in_(("pending", "delivered")),
        )
    ).all()
    for row in rows:
        row.state = "acked"
        row.acked_at = now
        row.updated_at = now


def sweep_expired_leases(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Expire leases whose TTL has passed, abandon runs, requeue or fail jobs."""
    now = now or utc_now()
    queue_backend = get_queue_backend(settings.queue_backend)
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
        job = db.get(Job, run.job_id)
        if job is None:
            continue
        runtime = db.get(Runtime, lease.runtime_id)

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
            queue_backend.enqueue_job(db, job=job)
            requeued += 1
        job.updated_at = now

        # Transition agent back to idle if it still exists
        agent = db.get(Agent, lease.agent_id) if lease.agent_id else None
        if agent is not None and agent.status == AgentStatus.BUSY.value:
            agent.status = AgentStatus.IDLE.value

        # Transition runtime back to idle if no remaining active leases
        if runtime is not None:
            active_runtime_leases = db.scalar(
                select(func.count()).select_from(Lease).where(
                    Lease.runtime_id == runtime.runtime_id,
                    Lease.status == LeaseStatus.ACTIVE.value,
                    Lease.lease_id != lease.lease_id,
                )
            ) or 0
            runtime.status = RuntimeStatus.BUSY.value if active_runtime_leases else RuntimeStatus.IDLE.value

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
                db, job_id=job.job_id, event_type="job.requeued",
                body={"reason": "lease_expiry", "retry_count": job.retry_count},
            )
        else:
            _create_event(
                db, job_id=job.job_id, event_type="job.failed",
                body={"reason": "lease_expiry_retry_exhausted", "retry_count": job.retry_count},
            )
        processed += 1
    if processed:
        db.commit()
    return {"expired_leases": processed, "requeued_jobs": requeued, "failed_jobs": failed}


def sweep_stale_agents(
    db: Session,
    *,
    now: datetime | None = None,
    heartbeat_grace_seconds: int | None = None,
    stale_timeout_seconds: int | None = None,
    degraded_timeout_seconds: int | None = None,
) -> dict[str, int]:
    """Unified agent+runtime liveness sweep.

    - Delete idle agents with stale heartbeat (conditional DELETE for race safety)
    - Delete draining agents with empty queues
    - Mark stale runtimes degraded/offline
    - Resume draining runtimes with no active leases

    Busy agents are never deleted — the lease sweeper handles them first.
    """
    now = now or utc_now()
    grace = heartbeat_grace_seconds or settings.agent_heartbeat_grace_seconds
    cutoff = now - timedelta(seconds=grace)

    # ── Phase 1: Delete stale idle agents ──
    idle_candidates = db.scalars(
        select(Agent).where(
            Agent.status == AgentStatus.IDLE.value,
            Agent.last_heartbeat_at < cutoff,
        )
    ).all()

    deleted = 0
    for agent in idle_candidates:
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if has_active_lease:
            continue

        result = db.execute(
            text(
                "DELETE FROM agents "
                "WHERE agent_id = :aid AND status = 'idle' AND last_heartbeat_at < :cutoff"
            ),
            {"aid": agent.agent_id, "cutoff": cutoff},
        )
        if result.rowcount:
            _create_event(
                db, agent_id=agent.agent_id, event_type="agent.deleted",
                body={"reason": "heartbeat_timeout", "grace_seconds": grace},
            )
            _nullify_agent_references(db, agent.agent_id)
            db.expire(agent)
            deleted += 1

    # ── Phase 2: Delete draining agents with no remaining work ──
    # Draining agents can't claim new work, so any queued jobs targeted at
    # them would be stuck forever (M1 deadlock).  When no active leases
    # remain, cancel stranded targeted jobs and delete the agent.
    draining_candidates = db.scalars(
        select(Agent).where(Agent.status == AgentStatus.DRAINING.value)
    ).all()
    drained = 0
    stranded_cancelled = 0
    for agent in draining_candidates:
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if has_active_lease:
            continue

        # Cancel queued jobs targeted at this agent — they'll never be claimed.
        stranded_jobs = db.scalars(
            select(Job).where(
                Job.target_agent_id == agent.agent_id,
                Job.status == JobStatus.QUEUED.value,
            )
        ).all()
        stranded_job_ids: list[str] = []
        for job in stranded_jobs:
            job.status = JobStatus.FAILED.value
            job.updated_at = now
            stranded_job_ids.append(job.job_id)
            _create_event(
                db, job_id=job.job_id, event_type="job.failed",
                body={"reason": "agent_drain_complete", "agent_id": agent.agent_id},
            )
            stranded_cancelled += 1
        if stranded_job_ids:
            _ack_queue_deliveries(db, job_ids=stranded_job_ids, now=now)
            get_queue_backend(settings.queue_backend).remove_jobs(
                db,
                target_queue=f"agent:{agent.agent_id}",
                job_ids=stranded_job_ids,
            )

        # M2: conditional DELETE for race safety (mirrors Phase 1 pattern)
        result = db.execute(
            text(
                "DELETE FROM agents "
                "WHERE agent_id = :aid AND status = 'draining'"
            ),
            {"aid": agent.agent_id},
        )
        if result.rowcount:
            _create_event(
                db, agent_id=agent.agent_id, event_type="agent.deleted",
                body={"reason": "drain_complete"},
            )
            _nullify_agent_references(db, agent.agent_id)
            db.expire(agent)
            drained += 1

    # ── Phase 3: Mark stale runtimes degraded/offline ──
    stale_timeout = stale_timeout_seconds or settings.runtime_stale_timeout_seconds
    degraded_timeout = degraded_timeout_seconds or settings.runtime_degraded_timeout_seconds
    degraded_cutoff = now - timedelta(seconds=degraded_timeout)
    offline_cutoff = now - timedelta(seconds=stale_timeout)

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
            db, runtime_id=runtime.runtime_id, event_type="runtime.degraded",
            body={"reason": "heartbeat_timeout", "degraded_timeout_seconds": degraded_timeout},
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
    for runtime in offline_candidates:
        runtime.status = RuntimeStatus.OFFLINE.value
        runtime.health_status = HealthStatus.UNREACHABLE.value
        runtime.updated_at = now
        _record_health_transition(
            db, entity_type="runtime", entity_id=runtime.runtime_id,
            health_status=HealthStatus.UNREACHABLE.value, reason="heartbeat_timeout_offline",
        )
        _create_event(
            db, runtime_id=runtime.runtime_id, event_type="runtime.offline",
            body={"reason": "heartbeat_timeout", "stale_timeout_seconds": stale_timeout},
        )
        offlined += 1

    # ── Phase 4: Resume draining runtimes with no active leases ──
    draining_runtimes = db.scalars(
        select(Runtime).where(Runtime.status == RuntimeStatus.DRAINING.value)
    ).all()
    resumed = 0
    for runtime in draining_runtimes:
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
            db, runtime_id=runtime.runtime_id, event_type="runtime.idle",
            body={"reason": "drain_complete"},
        )
        resumed += 1

    # ── Phase 5: Fail orphaned queued jobs whose target agent no longer exists ──
    # When a stale agent is deleted (Phase 1), jobs with target_agent_id pointing
    # at it become permanently stranded — no agent will ever claim them. (M12)
    orphaned_jobs = db.scalars(
        select(Job).where(
            Job.target_agent_id.is_not(None),
            Job.status == JobStatus.QUEUED.value,
            ~Job.target_agent_id.in_(select(Agent.agent_id)),
        )
    ).all()
    orphaned = 0
    for job in orphaned_jobs:
        job.status = JobStatus.FAILED.value
        job.updated_at = now
        _create_event(
            db, job_id=job.job_id, event_type="job.failed",
            body={"reason": "target_agent_deleted", "target_agent_id": job.target_agent_id},
        )
        orphaned += 1

    total = deleted + drained + degraded_runtimes + offlined + resumed + stranded_cancelled + orphaned
    if total:
        db.commit()
    return {
        "deleted_stale": deleted,
        "deleted_drained": drained,
        "stranded_jobs_cancelled": stranded_cancelled,
        "orphaned_jobs_failed": orphaned,
        "degraded_runtimes": degraded_runtimes,
        "offline_runtimes": offlined,
        "resumed_runtimes": resumed,
    }


def sweep_stale_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
    stale_timeout_seconds: int | None = None,
    degraded_timeout_seconds: int | None = None,
) -> dict[str, int]:
    """Deprecated: use sweep_stale_agents which now includes runtime sweeping."""
    result = sweep_stale_agents(
        db, now=now,
        stale_timeout_seconds=stale_timeout_seconds,
        degraded_timeout_seconds=degraded_timeout_seconds,
    )
    return {"degraded_runtimes": result["degraded_runtimes"], "offline_runtimes": result["offline_runtimes"]}


def sweep_draining_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Deprecated: use sweep_stale_agents which now includes runtime sweeping."""
    result = sweep_stale_agents(db, now=now)
    return {"resumed_runtimes": result["resumed_runtimes"]}


def refresh_active_leases(db: Session, *, default_ttl_seconds: int = 60) -> int:
    """Refresh all active lease expiry times on CP startup.

    Prevents the lease sweeper from mass-expiring leases that timed out
    while the CP was down.
    """
    now = utc_now()
    new_expiry = now + timedelta(seconds=default_ttl_seconds)
    result = db.execute(
        text(
            "UPDATE leases SET expires_at = :new_expiry "
            "WHERE status = 'active' AND expires_at < :now"
        ),
        {"new_expiry": new_expiry, "now": now},
    )
    count = result.rowcount
    if count:
        db.commit()
    return count
