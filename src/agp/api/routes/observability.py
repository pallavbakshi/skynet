"""Observability route handlers."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.api.helpers import (
    _apply_created_cursor,
    _count_by,
    _decode_cursor,
    _duration_seconds,
    _encode_cursor,
    _ok,
    _page,
    _prom_metric,
    _serialize,
)
from agp.config import settings
from agp.db import get_db
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.logs import read_tail_jsonl_family
from agp.models import (
    Agent,
    Capability,
    Event,
    HealthRecord,
    Job,
    Lease,
    QueueDeliveryRecord,
    Run,
    Runtime,
    SystemMetadata,
    utc_now,
)
from agp.services._helpers import _control_plane_log_path, _require_job
from agp.services.observability import _current_alerts_payload

router = APIRouter()


@router.get("/observability/summary", deprecated=True)
def observability_summary(db: Session = Depends(get_db)) -> dict:
    latest_event_seq = int(db.scalar(select(func.max(Event.event_seq))) or 0)
    queue_depth = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED.value)) or 0)
    active_leases = int(db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.ACTIVE.value)) or 0)
    dead_lettered_deliveries = int(db.scalar(select(func.count()).select_from(QueueDeliveryRecord).where(QueueDeliveryRecord.state == "dead_lettered")) or 0)
    return _ok({
        "jobs": _count_by(db, Job, Job.status, [s.value for s in JobStatus]),
        "runtimes": _count_by(db, Runtime, Runtime.status, [s.value for s in RuntimeStatus]),
        "agents": _count_by(db, Agent, Agent.status, [s.value for s in AgentStatus]),
        "leases": {
            "active": active_leases,
            "expired": int(db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.EXPIRED.value)) or 0),
        },
        "queue": {"depth": queue_depth, "dead_lettered_deliveries": dead_lettered_deliveries},
        "events": {"latest_event_seq": latest_event_seq},
    })


@router.get("/observability/alerts", deprecated=True)
def observability_alerts(db: Session = Depends(get_db)) -> dict:
    return _ok(_current_alerts_payload(db))


@router.post("/observability/alerts/dispatch")
def observability_dispatch_alerts(db: Session = Depends(get_db)) -> dict:
    if not settings.observability_alert_webhook_url:
        raise HTTPException(status_code=409, detail="observability alert webhook is not configured")
    payload = {
        "source": "agp",
        "kind": "observability_alerts",
        "generated_at": utc_now().isoformat(),
        **_current_alerts_payload(db),
    }
    with httpx.Client(timeout=settings.observability_alert_webhook_timeout_seconds) as client:
        response = client.post(settings.observability_alert_webhook_url, json=payload)
        response.raise_for_status()
    return _ok({"delivered": True, "target": settings.observability_alert_webhook_url, "alert_count": len(payload["items"])})


@router.get("/observability/metrics", deprecated=True)
def observability_metrics(db: Session = Depends(get_db)) -> PlainTextResponse:
    lines = ["# HELP agp_jobs_total Jobs grouped by status.", "# TYPE agp_jobs_total gauge"]
    for status, count in _count_by(db, Job, Job.status, [s.value for s in JobStatus]).items():
        lines.append(_prom_metric("agp_jobs_total", count, {"status": status}))
    lines.extend(["# HELP agp_runs_total Runs grouped by status.", "# TYPE agp_runs_total gauge"])
    for status, count in _count_by(db, Run, Run.status, [s.value for s in RunStatus]).items():
        lines.append(_prom_metric("agp_runs_total", count, {"status": status}))
    lines.extend(["# HELP agp_agents_total Agents grouped by status.", "# TYPE agp_agents_total gauge"])
    for status, count in _count_by(db, Agent, Agent.status, [s.value for s in AgentStatus]).items():
        lines.append(_prom_metric("agp_agents_total", count, {"status": status}))
    lines.extend(["# HELP agp_runtimes_total Runtimes grouped by status.", "# TYPE agp_runtimes_total gauge"])
    for status, count in _count_by(db, Runtime, Runtime.status, [s.value for s in RuntimeStatus]).items():
        lines.append(_prom_metric("agp_runtimes_total", count, {"status": status}))
    lines.extend(["# HELP agp_runtime_health_total Runtimes grouped by health status.", "# TYPE agp_runtime_health_total gauge"])
    for status, count in _count_by(db, Runtime, Runtime.health_status, [s.value for s in HealthStatus]).items():
        lines.append(_prom_metric("agp_runtime_health_total", count, {"status": status}))
    lines.extend(["# HELP agp_leases_total Leases grouped by status.", "# TYPE agp_leases_total gauge"])
    for status, count in _count_by(db, Lease, Lease.status, [s.value for s in LeaseStatus]).items():
        lines.append(_prom_metric("agp_leases_total", count, {"status": status}))
    lines.extend(["# HELP agp_queue_deliveries_total Queue deliveries grouped by state.", "# TYPE agp_queue_deliveries_total gauge"])
    for state in ("pending", "delivered", "acked", "dead_lettered"):
        count = int(db.scalar(select(func.count()).select_from(QueueDeliveryRecord).where(QueueDeliveryRecord.state == state)) or 0)
        lines.append(_prom_metric("agp_queue_deliveries_total", count, {"state": state}))
    lines.extend(["# HELP agp_observability_active_alerts Current active alerts grouped by code.", "# TYPE agp_observability_active_alerts gauge"])
    for item in _current_alerts_payload(db)["items"]:
        lines.append(_prom_metric("agp_observability_active_alerts", 1, {"code": item["code"], "severity": item["severity"]}))
    latest_event_seq = int(db.scalar(select(func.max(Event.event_seq))) or 0)
    lines.extend(["# HELP agp_events_latest_seq Latest allocated event sequence.", "# TYPE agp_events_latest_seq gauge", _prom_metric("agp_events_latest_seq", latest_event_seq)])
    total_completed = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.COMPLETED.value)) or 0)
    total_failed = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.FAILED.value)) or 0)
    lines.extend([
        "# HELP agp_jobs_completed_total Total jobs that reached completed state.", "# TYPE agp_jobs_completed_total counter", _prom_metric("agp_jobs_completed_total", total_completed),
        "# HELP agp_jobs_failed_total Total jobs that reached failed state.", "# TYPE agp_jobs_failed_total counter", _prom_metric("agp_jobs_failed_total", total_failed),
    ])
    interrupt_count = int(db.scalar(select(func.count()).select_from(Event).where(Event.event_type == "job.interrupt_requested")) or 0)
    lines.extend(["# HELP agp_interrupts_total Total job interrupt requests.", "# TYPE agp_interrupts_total counter", _prom_metric("agp_interrupts_total", interrupt_count)])
    queue_depth = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED.value)) or 0)
    lines.extend(["# HELP agp_queue_depth Current number of queued jobs.", "# TYPE agp_queue_depth gauge", _prom_metric("agp_queue_depth", queue_depth)])
    active_leases = int(db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.ACTIVE.value)) or 0)
    lines.extend(["# HELP agp_leases_active Current active leases.", "# TYPE agp_leases_active gauge", _prom_metric("agp_leases_active", active_leases)])
    last_backup_at = db.scalar(select(SystemMetadata.value).where(SystemMetadata.key == "last_backup_at"))
    if last_backup_at:
        try:
            from datetime import datetime as _dt
            backup_ts = _dt.fromisoformat(last_backup_at)
            backup_age = (utc_now() - backup_ts).total_seconds()
        except Exception:
            backup_age = -1
    else:
        backup_age = -1
    lines.extend(["# HELP agp_backup_age_seconds Seconds since last successful backup (-1 if unknown).", "# TYPE agp_backup_age_seconds gauge", _prom_metric("agp_backup_age_seconds", backup_age)])
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/observability/jobs/{job_id}/trace")
def observability_job_trace(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    events = db.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.event_seq.asc())).all()
    if not events:
        raise HTTPException(status_code=404, detail=f"no events found for job: {job_id}")
    first_by_type: dict[str, Event] = {}
    for event in events:
        first_by_type.setdefault(event.event_type, event)
    trace_started_at = events[0].created_at
    trace_finished_at = events[-1].created_at if job.status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value} else None
    accepted_at = first_by_type.get("job.accepted", events[0]).created_at
    queued_at = first_by_type.get("job.queued").created_at if "job.queued" in first_by_type else None
    lease_at = first_by_type.get("lease.acquired").created_at if "lease.acquired" in first_by_type else None
    run_at = first_by_type.get("run.running").created_at if "run.running" in first_by_type else None
    terminal_event = first_by_type.get("job.completed") or first_by_type.get("job.failed") or first_by_type.get("job.cancelled")
    terminal_at = terminal_event.created_at if terminal_event is not None else None
    return _ok({
        "job": _serialize(job, ("job_id", "message_id", "target_agent_id", "target_queue", "status", "retry_count", "max_retries", "latest_run_id", "result_artifact_id", "created_at", "updated_at")),
        "runs": [_serialize(run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status", "started_at", "finished_at")) for run in db.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.attempt.asc())).all()],
        "timeline": [{"event_id": e.event_id, "event_seq": e.event_seq, "event_type": e.event_type, "created_at": e.created_at, "run_id": e.run_id, "agent_id": e.agent_id, "runtime_id": e.runtime_id, "body": e.body_json} for e in events],
        "trace": {
            "started_at": trace_started_at,
            "finished_at": trace_finished_at,
            "durations_seconds": {
                "accepted_to_queued": _duration_seconds(accepted_at, queued_at),
                "queued_to_lease": _duration_seconds(queued_at, lease_at),
                "lease_to_running": _duration_seconds(lease_at, run_at),
                "running_to_terminal": _duration_seconds(run_at, terminal_at),
                "total": _duration_seconds(trace_started_at, trace_finished_at),
            },
        },
    })


@router.get("/observability/logs/control-plane", deprecated=True)
def observability_control_plane_logs(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return _ok({"items": read_tail_jsonl_family(_control_plane_log_path(), limit=limit), "source": str(_control_plane_log_path()), "limit": limit})


@router.get("/observability/logs/runtimes/{runtime_id}", deprecated=True)
def observability_runtime_logs(runtime_id: str, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    path = settings.log_root / f"runtime-{runtime_id}.jsonl"
    return _ok({"items": read_tail_jsonl_family(path, limit=limit), "source": str(path), "runtime_id": runtime_id, "limit": limit})


_AUDIT_EVENT_TYPES = frozenset({
    "agent.registered", "agent.idle", "agent.deleted", "agent.draining",
    "job.interrupt_requested", "job.cancelled",
    "runtime.registered", "runtime.offline",
    "handoff.created",
    "token.operator_rotated", "token.runtime_rotated",
})


@router.get("/observability/audit", deprecated=True)
def observability_audit(db: Session = Depends(get_db), limit: int = Query(default=100, ge=1, le=500), cursor: str | None = Query(default=None)) -> dict:
    query = select(Event).where(Event.event_type.in_(_AUDIT_EVENT_TYPES))
    query = _apply_created_cursor(query, Event, cursor)
    rows = db.scalars(query.order_by(Event.event_seq.desc()).limit(limit + 1)).all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        tail = page_rows[-1]
        next_cursor = _encode_cursor({"created_at": tail.created_at.isoformat(), "id": tail.event_id})
    return _ok(_page(
        [{"event_id": e.event_id, "event_seq": e.event_seq, "event_type": e.event_type, "agent_id": e.agent_id, "runtime_id": e.runtime_id, "job_id": e.job_id, "body": e.body_json, "created_at": e.created_at} for e in page_rows],
        limit=limit, next_cursor=next_cursor,
    ))


@router.get("/observability/triage", deprecated=True)
def observability_triage(db: Session = Depends(get_db)) -> dict:
    active_leases = db.scalars(select(Lease).where(Lease.status == LeaseStatus.ACTIVE.value)).all()
    active_by_runtime: dict[str, list[dict]] = {}
    for lease in active_leases:
        run = db.get(Run, lease.run_id)
        job = db.get(Job, run.job_id) if run else None
        entry = {"lease_id": lease.lease_id, "run_id": lease.run_id, "agent_id": lease.agent_id, "job_id": run.job_id if run else None, "job_status": job.status if job else None, "expires_at": lease.expires_at.isoformat() if lease.expires_at else None}
        active_by_runtime.setdefault(lease.runtime_id, []).append(entry)
    recent_failures = db.scalars(select(Job).where(Job.status == JobStatus.FAILED.value).order_by(Job.updated_at.desc()).limit(20)).all()
    failure_items = [_serialize(job, ("job_id", "target_agent_id", "target_queue", "status", "retry_count", "latest_run_id", "updated_at")) for job in recent_failures]
    problem_runtimes = db.scalars(select(Runtime).where(Runtime.status.in_(["offline", "degraded"]))).all()
    stale_items = [_serialize(rt, ("runtime_id", "hostname", "status", "health_status", "last_heartbeat_at")) for rt in problem_runtimes]
    # Agent summary: count by status (agents are ephemeral, capabilities self-declared)
    idle_agents = int(db.scalar(select(func.count()).select_from(Agent).where(Agent.status == AgentStatus.IDLE.value)) or 0)
    busy_agents = int(db.scalar(select(func.count()).select_from(Agent).where(Agent.status == AgentStatus.BUSY.value)) or 0)
    agent_summary = {"idle": idle_agents, "busy": busy_agents, "total": idle_agents + busy_agents}
    return _ok({"active_jobs_by_runtime": active_by_runtime, "recent_failures": failure_items, "stale_runtimes": stale_items, "agents": agent_summary})


@router.get("/observability/health-records", deprecated=True)
def list_health_records(entity_type: str | None = Query(default=None), entity_id: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)) -> dict:
    query = select(HealthRecord)
    if entity_type is not None:
        query = query.where(HealthRecord.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(HealthRecord.entity_id == entity_id)
    records = db.scalars(query.order_by(HealthRecord.observed_at.desc()).limit(limit)).all()
    return _ok({"items": [{"entity_type": r.entity_type, "entity_id": r.entity_id, "health_status": r.health_status, "reason": r.reason, "observed_at": r.observed_at.isoformat() if r.observed_at else None} for r in records]})
