"""Shared domain helpers used across services and routes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session

from agp.artifact_store import get_artifact_store
from agp.config import settings
from agp.db import get_db
from agp.enums import ArtifactKind, HealthStatus, RuntimeStatus
from agp.logs import append_jsonl_log
from agp.models import (
    Agent,
    Capability,
    HealthRecord,
    Job,
    Nudge,
    Runtime,
    SystemMetadata,
    utc_now,
)
from agp.queue_backend import get_queue_backend
from agp.services.exceptions import BadRequestError, ConflictError, NotFoundError


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _queue_backend():
    return get_queue_backend(settings.queue_backend)


def _artifact_store():
    return get_artifact_store(settings.artifact_backend, settings.artifact_root)


def _control_plane_log_path() -> Path:
    return settings.log_root / "control-plane.jsonl"


def _append_control_plane_log(entry: dict) -> None:
    append_jsonl_log(
        _control_plane_log_path(),
        entry,
        rotation_bytes=settings.observability_log_rotation_bytes,
    )


def _record_health_transition(
    db: Session,
    *,
    entity_type: str,
    entity_id: str,
    health_status: str,
    reason: str,
) -> None:
    db.add(HealthRecord(
        entity_type=entity_type,
        entity_id=entity_id,
        health_status=health_status,
        reason=reason,
    ))


def _require_capability(db: Session, capability_id: str) -> Capability:
    capability = db.get(Capability, capability_id)
    if capability is None:
        raise NotFoundError(f"capability not found: {capability_id}")
    return capability


def _require_agent(db: Session, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    return agent


def _require_runtime(db: Session, runtime_id: str) -> Runtime:
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        raise NotFoundError(f"runtime not found: {runtime_id}")
    return runtime


def _require_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"job not found: {job_id}")
    return job


def _system_metadata_value(db: Session, key: str) -> str | None:
    row = db.get(SystemMetadata, key)
    return row.value if row is not None else None


def _set_system_metadata_value(db: Session, key: str, value: str | None) -> None:
    row = db.get(SystemMetadata, key)
    if value is None:
        if row is not None:
            db.delete(row)
        return
    now = utc_now()
    if row is None:
        db.add(SystemMetadata(key=key, value=value, updated_at=now))
        return
    row.value = value
    row.updated_at = now


def _load_persisted_auth_settings() -> None:
    from sqlalchemy.exc import OperationalError

    db = next(get_db())
    try:
        try:
            operator_legacy = _system_metadata_value(db, "operator_bearer_token")
            operator_roles = _system_metadata_value(db, "operator_token_roles_json")
            runtime_legacy = _system_metadata_value(db, "runtime_bearer_token")
            runtime_active = _system_metadata_value(db, "runtime_active_tokens_json")
        except OperationalError:
            return
        if operator_legacy is not None:
            settings.operator_bearer_token = operator_legacy or None
        if operator_roles is not None:
            settings.operator_token_roles_json = dict(json.loads(operator_roles))
        if runtime_legacy is not None:
            settings.runtime_bearer_token = runtime_legacy or None
        if runtime_active is not None:
            settings.runtime_active_tokens_json = list(json.loads(runtime_active))
    finally:
        db.close()


def _get_upgrade_status(db: Session) -> dict:
    from agp.db import current_release_version

    release_version = _system_metadata_value(db, "release_version") or current_release_version()
    schema_version = _system_metadata_value(db, "schema_version") or "unknown"
    previous_release_version = _system_metadata_value(db, "previous_release_version")
    previous_schema_version = _system_metadata_value(db, "previous_schema_version")
    return {
        "release_version": release_version,
        "schema_version": schema_version,
        "previous_release_version": previous_release_version,
        "previous_schema_version": previous_schema_version,
        "package_version": current_release_version(),
        "rollback_target_release_version": previous_release_version,
        "rollback_target_schema_version": previous_schema_version,
    }


def _parse_release_version(value: str) -> tuple[int, int, int]:
    normalized = value.strip()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    parts = normalized.split(".")
    if len(parts) < 2 or len(parts) > 3:
        raise BadRequestError(f"invalid release version: {value}")
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise BadRequestError(f"invalid release version: {value}") from exc
    return major, minor, patch


def _assert_supported_runtime_skew(db: Session, runtime_release_version: str) -> None:
    control_plane_release = _get_upgrade_status(db)["release_version"]
    cp_major, cp_minor, _ = _parse_release_version(control_plane_release)
    rt_major, rt_minor, _ = _parse_release_version(runtime_release_version)
    if rt_major != cp_major:
        raise ConflictError("unsupported major-version skew")
    if rt_minor > cp_minor:
        raise ConflictError("runtime release is ahead of control plane")
    if cp_minor - rt_minor > 1:
        raise ConflictError("runtime release is too far behind control plane")


def _queue_for_target(target_type: str, target_id: str) -> str:
    if target_type == "agent":
        return f"agent:{target_id}"
    if target_type == "capability":
        return f"capability:{target_id}"
    raise BadRequestError(f"unsupported target type: {target_type}")


def _capability_queue_for(db: Session, capability_name: str) -> str:
    """Return a queue ID for capability-based best-effort routing."""
    return f"capability:{capability_name}"


_NUDGE_SEP = "========================================="


def _format_job_nudge(job: Job, status_label: str) -> str:
    result_line = ""
    if job.result_artifact_id:
        result_line = f"RESULT:       artifact {job.result_artifact_id}\n"
    return (
        f"{_NUDGE_SEP}\n"
        f"[SYSTEM NUDGE] Async Task Completed\n"
        f"{_NUDGE_SEP}\n"
        f"JOB_ID:       {job.job_id}\n"
        f"AGENT:        {job.target_agent_id or 'unknown'}\n"
        f"STATUS:       {status_label}\n"
        f"{result_line}\n"
        f"ACTION REQUIRED: Please consume the result and determine your next step."
    )


def _enqueue_nudge(
    db: Session,
    *,
    target_agent_id: str,
    priority: int,
    source: str,
    payload: str,
    job_id: str | None = None,
) -> Nudge:
    nudge = Nudge(
        nudge_id=_new_id("ndg"),
        target_agent_id=target_agent_id,
        priority=priority,
        source=source,
        payload=payload,
        job_id=job_id,
        status="pending",
    )
    db.add(nudge)
    return nudge


def _write_control_plane_artifact(*, job_id: str, name: str, content: str, role: str | None = None) -> SimpleNamespace:
    role_map = {
        "prompt.txt": ArtifactKind.PROMPT.value,
        "transcript.txt": ArtifactKind.TRANSCRIPT_LOG.value,
        "exec.txt": ArtifactKind.EXEC_LOG.value,
        "result.txt": ArtifactKind.RESULT.value,
        "failure.txt": ArtifactKind.FAILURE_EVIDENCE.value,
    }
    resolved_role = role or role_map.get(name, "attachment")
    stored = _artifact_store().write_text(
        namespace="control-plane",
        job_id=job_id,
        name=name,
        content=content,
        role=resolved_role,
    )
    return SimpleNamespace(
        role=stored.role,
        storage_ref=stored.storage_ref,
        content_type=stored.content_type,
        checksum=stored.checksum,
        size_bytes=stored.size_bytes,
    )


def _ensure_inline_runtime(db: Session) -> Runtime:
    from agp.services.events import _create_event

    runtime = db.get(Runtime, "rtm_inline")
    if runtime is None:
        runtime = Runtime(
            runtime_id="rtm_inline",
            hostname="control-plane",
            status=RuntimeStatus.IDLE.value,
            health_status=HealthStatus.HEALTHY.value,
            metadata_json={"kind": "inline"},
            last_seen_at=utc_now(),
            last_heartbeat_at=utc_now(),
        )
        db.add(runtime)
        db.flush()
        _create_event(db, runtime_id=runtime.runtime_id, event_type="runtime.registered", body={"hostname": runtime.hostname})
    return runtime
