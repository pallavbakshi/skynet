"""Agent route handlers — thin HTTP layer delegating to services."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.models import Agent
from agp.schemas import AgentDownRequest, AgentInterruptRequest, AgentPatchRequest, AgentResponse, AgentUpRequest, OkResponse, PagedData
from agp.services.agents import agent_down_service, agent_interrupt_service, agent_patch_service, agent_undrain_service, agent_up_service

router = APIRouter()

_AGENT_FIELDS = ("agent_id", "capabilities", "metadata_json", "queue_id", "status", "workspace_ref", "last_heartbeat_at", "created_at")


@router.get("/agents", response_model=OkResponse[PagedData[AgentResponse]])
def list_agents(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    capability: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Agent)
    if status is not None:
        query = query.where(Agent.status == status)
    if capability is not None:
        # Filter by self-declared capability in JSONB array
        from agp.db import engine
        from sqlalchemy import text as _text
        if str(engine.url).startswith("postgresql"):
            from sqlalchemy import cast, literal
            from sqlalchemy.dialects.postgresql import JSONB
            query = query.where(Agent.capabilities.op("@>")(cast(literal(f'["{capability}"]'), JSONB)))
        else:
            # json_each for exact array element match (avoids substring false positives)
            query = query.where(_text("EXISTS (SELECT 1 FROM json_each(agents.capabilities) je WHERE je.value = :cap)").bindparams(cap=capability))
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
            [_serialize(agent, _AGENT_FIELDS) for agent in page_items],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.get("/agents/{agent_id}", response_model=OkResponse[AgentResponse])
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> dict:
    from agp.services._helpers import _require_agent
    agent = _require_agent(db, agent_id)
    return _ok(_serialize(agent, (*_AGENT_FIELDS, "created_at", "updated_at")))


@router.post("/agents/up", response_model=OkResponse[AgentResponse])
def agent_up(request: AgentUpRequest, db: Session = Depends(get_db)) -> dict:
    agent = agent_up_service(
        db,
        agent_id=request.agent_id,
        capabilities=request.capabilities,
        metadata=request.metadata,
        workspace_ref=request.workspace_ref,
    )
    return _ok(_serialize(agent, _AGENT_FIELDS))


@router.patch("/agents/{agent_id}", response_model=OkResponse[AgentResponse])
def agent_patch(agent_id: str, request: AgentPatchRequest, db: Session = Depends(get_db)) -> dict:
    workspace_ref = request.workspace_ref if "workspace_ref" in request.model_fields_set else None
    clear_workspace_ref = "workspace_ref" in request.model_fields_set
    agent = agent_patch_service(
        db,
        agent_id=agent_id,
        workspace_ref=workspace_ref,
        clear_workspace_ref=clear_workspace_ref,
    )
    return _ok(_serialize(agent, _AGENT_FIELDS))


@router.post("/agents/{agent_id}/undrain")
def agent_undrain(agent_id: str, db: Session = Depends(get_db)) -> dict:
    agent = agent_undrain_service(db, agent_id=agent_id)
    return _ok({"agent_id": agent.agent_id, "status": agent.status})


@router.post("/agents/{agent_id}/interrupt")
def agent_interrupt(agent_id: str, request: AgentInterruptRequest = AgentInterruptRequest(), db: Session = Depends(get_db)) -> dict:
    result = agent_interrupt_service(db, agent_id=agent_id, purge=request.purge)
    return _ok(result)


@router.post("/agents/{agent_id}/down")
def agent_down(agent_id: str, body: AgentDownRequest, http_request: Request, db: Session = Depends(get_db)) -> dict:
    # M6: force-delete requires operator lifecycle auth, not just runtime token.
    if body.mode == "force":
        from agp.api.middleware import _extract_bearer_token, _operator_role_for_token, _OPERATOR_ROLE_RANK
        from agp.config import settings
        if settings.operator_bearer_token or settings.operator_token_roles_json:
            token = _extract_bearer_token(http_request.headers.get("Authorization"))
            role = _operator_role_for_token(token)
            if role is None or _OPERATOR_ROLE_RANK.get(role, 0) < _OPERATOR_ROLE_RANK["lifecycle"]:
                from agp.api.helpers import _error_response
                return _error_response(403, "forbidden", "force-delete requires operator lifecycle role; use mode=drain for runtime self-deregister")
    result = agent_down_service(db, agent_id=agent_id, mode=body.mode)
    return _ok(result)
