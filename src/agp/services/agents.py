"""Agent domain operations — up, down, undrain, patch."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.enums import AgentStatus, JobStatus
from agp.models import Agent, Job, utc_now
from agp.services._helpers import _new_id, _require_agent, _require_capability, _require_runtime
from agp.services.events import _create_event
from agp.services.exceptions import ConflictError


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
    if mode == "drain":
        agent.status = AgentStatus.DRAINING.value
        event_type = "agent.draining"
    else:
        agent.status = AgentStatus.TERMINATED.value
        event_type = "agent.terminated"
        if mode == "force":
            running_jobs = db.scalars(
                select(Job).where(Job.target_agent_id == agent_id, Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value]))
            ).all()
            for job in running_jobs:
                job.status = JobStatus.CANCELLED.value
                job.updated_at = utc_now()
                _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    agent.updated_at = utc_now()
    _create_event(db, agent_id=agent.agent_id, event_type=event_type, body={"mode": mode})
    db.commit()
    return agent
