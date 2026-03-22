"""Observability data computation (alerts, triage)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.config import settings
from agp.enums import HealthStatus, JobStatus, LeaseStatus
from agp.models import Job, Lease, QueueDeliveryRecord, Runtime


def _current_alerts_payload(db: Session) -> dict:
    expired_leases = int(
        db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.EXPIRED.value)) or 0
    )
    dead_lettered_deliveries = int(
        db.scalar(select(func.count()).select_from(QueueDeliveryRecord).where(QueueDeliveryRecord.state == "dead_lettered"))
        or 0
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

    return {
        "items": alerts,
        "counts": {
            "active": len(alerts),
            "expired_leases": expired_leases,
            "dead_lettered_deliveries": dead_lettered_deliveries,
            "unreachable_runtimes": unreachable_runtimes,
        },
    }
