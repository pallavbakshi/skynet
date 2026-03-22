"""Agent route handlers."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.enums import AgentStatus, JobStatus
from agp.models import Agent, Job, utc_now
from agp.schemas import AgentDownRequest, AgentPatchRequest, AgentUpRequest
from agp.services._helpers import _new_id, _require_agent, _require_capability, _require_runtime
from agp.services.events import _create_event

router = APIRouter()


@router.get("/agents", response_model=dict)
def list_agents(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    capability_id: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Agent)
    if status is not None:
        query = query.where(Agent.status == status)
    if capability_id is not None:
        query = query.where(Agent.capability_id == capability_id)
    if created_after is not None:
        query = query.where(Agent.created_at >= created_after)
    if cursor_payload is not None:
        created_at = datetime.fromisoformat(str(cursor_payload["created_at"]))
        agent_id = str(cursor_payload["agent_id"])
        query = query.where(
            (Agent.created_at < created_at) | ((Agent.created_at == created_at) & (Agent.agent_id < agent_id))
        )
    agents = db.scalars(query.order_by(Agent.created_at.desc(), Agent.agent_id.desc()).limit(limit + 1)).all()
    page_items = agents[:limit]
    next_cursor = None
    if len(agents) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"created_at": last.created_at.isoformat(), "agent_id": last.agent_id})
    return _ok(
        _page(
            [_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")) for agent in page_items],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.get("/agents/{agent_id}", response_model=dict)
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = _require_agent(db, agent_id)
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref", "created_at", "updated_at", "last_seen_at")))


@router.post("/agents/up", response_model=dict)
def agent_up(request: AgentUpRequest, db: Session = Depends(get_db)) -> dict:
    _require_capability(db, request.capability_id)
    agent_id = request.agent_id or _new_id("agt")
    if db.get(Agent, agent_id) is not None:
        raise HTTPException(status_code=409, detail=f"agent already exists: {agent_id}")
    if request.assigned_runtime_id is not None:
        _require_runtime(db, request.assigned_runtime_id)
    agent = Agent(
        agent_id=agent_id,
        capability_id=request.capability_id,
        assigned_runtime_id=request.assigned_runtime_id,
        queue_id=f"agent:{agent_id}",
        status=AgentStatus.PROVISIONING.value,
        workspace_ref=request.workspace_ref,
        last_seen_at=utc_now(),
    )
    db.add(agent)
    _create_event(db, agent_id=agent.agent_id, event_type="agent.provisioning", body={"capability_id": agent.capability_id})
    agent.status = AgentStatus.IDLE.value
    _create_event(db, agent_id=agent.agent_id, event_type="agent.idle", body={"capability_id": agent.capability_id})
    db.commit()
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")))


@router.patch("/agents/{agent_id}", response_model=dict)
def agent_patch(agent_id: str, request: AgentPatchRequest, db: Session = Depends(get_db)) -> dict:
    agent = _require_agent(db, agent_id)
    if request.workspace_ref is not None:
        agent.workspace_ref = request.workspace_ref
    db.commit()
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")))


@router.post("/agents/{agent_id}/undrain", response_model=dict)
def agent_undrain(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = _require_agent(db, agent_id)
    if agent.status != AgentStatus.DRAINING.value:
        raise HTTPException(status_code=409, detail=f"agent is not draining (status={agent.status})")
    agent.status = AgentStatus.IDLE.value
    agent.updated_at = utc_now()
    _create_event(db, agent_id=agent.agent_id, event_type="agent.idle", body={"reason": "undrain"})
    db.commit()
    return _ok({"agent_id": agent.agent_id, "status": agent.status})


@router.post("/agents/{agent_id}/down", response_model=dict)
def agent_down(agent_id: str, request: AgentDownRequest, db: Session = Depends(get_db)) -> dict:
    agent = _require_agent(db, agent_id)
    if request.mode == "drain":
        agent.status = AgentStatus.DRAINING.value
        event_type = "agent.draining"
    else:
        agent.status = AgentStatus.TERMINATED.value
        event_type = "agent.terminated"
        if request.mode == "force":
            running_jobs = db.scalars(
                select(Job).where(Job.target_agent_id == agent_id, Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value]))
            ).all()
            for job in running_jobs:
                job.status = JobStatus.CANCELLED.value
                job.updated_at = utc_now()
                _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    agent.updated_at = utc_now()
    _create_event(db, agent_id=agent.agent_id, event_type=event_type, body={"mode": request.mode})
    db.commit()
    return _ok({"agent_id": agent.agent_id, "status": agent.status, "mode": request.mode})
