"""Observability data computation (alerts, triage)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.config import settings
from agp.enums import HealthStatus, JobStatus
from agp.models import Agent, Event, Job, QueueDeliveryRecord, Runtime, utc_now
from agp.queue_backend import agent_queue_targets, queue_backlogs_by_target_queue, queue_oldest_queued_at


def _current_alerts_payload(db: Session) -> dict:
    _alert_window = utc_now() - timedelta(seconds=settings.runtime_stale_timeout_seconds * 2)
    expired_leases = int(
        db.scalar(
            select(func.count()).select_from(Event).where(
                Event.event_type == "lease.expired",
                Event.created_at >= _alert_window,
            )
        ) or 0
    )
    dead_lettered_deliveries = int(
        db.scalar(
            select(func.count()).select_from(QueueDeliveryRecord).where(
                QueueDeliveryRecord.state == "dead_lettered",
                QueueDeliveryRecord.dead_lettered_at >= _alert_window,
            )
        ) or 0
    )
    global_queue_depth = int(
        db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED.value)) or 0
    )
    unreachable_runtimes = int(
        db.scalar(select(func.count()).select_from(Runtime).where(Runtime.health_status == HealthStatus.UNREACHABLE.value))
        or 0
    )

    terminal_jobs = db.scalars(
        select(Job.status)
        .where(Job.status.in_((JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)))
        .order_by(Job.updated_at.desc())
        .limit(20)
    ).all()
    failed_terminal_jobs = sum(1 for status in terminal_jobs if status == JobStatus.FAILED.value)
    failure_rate = (failed_terminal_jobs / len(terminal_jobs)) if terminal_jobs else 0.0
    agent_queue_depths = []
    stale_queue_agents = []
    agents = db.scalars(select(Agent).order_by(Agent.agent_id.asc())).all()
    target_queues = [agent_queue_targets(agent_id=agent.agent_id)[0] for agent in agents]
    backlog_by_queue = queue_backlogs_by_target_queue(db, target_queues=target_queues)
    for agent in agents:
        backlog = backlog_by_queue.get(agent_queue_targets(agent_id=agent.agent_id)[0], {"queue_depth": 0, "oldest_queued_at": None})
        oldest_queued_at = backlog["oldest_queued_at"]
        oldest_queue_age_seconds = (
            max(0.0, (utc_now() - oldest_queued_at).total_seconds()) if oldest_queued_at is not None else None
        )
        item = {
            "agent_id": agent.agent_id,
            "queue_depth": backlog["queue_depth"],
            "oldest_queued_at": oldest_queued_at.isoformat() if oldest_queued_at is not None else None,
            "oldest_queue_age_seconds": oldest_queue_age_seconds,
        }
        agent_queue_depths.append(item)
        if oldest_queue_age_seconds is not None and oldest_queue_age_seconds >= settings.observability_stale_queue_age_seconds:
            stale_queue_agents.append(item)
    global_oldest_queued_at = queue_oldest_queued_at(db)
    global_oldest_queue_age_seconds = (
        max(0.0, (utc_now() - global_oldest_queued_at).total_seconds()) if global_oldest_queued_at is not None else None
    )
    queue_depth_breaches = [
        item
        for item in agent_queue_depths
        if item["queue_depth"] >= settings.observability_queue_depth_alert_threshold
    ]
    direct_queue_depth_total = sum(int(item["queue_depth"]) for item in agent_queue_depths)
    shared_queue_depth = max(0, global_queue_depth - direct_queue_depth_total)

    alerts: list[dict] = []
    if unreachable_runtimes >= settings.observability_unreachable_runtime_threshold:
        alerts.append(
            {
                "code": "runtime_unreachable",
                "severity": "critical",
                "status": "active",
                "evidence": {
                    "unreachable_runtimes": unreachable_runtimes,
                    "threshold": settings.observability_unreachable_runtime_threshold,
                },
            }
        )
    if expired_leases >= settings.observability_expired_lease_alert_threshold:
        alerts.append(
            {
                "code": "heartbeat_loss_spike",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "expired_leases": expired_leases,
                    "threshold": settings.observability_expired_lease_alert_threshold,
                },
            }
        )
        alerts.append(
            {
                "code": "repeated_fencing_events",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "expired_leases": expired_leases,
                    "threshold": settings.observability_expired_lease_alert_threshold,
                },
            }
        )
    if dead_lettered_deliveries >= settings.observability_dead_letter_alert_threshold:
        alerts.append(
            {
                "code": "queue_dead_lettering",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "dead_lettered_deliveries": dead_lettered_deliveries,
                    "threshold": settings.observability_dead_letter_alert_threshold,
                },
            }
        )
    if (
        len(terminal_jobs) >= settings.observability_terminal_failure_sample_size
        and failure_rate >= settings.observability_terminal_failure_rate_threshold
    ):
        alerts.append(
            {
                "code": "rising_terminal_failure_rate",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "sample_size": len(terminal_jobs),
                    "failed_terminal_jobs": failed_terminal_jobs,
                    "failure_rate": round(failure_rate, 4),
                    "threshold": settings.observability_terminal_failure_rate_threshold,
                },
            }
        )
    if queue_depth_breaches:
        alerts.append(
            {
                "code": "queue_depth_high",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "threshold": settings.observability_queue_depth_alert_threshold,
                    "affected_agents": queue_depth_breaches,
                    "max_queue_depth": max(item["queue_depth"] for item in queue_depth_breaches),
                },
            }
        )
    if shared_queue_depth >= settings.observability_queue_depth_alert_threshold:
        alerts.append(
            {
                "code": "queue_depth_global_high",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "threshold": settings.observability_queue_depth_alert_threshold,
                    "queue_depth_total": global_queue_depth,
                    "direct_queue_depth_total": direct_queue_depth_total,
                    "shared_queue_depth": shared_queue_depth,
                },
            }
        )
    if global_oldest_queue_age_seconds is not None and global_oldest_queue_age_seconds >= settings.observability_stale_queue_age_seconds:
        alerts.append(
            {
                "code": "stale_queued_jobs",
                "severity": "warning",
                "status": "active",
                "evidence": {
                    "threshold_seconds": settings.observability_stale_queue_age_seconds,
                    "affected_agents": stale_queue_agents,
                    "max_oldest_queue_age_seconds": global_oldest_queue_age_seconds,
                    "global_oldest_queued_at": global_oldest_queued_at.isoformat(),
                },
            }
        )

    return {
        "items": alerts,
        "counts": {
            "active": len(alerts),
            "expired_leases": expired_leases,
            "dead_lettered_deliveries": dead_lettered_deliveries,
            "unreachable_runtimes": unreachable_runtimes,
            "queue_depth_breaches": len(queue_depth_breaches),
            "global_queue_depth_breaches": int(
                shared_queue_depth >= settings.observability_queue_depth_alert_threshold
            ),
            "stale_queue_agents": len(stale_queue_agents),
            "stale_queued_work": int(
                global_oldest_queue_age_seconds is not None
                and global_oldest_queue_age_seconds >= settings.observability_stale_queue_age_seconds
            ),
        },
    }
