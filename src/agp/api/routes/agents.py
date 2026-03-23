"""Agent route handlers — thin HTTP layer delegating to services."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.models import Agent
from agp.schemas import AgentDownRequest, AgentPatchRequest, AgentResponse, AgentUpRequest, OkResponse, PagedData
from agp.services.agents import agent_down_service, agent_patch_service, agent_undrain_service, agent_up_service

router = APIRouter()


@router.get("/agents", response_model=OkResponse[PagedData[AgentResponse]])
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


@router.get("/agents/{agent_id}", response_model=OkResponse[AgentResponse])
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    from agp.services._helpers import _require_agent
    agent = _require_agent(db, agent_id)
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref", "created_at", "updated_at", "last_seen_at")))


@router.post("/agents/up", response_model=OkResponse[AgentResponse])
def agent_up(request: AgentUpRequest, db: Session = Depends(get_db)) -> dict:
    agent = agent_up_service(db, agent_id=request.agent_id, capability_id=request.capability_id, assigned_runtime_id=request.assigned_runtime_id, workspace_ref=request.workspace_ref)
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")))


@router.patch("/agents/{agent_id}", response_model=OkResponse[AgentResponse])
def agent_patch(agent_id: str, request: AgentPatchRequest, db: Session = Depends(get_db)) -> dict:
    agent = agent_patch_service(db, agent_id=agent_id, workspace_ref=request.workspace_ref)
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")))


@router.post("/agents/{agent_id}/undrain")
def agent_undrain(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = agent_undrain_service(db, agent_id=agent_id)
    return _ok({"agent_id": agent.agent_id, "status": agent.status})


@router.post("/agents/{agent_id}/down")
def agent_down(agent_id: str, request: AgentDownRequest, db: Session = Depends(get_db)) -> dict:
    agent = agent_down_service(db, agent_id=agent_id, mode=request.mode)
    return _ok({"agent_id": agent.agent_id, "status": agent.status, "mode": request.mode})
