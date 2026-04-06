"""Runtime route handlers."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agp.api.helpers import _decode_cursor, _encode_cursor, _ok, _page, _serialize
from agp.db import get_db
from agp.enums import HealthStatus, LeaseStatus
from agp.models import Agent, Job, Lease, Run, Runtime
from agp.schemas import OkResponse, PeekResultRequest, RuntimeRegisterRequest, RuntimeResponse
from agp.services._helpers import _require_runtime
from agp.services.runtimes import register_runtime_service

router = APIRouter()


@router.post("/runtimes/register", response_model=OkResponse[RuntimeResponse])
def register_runtime(request: RuntimeRegisterRequest, db: Session = Depends(get_db)) -> dict:
    runtime = register_runtime_service(db, runtime_id=request.runtime_id, hostname=request.hostname, release_version=request.release_version, metadata=request.metadata)
    return _ok(
        _serialize(
            runtime,
            ("runtime_id", "hostname", "release_version", "status", "health_status", "metadata_json", "last_heartbeat_at"),
        )
    )


@router.get("/runtimes", deprecated=True)
def list_runtimes(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    health_status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Runtime)
    if status is not None:
        query = query.where(Runtime.status == status)
    if health_status is not None:
        query = query.where(Runtime.health_status == health_status)
    if cursor_payload is not None:
        created_at = datetime.fromisoformat(str(cursor_payload["created_at"]))
        runtime_id = str(cursor_payload["runtime_id"])
        query = query.where(
            (Runtime.created_at < created_at) | ((Runtime.created_at == created_at) & (Runtime.runtime_id < runtime_id))
        )
    runtimes = db.scalars(query.order_by(Runtime.created_at.desc(), Runtime.runtime_id.desc()).limit(limit + 1)).all()
    page_items = runtimes[:limit]
    next_cursor = None
    if len(runtimes) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"created_at": last.created_at.isoformat(), "runtime_id": last.runtime_id})
    items = []
    for runtime in page_items:
        data = _serialize(runtime, ("runtime_id", "agent_id", "hostname", "release_version", "status", "health_status", "metadata_json", "last_heartbeat_at"))
        active_leases = db.scalars(select(Lease).where(Lease.runtime_id == runtime.runtime_id, Lease.status == LeaseStatus.ACTIVE.value)).all()
        data["claimed_work"] = [{"lease_id": l.lease_id, "run_id": l.run_id, "agent_id": l.agent_id} for l in active_leases]
        data["active_run_count"] = len(active_leases)
        items.append(data)
    return _ok(_page(items, limit=limit, next_cursor=next_cursor))


@router.get("/runtimes/{runtime_id}", deprecated=True)
def get_runtime_detail(runtime_id: str, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, runtime_id)
    data = _serialize(runtime, ("runtime_id", "hostname", "release_version", "status", "health_status", "metadata_json", "last_heartbeat_at", "created_at"))
    active_leases = db.scalars(select(Lease).where(Lease.runtime_id == runtime.runtime_id, Lease.status == LeaseStatus.ACTIVE.value)).all()
    claimed_work = []
    for lease in active_leases:
        run = db.get(Run, lease.run_id)
        job = db.get(Job, run.job_id) if run else None
        claimed_work.append({
            "lease_id": lease.lease_id,
            "run_id": lease.run_id,
            "agent_id": lease.agent_id,
            "job_id": run.job_id if run else None,
            "job_status": job.status if job else None,
            "run_status": run.status if run else None,
            "fencing_token": lease.fencing_token,
            "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        })
    data["claimed_work"] = claimed_work
    data["active_run_count"] = len(active_leases)
    agents = db.scalars(select(Agent).join(Runtime, Runtime.agent_id == Agent.agent_id).where(Runtime.runtime_id == runtime.runtime_id)).all()
    data["agents"] = [_serialize(agent, ("agent_id", "capabilities", "status")) for agent in agents]
    return _ok(data)


@router.post("/runtimes/{runtime_id}/peek-result")
def submit_peek_result(runtime_id: str, body: PeekResultRequest, db: Session = Depends(get_db)) -> dict:
    """Runtime uploads captured terminal text for a peek request."""
    from agp.services.peek import peek_store
    _require_runtime(db, runtime_id)
    peek_store.submit_result(
        body.request_id,
        runtime_id=runtime_id,
        text=body.text,
        session_id=body.session_id,
        host_kind=body.host_kind,
    )
    return _ok({"accepted": True})


