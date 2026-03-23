"""Runtime domain operations — register."""

from __future__ import annotations

from sqlalchemy.orm import Session

from agp.enums import HealthStatus
from agp.models import Runtime, utc_now
from agp.services._helpers import _assert_supported_runtime_skew, _new_id, _record_health_transition
from agp.services.events import _create_event


def register_runtime_service(
    db: Session,
    *,
    runtime_id: str | None,
    hostname: str,
    release_version: str,
    metadata: dict,
) -> Runtime:
    _assert_supported_runtime_skew(db, release_version)
    resolved_id = runtime_id or _new_id("rtm")
    runtime = db.get(Runtime, resolved_id)
    if runtime is None:
        runtime = Runtime(
            runtime_id=resolved_id,
            hostname=hostname,
            release_version=release_version,
            status="idle",
            health_status=HealthStatus.HEALTHY.value,
            metadata_json=metadata,
            last_seen_at=utc_now(),
            last_heartbeat_at=utc_now(),
        )
        db.add(runtime)
        db.flush()
        _record_health_transition(
            db, entity_type="runtime", entity_id=resolved_id,
            health_status=HealthStatus.HEALTHY.value, reason="registered",
        )
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.registered",
            body={"hostname": runtime.hostname, "release_version": runtime.release_version},
        )
    else:
        previous_health = runtime.health_status
        runtime.hostname = hostname
        runtime.release_version = release_version
        runtime.metadata_json = metadata
        runtime.status = "idle"
        runtime.health_status = HealthStatus.HEALTHY.value
        runtime.last_seen_at = utc_now()
        runtime.last_heartbeat_at = utc_now()
        if previous_health != HealthStatus.HEALTHY.value:
            _record_health_transition(
                db, entity_type="runtime", entity_id=resolved_id,
                health_status=HealthStatus.HEALTHY.value, reason="re_registered",
            )
    db.commit()
    return runtime
