"""Agent domain operations — up, down, undrain, patch, interrupt."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.enums import AgentStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import Agent, Job, Lease, QueueDeliveryRecord, Run, Runtime, utc_now
from agp.services._helpers import _new_id, _queue_backend, _require_agent, _require_capability, _require_runtime
from agp.services.events import _create_event
from agp.services.exceptions import ConflictError

# All non-terminal job statuses that force-down must cancel.
_CANCELLABLE_JOB_STATUSES = [
    JobStatus.ACCEPTED.value,
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.INTERRUPT_REQUESTED.value,
    JobStatus.BLOCKED.value,
]

_CANCELLABLE_RUN_STATUSES = [
    RunStatus.CREATED.value,
    RunStatus.LEASED.value,
    RunStatus.RUNNING.value,
    RunStatus.RECOVERING.value,
]


def agent_up_service(
    db: Session,
    *,
    agent_id: str | None,
    capability_id: str,
    assigned_runtime_id: str | None,
    workspace_ref: str | None,
) -> Agent:
    _require_capability(db, capability_id)
    resolved_id = agent_id or _new_id("agt")
    if db.get(Agent, resolved_id) is not None:
        raise ConflictError(f"agent already exists: {resolved_id}")
    if assigned_runtime_id is not None:
        _require_runtime(db, assigned_runtime_id)
    agent = Agent(
        agent_id=resolved_id,
        capability_id=capability_id,
        assigned_runtime_id=assigned_runtime_id,
        queue_id=f"agent:{resolved_id}",
        status=AgentStatus.PROVISIONING.value,
        workspace_ref=workspace_ref,
        last_seen_at=utc_now(),
    )
    db.add(agent)
    _create_event(db, agent_id=agent.agent_id, event_type="agent.provisioning", body={"capability_id": agent.capability_id})
    agent.status = AgentStatus.IDLE.value
    _create_event(db, agent_id=agent.agent_id, event_type="agent.idle", body={"capability_id": agent.capability_id})
    db.commit()
    return agent


def agent_patch_service(db: Session, *, agent_id: str, workspace_ref: str | None) -> Agent:
    agent = _require_agent(db, agent_id)
    if workspace_ref is not None:
        agent.workspace_ref = workspace_ref
    db.commit()
    return agent


def agent_undrain_service(db: Session, *, agent_id: str) -> Agent:
    agent = _require_agent(db, agent_id)
    if agent.status != AgentStatus.DRAINING.value:
        raise ConflictError(f"agent is not draining (status={agent.status})")
    agent.status = AgentStatus.IDLE.value
    agent.updated_at = utc_now()
    _create_event(db, agent_id=agent.agent_id, event_type="agent.idle", body={"reason": "undrain"})
    db.commit()
    return agent


def agent_down_service(db: Session, *, agent_id: str, mode: str) -> Agent:
    agent = _require_agent(db, agent_id)
    now = utc_now()
    previous_status = agent.status

    # Idempotency: already terminated is a no-op conflict.
    if previous_status == AgentStatus.TERMINATED.value:
        raise ConflictError(f"agent is already terminated: {agent_id}")

    # Double-drain guard.
    if mode == "drain" and previous_status == AgentStatus.DRAINING.value:
        raise ConflictError(f"agent is already draining: {agent_id}")

    # TOCTOU guard: if caller asked for terminate but agent may have
    # active work, reject rather than silently orphaning it.
    _ACTIVE_WORK_STATUSES = (
        AgentStatus.BUSY.value,
        AgentStatus.DRAINING.value,
        AgentStatus.DEGRADED.value,
    )
    if mode == "terminate" and previous_status in _ACTIVE_WORK_STATUSES:
        raise ConflictError(
            f"agent has active work (status={previous_status}); use mode='force' to cancel"
        )

    if mode == "drain":
        agent.status = AgentStatus.DRAINING.value
        event_type = "agent.draining"
    else:
        agent.status = AgentStatus.TERMINATED.value
        event_type = "agent.terminated"
        if mode == "force":
            _force_cancel_agent_work(db, agent_id, now)

    agent.updated_at = now
    _create_event(
        db, agent_id=agent.agent_id, event_type=event_type,
        body={"mode": mode, "previous_status": previous_status},
    )
    db.commit()
    return agent


def _cancel_job_cascade(db: Session, job: Job, now, *, reason: str) -> set[str]:
    """Cancel a single job and cascade to its runs and leases.

    Returns the set of runtime_ids whose leases were released.
    """
    prev_job_status = job.status
    job.status = JobStatus.CANCELLED.value
    job.updated_at = now
    _create_event(
        db, job_id=job.job_id, event_type="job.cancelled",
        body={"previous_status": prev_job_status, "reason": reason},
    )
    affected_runtime_ids: set[str] = set()
    active_runs = db.scalars(
        select(Run).where(
            Run.job_id == job.job_id,
            Run.status.in_(_CANCELLABLE_RUN_STATUSES),
        )
    ).all()
    for run in active_runs:
        prev_run_status = run.status
        run.status = RunStatus.CANCELLED.value
        run.finished_at = now
        _create_event(
            db, job_id=job.job_id, event_type="run.cancelled",
            body={"run_id": run.run_id, "previous_status": prev_run_status, "reason": reason},
        )
        active_leases = db.scalars(
            select(Lease).where(
                Lease.run_id == run.run_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ).all()
        for lease in active_leases:
            prev_lease_status = lease.status
            lease.status = LeaseStatus.RELEASED.value
            lease.released_at = now
            if lease.runtime_id:
                affected_runtime_ids.add(lease.runtime_id)
            _create_event(
                db, job_id=job.job_id, event_type="lease.released",
                body={"lease_id": lease.lease_id, "previous_status": prev_lease_status, "reason": reason},
            )
    return affected_runtime_ids


def _release_idle_runtimes(db: Session, runtime_ids: set[str]) -> None:
    """Transition runtimes back to IDLE if they have no remaining active leases."""
    for runtime_id in runtime_ids:
        remaining = db.scalar(
            select(Lease.lease_id).where(
                Lease.runtime_id == runtime_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            ).limit(1)
        )
        if remaining is None:
            runtime = db.get(Runtime, runtime_id)
            if runtime is not None and runtime.status == RuntimeStatus.BUSY.value:
                runtime.status = RuntimeStatus.IDLE.value


def _force_cancel_agent_work(db: Session, agent_id: str, now) -> None:
    """Cancel all active jobs, runs, and leases for an agent.

    Finds jobs two ways:
    1. Directly targeted: ``Job.target_agent_id == agent_id``
    2. Capability-routed: ``target_agent_id IS NULL`` but has an active
       run assigned to this agent.
    """
    # Path 1: directly targeted jobs
    direct_jobs = db.scalars(
        select(Job).where(
            Job.target_agent_id == agent_id,
            Job.status.in_(_CANCELLABLE_JOB_STATUSES),
        )
    ).all()
    # Path 2: capability-routed jobs with active runs on this agent
    routed_jobs = db.scalars(
        select(Job).join(Run, Run.job_id == Job.job_id).where(
            Run.agent_id == agent_id,
            Run.status.in_(_CANCELLABLE_RUN_STATUSES),
            Job.status.in_(_CANCELLABLE_JOB_STATUSES),
        )
    ).all()
    # Deduplicate
    seen: set[str] = set()
    active_jobs: list[Job] = []
    for job in [*direct_jobs, *routed_jobs]:
        if job.job_id not in seen:
            seen.add(job.job_id)
            active_jobs.append(job)

    affected_runtime_ids: set[str] = set()
    for job in active_jobs:
        affected_runtime_ids |= _cancel_job_cascade(db, job, now, reason="agent_force_down")

    _release_idle_runtimes(db, affected_runtime_ids)
    _ack_cancelled_deliveries(db, [j.job_id for j in active_jobs], [], now)


# ── Interrupt ─────────────────────────────────────────────────────

# Job statuses that represent active execution (currently running on a runtime).
_ACTIVE_JOB_STATUSES = [
    JobStatus.RUNNING.value,
    JobStatus.INTERRUPT_REQUESTED.value,
]

# Job statuses sitting in the queue (not yet claimed by a runtime).
_QUEUED_JOB_STATUSES = [
    JobStatus.ACCEPTED.value,
    JobStatus.QUEUED.value,
    JobStatus.BLOCKED.value,
]


def _ack_cancelled_deliveries(
    db: Session,
    halted_job_ids: list[str],
    dropped_job_ids: list[str],
    now,
) -> None:
    """Transition delivery records for cancelled jobs to 'acked' so they
    don't pollute observability metrics or trigger stale-delivery redrives."""
    all_ids = [*halted_job_ids, *dropped_job_ids]
    if not all_ids:
        return
    stale = db.scalars(
        select(QueueDeliveryRecord).where(
            QueueDeliveryRecord.job_id.in_(all_ids),
            QueueDeliveryRecord.state.in_(("pending", "delivered")),
        )
    ).all()
    for d in stale:
        d.state = "acked"
        d.acked_at = now


def agent_interrupt_service(
    db: Session,
    *,
    agent_id: str,
    purge: bool = False,
) -> dict:
    """Interrupt active execution on an agent, optionally purging its queue.

    The active job (if any) is cancelled along with its runs and leases.
    When *purge* is ``True``, all queued jobs in the agent's **direct**
    queue (``agent:{agent_id}``) are also cancelled.  Capability-pool
    queued jobs are intentionally left untouched since they are shared
    resources that could be claimed by other agents.

    Returns a structured dict describing what was cancelled.
    """
    agent = _require_agent(db, agent_id)
    now = utc_now()
    agent_queue = f"agent:{agent_id}"

    if agent.status == AgentStatus.TERMINATED.value:
        raise ConflictError(f"agent is already terminated: {agent_id}")

    # ── Find the active (running) job ──
    # An agent should have at most one active job; limit(1) is a guard.
    # Path 1: directly targeted
    active_job = db.scalars(
        select(Job).where(
            Job.target_agent_id == agent_id,
            Job.status.in_(_ACTIVE_JOB_STATUSES),
        ).limit(1)
    ).first()
    # Path 2: capability-routed via active run
    if active_job is None:
        active_job = db.scalars(
            select(Job).join(Run, Run.job_id == Job.job_id).where(
                Run.agent_id == agent_id,
                Run.status.in_(_CANCELLABLE_RUN_STATUSES),
                Job.status.in_(_ACTIVE_JOB_STATUSES),
            ).limit(1)
        ).first()

    halted_job_id: str | None = None

    if active_job is not None:
        halted_job_id = active_job.job_id
        if active_job.status == JobStatus.RUNNING.value:
            active_job.status = JobStatus.INTERRUPT_REQUESTED.value
            active_job.updated_at = now
            _create_event(
                db,
                job_id=active_job.job_id,
                event_type="job.interrupt_requested",
                body={"previous_status": JobStatus.RUNNING.value, "status": JobStatus.INTERRUPT_REQUESTED.value},
            )

    # ── Purge queued jobs ──
    dropped_job_ids: list[str] = []
    if purge:
        queued_jobs = db.scalars(
            select(Job).where(
                Job.target_queue == agent_queue,
                Job.status.in_(_QUEUED_JOB_STATUSES),
            )
        ).all()
        for job in queued_jobs:
            prev = job.status
            job.status = JobStatus.CANCELLED.value
            job.updated_at = now
            _create_event(
                db, job_id=job.job_id, event_type="job.cancelled",
                body={"previous_status": prev, "reason": "purge"},
            )
            dropped_job_ids.append(job.job_id)
        _ack_cancelled_deliveries(db, [], dropped_job_ids, now)
        _queue_backend().remove_jobs(db, target_queue=agent_queue, job_ids=dropped_job_ids)

    # ── Count remaining queued jobs ──
    remaining_count: int = db.scalar(
        select(func.count()).select_from(Job).where(
            Job.target_queue == agent_queue,
            Job.status.in_(_QUEUED_JOB_STATUSES),
        )
    ) or 0

    # ── Update agent status ──
    _PRESERVE_STATUSES = frozenset({
        AgentStatus.TERMINATED.value,
        AgentStatus.DRAINING.value,
    })
    if active_job is None and agent.status not in _PRESERVE_STATUSES:
        agent.status = AgentStatus.IDLE.value
    agent.updated_at = now

    _create_event(
        db, agent_id=agent_id, event_type="agent.interrupted",
        body={
            "halted_job_id": halted_job_id,
            "dropped_job_ids": dropped_job_ids,
            "purge": purge,
        },
    )
    db.commit()

    return {
        "agent_id": agent_id,
        "status": agent.status,
        "halted_job_id": halted_job_id,
        "dropped_job_ids": dropped_job_ids,
        "remaining_queue_size": remaining_count,
    }
