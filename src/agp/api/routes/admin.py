"""Admin route handlers: capabilities, pools, nudges, health, upgrade status, queue deliveries."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _apply_created_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.models import Capability, CapabilityPool, Nudge, QueueDeliveryRecord, utc_now
from agp.schemas import CapabilitySeedRequest, CreateNudgeRequest, HealthResponse
from agp.services._helpers import (
    _enqueue_nudge,
    _ensure_capability_pool,
    _get_upgrade_status,
    _new_id,
    _require_capability,
)
from agp.services.events import _create_event
from agp.api.helpers import _decode_cursor

router = APIRouter()


@router.get("/health")
def health() -> dict:
    payload = HealthResponse(components={"api": "ok", "db": "ok"})
    return _ok(payload.model_dump())


@router.get("/system/upgrade-status")
def system_upgrade_status(db: Session = Depends(get_db)) -> dict:
    return _ok(_get_upgrade_status(db))


@router.get("/capabilities/{capability_id}")
def get_capability(capability_id: str, db: Session = Depends(get_db)) -> dict:
    cap = _require_capability(db, capability_id)
    return _ok(_serialize(cap, ("capability_id", "name", "version", "image_ref", "model_ref", "resource_tier", "permission_profile", "queue_mode", "runtime_requirements_json")))


@router.get("/capabilities")
def list_capabilities(
    db: Session = Depends(get_db),
    version: str | None = Query(default=None),
    name: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Capability)
    if version is not None:
        query = query.where(Capability.version == version)
    if name is not None:
        query = query.where(Capability.name == name)
    if cursor_payload is not None:
        name = str(cursor_payload["name"])
        capability_id = str(cursor_payload["capability_id"])
        query = query.where(
            (Capability.name > name) | ((Capability.name == name) & (Capability.capability_id > capability_id))
        )
    capabilities = db.scalars(query.order_by(Capability.name.asc(), Capability.capability_id.asc()).limit(limit + 1)).all()
    page_items = capabilities[:limit]
    next_cursor = None
    if len(capabilities) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"name": last.name, "capability_id": last.capability_id})
    return _ok(_page(
        [_serialize(c, ("capability_id", "name", "version", "image_ref", "model_ref", "resource_tier", "permission_profile", "queue_mode", "runtime_requirements_json")) for c in page_items],
        limit=limit, next_cursor=next_cursor,
    ))


@router.post("/capabilities/seed")
def seed_capability(request: CapabilitySeedRequest, db: Session = Depends(get_db)) -> dict:
    capability_id = request.capability_id
    existing = db.get(Capability, capability_id)
    if existing is not None:
        existing.name = request.name
        existing.version = request.version
        existing.image_ref = request.image_ref
        existing.model_ref = request.model_ref
        existing.resource_tier = request.resource_tier
        existing.permission_profile = request.permission_profile
        existing.queue_mode = request.queue_mode
        existing.runtime_requirements_json = request.runtime_requirements
        existing.updated_at = utc_now()
        db.flush()
        pool = _ensure_capability_pool(db, capability_id)
        db.commit()
        return _ok({"capability_id": capability_id, "created": False, "pool_queue_id": pool.queue_id, "pool_routing_policy": pool.routing_policy})
    capability = Capability(
        capability_id=capability_id,
        name=request.name,
        version=request.version,
        image_ref=request.image_ref,
        model_ref=request.model_ref,
        resource_tier=request.resource_tier,
        permission_profile=request.permission_profile,
        queue_mode=request.queue_mode,
        runtime_requirements_json=request.runtime_requirements,
    )
    db.add(capability)
    db.flush()
    pool = _ensure_capability_pool(db, capability_id)
    _create_event(db, event_type="capability.seeded", body={"capability_id": capability_id, "pool_queue_id": pool.queue_id})
    db.commit()
    return _ok({"capability_id": capability_id, "created": True, "pool_queue_id": pool.queue_id, "pool_routing_policy": pool.routing_policy})


@router.get("/capability-pools")
def list_capability_pools(db: Session = Depends(get_db)) -> dict:
    pools = db.scalars(select(CapabilityPool).order_by(CapabilityPool.capability_id.asc())).all()
    return _ok({"items": [{"capability_id": p.capability_id, "queue_id": p.queue_id, "routing_policy": p.routing_policy} for p in pools]})


@router.post("/nudges")
def create_nudge(request: CreateNudgeRequest, db: Session = Depends(get_db)) -> dict:
    nudge = _enqueue_nudge(db, target_agent_id=request.target_agent_id, priority=request.priority, source=request.source, payload=request.payload, job_id=request.job_id)
    db.commit()
    return _ok(_serialize(nudge, ("nudge_id", "target_agent_id", "priority", "source", "status", "created_at")))


@router.get("/nudges/next")
def next_nudge(target_agent_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    nudge = db.scalars(
        select(Nudge).where(Nudge.target_agent_id == target_agent_id, Nudge.status == "pending")
        .order_by(Nudge.priority.asc(), Nudge.created_at.asc()).limit(1)
    ).first()
    if nudge is None:
        return _ok(None)
    nudge.status = "delivered"
    nudge.delivered_at = utc_now()
    db.commit()
    return _ok(_serialize(nudge, ("nudge_id", "target_agent_id", "priority", "source", "payload", "job_id", "status", "created_at", "delivered_at")))


@router.get("/nudges")
def list_nudges(target_agent_id: str | None = Query(default=None), status: str | None = Query(default=None), limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    query = select(Nudge)
    if target_agent_id is not None:
        query = query.where(Nudge.target_agent_id == target_agent_id)
    if status is not None:
        query = query.where(Nudge.status == status)
    nudges = db.scalars(query.order_by(Nudge.priority.asc(), Nudge.created_at.asc()).limit(limit)).all()
    return _ok({"items": [_serialize(n, ("nudge_id", "target_agent_id", "priority", "source", "status", "job_id", "created_at", "delivered_at")) for n in nudges]})


@router.get("/queue/deliveries")
def list_queue_deliveries(
    db: Session = Depends(get_db),
    state: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    target_queue: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(QueueDeliveryRecord)
    if state is not None:
        query = query.where(QueueDeliveryRecord.state == state)
    if job_id is not None:
        query = query.where(QueueDeliveryRecord.job_id == job_id)
    if target_queue is not None:
        query = query.where(QueueDeliveryRecord.target_queue == target_queue)
    query = _apply_created_cursor(query, QueueDeliveryRecord, cursor)
    rows = db.scalars(query.order_by(QueueDeliveryRecord.created_at.desc(), QueueDeliveryRecord.delivery_id.desc()).limit(limit + 1)).all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        tail = page_rows[-1]
        next_cursor = _encode_cursor({"created_at": tail.created_at.isoformat(), "id": tail.delivery_id})
    return _ok(_page(
        [_serialize(row, ("delivery_id", "job_id", "target_queue", "state", "delivery_attempt", "available_at", "last_delivered_at", "acked_at", "dead_lettered_at", "created_at")) for row in page_rows],
        limit=limit, next_cursor=next_cursor,
    ))
