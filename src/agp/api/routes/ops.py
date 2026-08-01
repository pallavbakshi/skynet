"""Ops routes — operator-facing endpoints for runtime infrastructure management.

Wraps existing observability/system endpoints under /ops/* namespace.
These replace the agent-facing /runtimes, /observability/*, and /system/* routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

# Re-use existing route implementations
from agp.api.routes.observability import (
    list_health_records,
    observability_alerts,
    observability_audit,
    observability_control_plane_logs,
    observability_dispatch_alerts,
    observability_job_trace,
    observability_metrics,
    observability_runtime_logs,
    observability_summary,
    observability_triage,
)
from agp.api.routes.runtimes import get_runtime_detail, list_runtimes
from agp.db import get_db

router = APIRouter(prefix="/ops", tags=["ops"])


# ── Runtime management ──


@router.get("/runtimes")
def ops_list_runtimes(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    health_status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """List managed runtimes."""
    return list_runtimes(db=db, status=status, health_status=health_status, cursor=cursor, limit=limit)


@router.get("/runtimes/{runtime_id}")
def ops_get_runtime(runtime_id: str, db: Session = Depends(get_db)) -> dict:
    """Get runtime details."""
    return get_runtime_detail(runtime_id=runtime_id, db=db)


@router.post("/runtimes/{runtime_id}/drain")
def ops_drain_runtime(runtime_id: str, db: Session = Depends(get_db)) -> dict:
    """Drain a runtime — stop accepting new work, finish current leases."""
    from agp.api.helpers import _ok
    from agp.enums import HealthStatus, RuntimeStatus
    from agp.models import utc_now
    from agp.services._helpers import _record_health_transition, _require_runtime
    from agp.services.events import _create_event

    runtime = _require_runtime(db, runtime_id)
    runtime.status = RuntimeStatus.DRAINING.value
    runtime.health_status = HealthStatus.DRAINING.value
    runtime.updated_at = utc_now()
    _record_health_transition(
        db, entity_type="runtime", entity_id=runtime.runtime_id,
        health_status=HealthStatus.DRAINING.value, reason="operator_drain",
    )
    _create_event(
        db, runtime_id=runtime.runtime_id, event_type="runtime.draining",
        body={"reason": "operator_initiated"},
    )
    db.commit()
    return _ok({"runtime_id": runtime.runtime_id, "status": runtime.status})


@router.post("/runtimes/{runtime_id}/restart")
def ops_restart_runtime(runtime_id: str, db: Session = Depends(get_db)) -> dict:
    """Signal a runtime restart — mark idle, reset health, emit event.

    The actual container restart is managed by the orchestrator (Docker/k8s).
    This endpoint resets the CP-side state so the runtime can re-register cleanly.
    """
    from agp.api.helpers import _ok
    from agp.enums import HealthStatus, LeaseStatus, RuntimeStatus
    from agp.models import Lease, utc_now
    from agp.services._helpers import _record_health_transition, _require_runtime
    from agp.services.events import _create_event
    from agp.services.exceptions import ConflictError

    runtime = _require_runtime(db, runtime_id)
    active_leases = db.scalar(
        select(func.count()).select_from(Lease).where(
            Lease.runtime_id == runtime.runtime_id,
            Lease.status == LeaseStatus.ACTIVE.value,
        )
    ) or 0
    if active_leases:
        raise ConflictError(f"runtime has active leases: {runtime.runtime_id}")
    runtime.status = RuntimeStatus.IDLE.value
    runtime.health_status = HealthStatus.HEALTHY.value
    runtime.updated_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    _record_health_transition(
        db, entity_type="runtime", entity_id=runtime.runtime_id,
        health_status=HealthStatus.HEALTHY.value, reason="operator_restart",
    )
    _create_event(
        db, runtime_id=runtime.runtime_id, event_type="runtime.restarted",
        body={"reason": "operator_initiated"},
    )
    db.commit()
    return _ok({"runtime_id": runtime.runtime_id, "status": runtime.status})


# ── Observability wrappers ──


@router.get("/health")
def ops_health(db: Session = Depends(get_db)) -> dict:
    """Infrastructure health summary."""
    return observability_summary(db=db)


@router.get("/alerts")
def ops_alerts(db: Session = Depends(get_db)) -> dict:
    """Active alerts."""
    return observability_alerts(db=db)


@router.get("/audit")
def ops_audit(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> dict:
    """Audit log."""
    return observability_audit(db=db, limit=limit, cursor=cursor)


@router.get("/metrics")
def ops_metrics(db: Session = Depends(get_db)) -> PlainTextResponse:
    """Prometheus metrics."""
    return observability_metrics(db=db)


@router.post("/alerts/dispatch")
def ops_dispatch_alerts(db: Session = Depends(get_db)) -> dict:
    """Dispatch alerts to configured webhook."""
    return observability_dispatch_alerts(db=db)


@router.get("/jobs/{job_id}/trace")
def ops_job_trace(job_id: str, db: Session = Depends(get_db)) -> dict:
    """Full execution trace for a job."""
    return observability_job_trace(job_id=job_id, db=db)


@router.get("/logs/control-plane")
def ops_control_plane_logs(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """Control plane logs."""
    return observability_control_plane_logs(limit=limit)


@router.get("/logs/runtimes/{runtime_id}")
def ops_runtime_logs(runtime_id: str, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """Runtime logs."""
    return observability_runtime_logs(runtime_id=runtime_id, limit=limit)


@router.get("/triage")
def ops_triage(db: Session = Depends(get_db)) -> dict:
    """Triage dashboard — active jobs, failures, stale runtimes."""
    return observability_triage(db=db)


@router.get("/health-records")
def ops_health_records(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """Health transition records."""
    return list_health_records(entity_type=entity_type, entity_id=entity_id, limit=limit, db=db)


@router.get("/upgrade-status")
def ops_upgrade_status(db: Session = Depends(get_db)) -> dict:
    """Persisted release and schema version state."""
    from agp.api.routes.admin import system_upgrade_status
    return system_upgrade_status(db=db)
