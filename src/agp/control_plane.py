"""Control plane application MVP."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agp.config import settings
from agp.artifact_store import get_artifact_store
from agp.db import current_release_version, get_db
from agp.enums import AgentStatus, ArtifactKind, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.logs import append_jsonl_log, read_tail_jsonl_family
from agp.models import (
    Agent,
    Artifact,
    Capability,
    Event,
    EventJobLink,
    Handoff,
    HandoffArtifact,
    HandoffJob,
    IdempotencyKey,
    Job,
    JobArtifact,
    Lease,
    Message,
    QueueDeliveryRecord,
    Run,
    RunArtifact,
    Runtime,
    SystemMetadata,
    utc_now,
)
from agp.queue_backend import QueueDelivery, get_queue_backend
from agp.schemas import (
    AgentDownRequest,
    AgentUpRequest,
    CancelRunRequest,
    ClaimRunRequest,
    CompleteRunRequest,
    FailRunRequest,
    HealthResponse,
    HeartbeatRequest,
    HandoffRequest,
    ProgressRequest,
    RecoveryRequest,
    RotateOperatorTokensRequest,
    RotateRuntimeTokensRequest,
    RuntimeRegisterRequest,
    SendMessageRequest,
)

router = APIRouter()
_event_seq_lock = Lock()
_event_seq_counter: int | None = None
def _queue_backend():
    return get_queue_backend(settings.queue_backend)


def _artifact_store():
    return get_artifact_store(settings.artifact_backend, settings.artifact_root)


def _ok(data: object) -> dict:
    return {"ok": True, "data": data}


def _page(items: list[dict], *, limit: int, next_cursor: str | None) -> dict:
    return {
        "items": items,
        "page": {"limit": limit, "next_cursor": next_cursor, "has_more": next_cursor is not None},
    }


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 6)


def _control_plane_log_path() -> Path:
    return settings.log_root / "control-plane.jsonl"


def _append_control_plane_log(entry: dict) -> None:
    append_jsonl_log(
        _control_plane_log_path(),
        entry,
        rotation_bytes=settings.observability_log_rotation_bytes,
    )


def _count_by(db: Session, model, column, values: list[str]) -> dict[str, int]:
    return {
        value: int(db.scalar(select(func.count()).select_from(model).where(column == value)) or 0)
        for value in values
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _serialize(model: object, fields: tuple[str, ...]) -> dict:
    return {field: getattr(model, field) for field in fields}


def _serialize_artifact_with_role(artifact: Artifact, role: str) -> dict:
    payload = _serialize(
        artifact,
        ("artifact_id", "job_id", "run_id", "kind", "content_type", "storage_ref", "checksum", "size_bytes", "created_at"),
    )
    payload["role"] = role
    return payload


def _error_response(status_code: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"code": code, "message": message, "retryable": retryable}},
    )


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


_OPERATOR_ROLE_RANK = {
    "read_only": 1,
    "operator": 2,
    "lifecycle": 3,
    "security_admin": 4,
}


def _required_operator_role(method: str, path: str) -> str | None:
    if path == "/system/auth-status" or path.startswith("/system/tokens/"):
        return "security_admin"
    if not (
        path.startswith("/messages/")
        or path.startswith("/jobs")
        or path.startswith("/system")
        or path.startswith("/observability")
        or path.startswith("/queue")
        or path.startswith("/agents")
        or path.startswith("/capabilities")
        or path.startswith("/artifacts")
        or (path.startswith("/runtimes") and path != "/runtimes/register")
    ):
        return None

    if method == "GET":
        return "read_only"
    if path.startswith("/messages/"):
        return "operator"
    if path.endswith("/interrupt") or path.endswith("/handoff"):
        return "operator"
    if path.startswith("/agents"):
        return "lifecycle"
    return "security_admin"


def _operator_role_for_token(token: str | None) -> str | None:
    if token is None:
        return None
    if settings.operator_bearer_token and token == settings.operator_bearer_token:
        return "security_admin"
    return settings.operator_token_roles_json.get(token)


def _runtime_token_allowed(token: str | None) -> bool:
    if token is None:
        return False
    if settings.runtime_bearer_token and token == settings.runtime_bearer_token:
        return True
    return token in settings.runtime_active_tokens_json


def _encode_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, object] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def _apply_created_cursor(query, model, cursor: str | None):
    cursor_payload = _decode_cursor(cursor)
    if cursor_payload is None:
        return query
    created_at = cursor_payload.get("created_at")
    entity_id = cursor_payload.get("id")
    if not isinstance(created_at, str) or not isinstance(entity_id, str):
        raise HTTPException(status_code=400, detail="invalid cursor")
    try:
        created_dt = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    pk_col = getattr(model, model.__mapper__.primary_key[0].key)
    return query.where(
        or_(
            model.created_at < created_dt,
            (model.created_at == created_dt) & (pk_col < entity_id),
        )
    )


def _queue_for_target(target_type: str, target_id: str) -> str:
    if target_type == "agent":
        return f"agent:{target_id}"
    if target_type == "capability":
        return f"capability:{target_id}"
    raise HTTPException(status_code=400, detail=f"unsupported target type: {target_type}")


def _capability_queue_for(db: Session, capability_id: str) -> str:
    capability = _require_capability(db, capability_id)
    return f"capability:{capability.capability_id}:{capability.version}"


def _next_event_seq(db: Session) -> int:
    global _event_seq_counter
    value = db.scalar(select(func.max(Event.event_seq)))
    db_max = int(value or 0)
    if _event_seq_counter is None:
        _event_seq_counter = db_max
    else:
        _event_seq_counter = max(_event_seq_counter, db_max)
    _event_seq_counter += 1
    return _event_seq_counter


def _create_event(
    db: Session,
    *,
    event_type: str,
    body: dict,
    job_id: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    runtime_id: str | None = None,
    related_jobs: list[tuple[str, str]] | None = None,
) -> Event:
    with _event_seq_lock:
        event = Event(
            event_id=_new_id("evt"),
            event_seq=_next_event_seq(db),
            job_id=job_id,
            run_id=run_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            event_type=event_type,
            body_json=body,
        )
        db.add(event)
        db.flush()
    for linked_job_id, relation in related_jobs or []:
        db.add(EventJobLink(event_id=event.event_id, job_id=linked_job_id, relation=relation))
    _append_control_plane_log(
        {
            "kind": "control_plane_event",
            "created_at": event.created_at,
            "event_id": event.event_id,
            "event_seq": event.event_seq,
            "event_type": event.event_type,
            "job_id": event.job_id,
            "run_id": event.run_id,
            "agent_id": event.agent_id,
            "runtime_id": event.runtime_id,
            "body": event.body_json,
        }
    )
    return event


def _require_capability(db: Session, capability_id: str) -> Capability:
    capability = db.get(Capability, capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail=f"capability not found: {capability_id}")
    return capability


def _require_agent(db: Session, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    return agent


def _require_runtime(db: Session, runtime_id: str) -> Runtime:
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"runtime not found: {runtime_id}")
    return runtime


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
        raise HTTPException(status_code=400, detail=f"invalid release version: {value}")
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid release version: {value}") from exc
    return major, minor, patch


def _assert_supported_runtime_skew(db: Session, runtime_release_version: str) -> None:
    control_plane_release = _get_upgrade_status(db)["release_version"]
    cp_major, cp_minor, _ = _parse_release_version(control_plane_release)
    rt_major, rt_minor, _ = _parse_release_version(runtime_release_version)
    if rt_major != cp_major:
        raise HTTPException(status_code=409, detail="unsupported major-version skew")
    if rt_minor > cp_minor:
        raise HTTPException(status_code=409, detail="runtime release is ahead of control plane")
    if cp_minor - rt_minor > 1:
        raise HTTPException(status_code=409, detail="runtime release is too far behind control plane")


def _require_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


def _active_lease_for_run(db: Session, run_id: str, lease_id: str) -> Lease:
    lease = db.scalar(
        select(Lease).where(
            Lease.lease_id == lease_id,
            Lease.run_id == run_id,
            Lease.status == LeaseStatus.ACTIVE.value,
        )
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="active lease not found")
    return lease


def _assert_lease_owner(lease: Lease, runtime_id: str, fencing_token: int) -> None:
    if lease.runtime_id != runtime_id:
        raise HTTPException(status_code=409, detail="lease runtime mismatch")
    if lease.fencing_token != fencing_token:
        raise HTTPException(status_code=409, detail="stale fencing token")


def _store_terminal_artifacts(
    db: Session,
    *,
    job_id: str,
    run_id: str,
    artifacts: list,
) -> tuple[str | None, str | None]:
    result_artifact_id: str | None = None
    failure_artifact_id: str | None = None
    for item in artifacts:
        artifact_id = _new_id("art")
        artifact = Artifact(
            artifact_id=artifact_id,
            job_id=job_id,
            run_id=run_id,
            kind=item.role,
            content_type=item.content_type,
            storage_ref=item.storage_ref,
            checksum=item.checksum,
            size_bytes=item.size_bytes,
        )
        db.add(artifact)
        db.add(JobArtifact(job_id=job_id, artifact_id=artifact_id, role=item.role))
        db.add(RunArtifact(run_id=run_id, artifact_id=artifact_id, role=item.role))
        _create_event(
            db,
            job_id=job_id,
            run_id=run_id,
            event_type="artifact.created",
            body={"artifact_id": artifact_id, "role": item.role, "storage_ref": item.storage_ref},
        )
        if item.role == ArtifactKind.RESULT.value:
            result_artifact_id = artifact_id
        if item.role == ArtifactKind.FAILURE_EVIDENCE.value:
            failure_artifact_id = artifact_id
    return result_artifact_id, failure_artifact_id


def _validate_terminal_artifact_roles(artifacts: list, required_roles: set[str]) -> None:
    seen = {item.role for item in artifacts}
    missing = sorted(required_roles - seen)
    if missing:
        raise HTTPException(status_code=400, detail=f"missing required artifact roles: {', '.join(missing)}")


def _validate_artifact_store_refs(artifacts: list) -> None:
    missing_refs = [item.storage_ref for item in artifacts if not _artifact_store().exists(storage_ref=item.storage_ref)]
    if missing_refs:
        raise HTTPException(status_code=400, detail=f"missing durable artifacts: {', '.join(missing_refs)}")


def _fail_exhausted_queued_jobs(db: Session, *, target_queues: list[str]) -> int:
    jobs = db.scalars(
        select(Job).where(
            Job.status == JobStatus.QUEUED.value,
            Job.retry_count >= Job.max_retries,
            Job.target_queue.in_(target_queues),
        )
    ).all()
    for job in jobs:
        job.status = JobStatus.FAILED.value
        job.updated_at = utc_now()
        _create_event(
            db,
            job_id=job.job_id,
            event_type="job.failed",
            body={"reason": "retry_budget_exhausted", "retry_count": job.retry_count},
        )
    return len(jobs)


def _write_control_plane_artifact(*, job_id: str, name: str, content: str) -> SimpleNamespace:
    role_map = {
        "prompt.txt": ArtifactKind.PROMPT.value,
        "transcript.txt": ArtifactKind.TRANSCRIPT_LOG.value,
        "exec.txt": ArtifactKind.EXEC_LOG.value,
        "result.txt": ArtifactKind.RESULT.value,
        "failure.txt": ArtifactKind.FAILURE_EVIDENCE.value,
    }
    stored = _artifact_store().write_text(
        namespace="control-plane",
        job_id=job_id,
        name=name,
        content=content,
        role=role_map[name],
    )
    return SimpleNamespace(
        role=stored.role,
        storage_ref=stored.storage_ref,
        content_type=stored.content_type,
        checksum=stored.checksum,
        size_bytes=stored.size_bytes,
    )


def _ensure_inline_runtime(db: Session) -> Runtime:
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


def _block_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.QUEUED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be blocked from state {job.status}")
    job.status = JobStatus.BLOCKED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.blocked", body={"reason": reason})


def _unblock_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.BLOCKED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be unblocked from state {job.status}")
    job.status = JobStatus.QUEUED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.queued", body={"target_queue": job.target_queue, "reason": reason})


def sweep_expired_leases(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or utc_now()
    expired = db.scalars(
        select(Lease).where(
            Lease.status == LeaseStatus.ACTIVE.value,
            Lease.expires_at < now,
        )
    ).all()
    processed = 0
    requeued = 0
    failed = 0
    for lease in expired:
        run = db.get(Run, lease.run_id)
        if run is None:
            continue
        job = _require_job(db, run.job_id)
        agent = _require_agent(db, lease.agent_id)
        runtime = _require_runtime(db, lease.runtime_id)
        lease.status = LeaseStatus.EXPIRED.value
        run.status = RunStatus.ABANDONED.value
        run.finished_at = now
        if job.retry_count + 1 >= job.max_retries:
            job.retry_count += 1
            job.status = JobStatus.FAILED.value
            failed += 1
        else:
            job.retry_count += 1
            job.status = JobStatus.QUEUED.value
            requeued += 1
        job.updated_at = now
        if agent.status != AgentStatus.TERMINATED.value:
            agent.status = AgentStatus.IDLE.value
            # Lease-expiry recovery makes the durable agent available for a new runtime claim.
            agent.assigned_runtime_id = None
        active_runtime_runs = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.runtime_id == runtime.runtime_id,
                Lease.status == LeaseStatus.ACTIVE.value,
                Lease.lease_id != lease.lease_id,
            )
        ) or 0
        runtime.status = RuntimeStatus.BUSY.value if active_runtime_runs else RuntimeStatus.IDLE.value
        _create_event(
            db,
            job_id=job.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=run.runtime_id,
            event_type="lease.expired",
            body={"lease_id": lease.lease_id, "reason": "heartbeat_timeout", "fencing_token": lease.fencing_token},
        )
        _create_event(
            db,
            job_id=job.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=run.runtime_id,
            event_type="run.abandoned",
            body={"lease_id": lease.lease_id},
        )
        if job.status == JobStatus.QUEUED.value:
            _create_event(
                db,
                job_id=job.job_id,
                event_type="job.requeued",
                body={"reason": "lease_expiry", "retry_count": job.retry_count},
            )
        else:
            _create_event(
                db,
                job_id=job.job_id,
                event_type="job.failed",
                body={"reason": "lease_expiry_retry_exhausted", "retry_count": job.retry_count},
            )
        processed += 1
    if processed:
        db.commit()
    return {"expired_leases": processed, "requeued_jobs": requeued, "failed_jobs": failed}


def sweep_idle_agents(
    db: Session,
    *,
    now: datetime | None = None,
    idle_timeout_seconds: int | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    idle_timeout_seconds = idle_timeout_seconds or settings.agent_idle_timeout_seconds
    cutoff = now - timedelta(seconds=idle_timeout_seconds)
    agents = db.scalars(
        select(Agent).where(
            Agent.status == AgentStatus.IDLE.value,
            Agent.last_seen_at.is_not(None),
            Agent.last_seen_at < cutoff,
        )
    ).all()
    terminated = 0
    for agent in agents:
        has_queued_work = db.scalar(
            select(func.count()).select_from(Job).where(
                Job.target_agent_id == agent.agent_id,
                Job.status == JobStatus.QUEUED.value,
            )
        ) or 0
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if has_queued_work or has_active_lease:
            continue
        agent.status = AgentStatus.TERMINATED.value
        agent.updated_at = now
        _create_event(
            db,
            agent_id=agent.agent_id,
            runtime_id=agent.assigned_runtime_id,
            event_type="agent.terminated",
            body={"reason": "idle_timeout", "idle_timeout_seconds": idle_timeout_seconds},
        )
        terminated += 1
    if terminated:
        db.commit()
    return {"terminated_agents": terminated}


def sweep_stale_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
    stale_timeout_seconds: int | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    stale_timeout_seconds = stale_timeout_seconds or settings.runtime_stale_timeout_seconds
    cutoff = now - timedelta(seconds=stale_timeout_seconds)
    runtimes = db.scalars(
        select(Runtime).where(
            Runtime.status != RuntimeStatus.OFFLINE.value,
            Runtime.last_heartbeat_at.is_not(None),
            Runtime.last_heartbeat_at < cutoff,
        )
    ).all()
    offlined = 0
    detached_agents = 0
    degraded_agents = 0
    for runtime in runtimes:
        runtime.status = RuntimeStatus.OFFLINE.value
        runtime.health_status = HealthStatus.UNREACHABLE.value
        runtime.updated_at = now
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.offline",
            body={"reason": "heartbeat_timeout", "stale_timeout_seconds": stale_timeout_seconds},
        )
        agents = db.scalars(
            select(Agent).where(Agent.assigned_runtime_id == runtime.runtime_id)
        ).all()
        for agent in agents:
            has_active_lease = db.scalar(
                select(func.count()).select_from(Lease).where(
                    Lease.agent_id == agent.agent_id,
                    Lease.runtime_id == runtime.runtime_id,
                    Lease.status == LeaseStatus.ACTIVE.value,
                )
            ) or 0
            if has_active_lease:
                if agent.status != AgentStatus.TERMINATED.value:
                    agent.status = AgentStatus.DEGRADED.value
                    agent.updated_at = now
                    _create_event(
                        db,
                        agent_id=agent.agent_id,
                        runtime_id=runtime.runtime_id,
                        event_type="agent.degraded",
                        body={"reason": "runtime_offline", "runtime_id": runtime.runtime_id},
                    )
                    degraded_agents += 1
                continue
            if agent.status in {AgentStatus.TERMINATED.value, AgentStatus.DRAINING.value}:
                continue
            agent.assigned_runtime_id = None
            agent.status = AgentStatus.IDLE.value
            agent.updated_at = now
            _create_event(
                db,
                agent_id=agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="agent.idle",
                body={"reason": "runtime_rebind_required", "previous_runtime_id": runtime.runtime_id},
            )
            detached_agents += 1
        offlined += 1
    if offlined:
        db.commit()
    return {
        "offline_runtimes": offlined,
        "detached_agents": detached_agents,
        "degraded_agents": degraded_agents,
    }


def sweep_draining_agents(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    agents = db.scalars(
        select(Agent).where(Agent.status == AgentStatus.DRAINING.value)
    ).all()
    terminated = 0
    for agent in agents:
        has_queued_work = db.scalar(
            select(func.count()).select_from(Job).where(
                Job.target_agent_id == agent.agent_id,
                Job.status == JobStatus.QUEUED.value,
            )
        ) or 0
        has_active_lease = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.agent_id == agent.agent_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if has_queued_work or has_active_lease:
            continue
        agent.status = AgentStatus.TERMINATED.value
        agent.updated_at = now
        _create_event(
            db,
            agent_id=agent.agent_id,
            runtime_id=agent.assigned_runtime_id,
            event_type="agent.terminated",
            body={"reason": "drain_complete"},
        )
        terminated += 1
    if terminated:
        db.commit()
    return {"terminated_agents": terminated}


def sweep_draining_runtimes(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or utc_now()
    runtimes = db.scalars(
        select(Runtime).where(Runtime.status == RuntimeStatus.DRAINING.value)
    ).all()
    resumed = 0
    for runtime in runtimes:
        active_leases = db.scalar(
            select(func.count()).select_from(Lease).where(
                Lease.runtime_id == runtime.runtime_id,
                Lease.status == LeaseStatus.ACTIVE.value,
            )
        ) or 0
        if active_leases:
            continue
        runtime.status = RuntimeStatus.IDLE.value
        if runtime.health_status == HealthStatus.DRAINING.value:
            runtime.health_status = HealthStatus.HEALTHY.value
        runtime.updated_at = now
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.idle",
            body={"reason": "drain_complete"},
        )
        resumed += 1
    if resumed:
        db.commit()
    return {"resumed_runtimes": resumed}


@router.get("/health", response_model=dict)
def health() -> dict:
    payload = HealthResponse(components={"api": "ok", "db": "ok"})
    return _ok(payload.model_dump())


@router.get("/system/upgrade-status", response_model=dict)
def system_upgrade_status(db: Session = Depends(get_db)) -> dict:
    return _ok(_get_upgrade_status(db))


@router.get("/system/auth-status", response_model=dict)
def system_auth_status() -> dict:
    role_counts: dict[str, int] = {}
    for role in settings.operator_token_roles_json.values():
        role_counts[role] = role_counts.get(role, 0) + 1
    return _ok(
        {
            "operator": {
                "legacy_admin_token_configured": bool(settings.operator_bearer_token),
                "managed_token_count": len(settings.operator_token_roles_json),
                "managed_role_counts": role_counts,
            },
            "runtime": {
                "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
                "active_token_count": len(settings.runtime_active_tokens_json),
            },
        }
    )


@router.post("/system/tokens/operator", response_model=dict)
def system_rotate_operator_tokens(request: RotateOperatorTokensRequest, db: Session = Depends(get_db)) -> dict:
    settings.operator_bearer_token = request.operator_bearer_token
    settings.operator_token_roles_json = dict(request.operator_token_roles_json)
    _set_system_metadata_value(db, "operator_bearer_token", settings.operator_bearer_token)
    _set_system_metadata_value(db, "operator_token_roles_json", json.dumps(settings.operator_token_roles_json, sort_keys=True))
    role_counts: dict[str, int] = {}
    for role in settings.operator_token_roles_json.values():
        role_counts[role] = role_counts.get(role, 0) + 1
    event = _create_event(
        db,
        event_type="system.operator_tokens_rotated",
        body={
            "legacy_admin_token_configured": bool(settings.operator_bearer_token),
            "managed_token_count": len(settings.operator_token_roles_json),
            "managed_role_counts": role_counts,
        },
    )
    db.commit()
    return _ok(
        {
            "rotated": "operator",
            "legacy_admin_token_configured": bool(settings.operator_bearer_token),
            "managed_token_count": len(settings.operator_token_roles_json),
            "managed_role_counts": role_counts,
            "audit_event_id": event.event_id,
        }
    )


@router.post("/system/tokens/runtime", response_model=dict)
def system_rotate_runtime_tokens(request: RotateRuntimeTokensRequest, db: Session = Depends(get_db)) -> dict:
    settings.runtime_bearer_token = request.runtime_bearer_token
    settings.runtime_active_tokens_json = list(request.runtime_active_tokens_json)
    _set_system_metadata_value(db, "runtime_bearer_token", settings.runtime_bearer_token)
    _set_system_metadata_value(db, "runtime_active_tokens_json", json.dumps(settings.runtime_active_tokens_json))
    event = _create_event(
        db,
        event_type="system.runtime_tokens_rotated",
        body={
            "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
            "active_token_count": len(settings.runtime_active_tokens_json),
        },
    )
    db.commit()
    return _ok(
        {
            "rotated": "runtime",
            "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
            "active_token_count": len(settings.runtime_active_tokens_json),
            "audit_event_id": event.event_id,
        }
    )


@router.post("/messages/send", response_model=dict)
def send_message(
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    request_hash = str(hash(request.model_dump_json()))
    if idempotency_key is not None:
        existing = db.get(IdempotencyKey, {"idempotency_key": idempotency_key, "endpoint": "/messages/send"})
        if existing is not None:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
            return existing.response_json

    if request.target.type == "agent":
        target_agent = _require_agent(db, request.target.id)
    elif request.target.type == "capability":
        _require_capability(db, request.target.id)
        target_agent = None
    else:
        raise HTTPException(status_code=400, detail="target.type must be agent or capability")

    message = Message(
        message_id=_new_id("msg"),
        target_type=request.target.type,
        target_id=request.target.id,
        text=request.message.text,
        metadata_json=request.message.metadata,
    )
    db.add(message)
    db.flush()

    job = Job(
        job_id=_new_id("job"),
        message_id=message.message_id,
        target_agent_id=request.target.id if request.target.type == "agent" else None,
        target_queue=(
            _queue_for_target(request.target.type, request.target.id)
            if request.target.type == "agent"
            else _capability_queue_for(db, request.target.id)
        ),
        status=JobStatus.QUEUED.value,
        max_retries=3,
    )
    db.add(job)
    db.flush()

    _create_event(
        db,
        job_id=job.job_id,
        event_type="job.accepted",
        body={"message_id": message.message_id, "target_type": request.target.type, "target_id": request.target.id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        event_type="job.queued",
        body={"target_queue": job.target_queue},
    )
    _queue_backend().enqueue_job(db, job=job)

    if request.target.type == "agent" and target_agent is not None:
        detach_mode = request.detach_policy.get("mode", "auto")
        if detach_mode == "inline" and target_agent.status == AgentStatus.IDLE.value:
            runtime = _ensure_inline_runtime(db)
            attempt = 1
            run = Run(
                run_id=_new_id("run"),
                job_id=job.job_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                attempt=attempt,
                status=RunStatus.RUNNING.value,
                started_at=utc_now(),
            )
            db.add(run)
            db.flush()
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.created",
                body={"attempt": attempt},
            )
            lease = Lease(
                lease_id=_new_id("lease"),
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                fencing_token=attempt,
                status=LeaseStatus.ACTIVE.value,
                expires_at=utc_now() + timedelta(seconds=30),
            )
            db.add(lease)
            job.status = JobStatus.RUNNING.value
            job.latest_run_id = run.run_id
            job.updated_at = utc_now()
            target_agent.status = AgentStatus.BUSY.value
            target_agent.assigned_runtime_id = runtime.runtime_id
            runtime.status = RuntimeStatus.BUSY.value
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="lease.acquired",
                body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()},
            )
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.running",
                body={"started_by": runtime.runtime_id},
            )
            artifacts = [
                _write_control_plane_artifact(job_id=job.job_id, name="prompt.txt", content=request.message.text),
                _write_control_plane_artifact(job_id=job.job_id, name="transcript.txt", content=f"inline\nmessage={request.message.text}\n"),
                _write_control_plane_artifact(job_id=job.job_id, name="exec.txt", content="inline-exec\n"),
                _write_control_plane_artifact(job_id=job.job_id, name="result.txt", content=f"inline result for {request.message.text}\n"),
            ]
            result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=artifacts)
            run.status = RunStatus.COMPLETED.value
            run.finished_at = utc_now()
            lease.status = LeaseStatus.RELEASED.value
            lease.released_at = utc_now()
            job.status = JobStatus.COMPLETED.value
            job.result_artifact_id = result_artifact_id
            job.updated_at = utc_now()
            target_agent.status = AgentStatus.IDLE.value
            runtime.status = RuntimeStatus.IDLE.value
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="lease.released",
                body={"lease_id": lease.lease_id},
            )
            _create_event(
                db,
                job_id=job.job_id,
                run_id=run.run_id,
                agent_id=target_agent.agent_id,
                runtime_id=runtime.runtime_id,
                event_type="run.completed",
                body={"artifact_ids": [artifact.artifact_id for artifact in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
            )
            _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
            response = _ok(
                {
                    "kind": "inline_result",
                    "job_id": job.job_id,
                    "result_artifact_id": result_artifact_id,
                    "status": JobStatus.COMPLETED.value,
                }
            )
            if idempotency_key is not None:
                db.add(
                    IdempotencyKey(
                        idempotency_key=idempotency_key,
                        endpoint="/messages/send",
                        request_hash=request_hash,
                        response_json=response,
                        expires_at=utc_now() + timedelta(days=1),
                    )
                )
            db.commit()
            return response

    response = _ok(
        {
            "kind": "accepted_async",
            "job_id": job.job_id,
            "status": JobStatus.QUEUED.value,
            "message_id": message.message_id,
            "target": request.target.model_dump(),
        }
    )
    if idempotency_key is not None:
        db.add(
            IdempotencyKey(
                idempotency_key=idempotency_key,
                endpoint="/messages/send",
                request_hash=request_hash,
                response_json=response,
                expires_at=utc_now() + timedelta(days=1),
            )
        )
    db.commit()
    return response


@router.get("/jobs", response_model=dict)
def list_jobs(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    target_agent_id: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    cursor_payload = _decode_cursor(cursor)
    query = select(Job)
    if status is not None:
        query = query.where(Job.status == status)
    if target_agent_id is not None:
        query = query.where(Job.target_agent_id == target_agent_id)
    if created_after is not None:
        query = query.where(Job.created_at >= created_after)
    if cursor_payload is not None:
        created_at = datetime.fromisoformat(str(cursor_payload["created_at"]))
        job_id = str(cursor_payload["job_id"])
        query = query.where(
            (Job.created_at < created_at) | ((Job.created_at == created_at) & (Job.job_id < job_id))
        )
    jobs = db.scalars(query.order_by(Job.created_at.desc(), Job.job_id.desc()).limit(limit + 1)).all()
    page_items = jobs[:limit]
    next_cursor = None
    if len(jobs) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"created_at": last.created_at.isoformat(), "job_id": last.job_id})
    return _ok(
        _page(
            [
                _serialize(
                    job,
                    (
                        "job_id",
                        "message_id",
                        "target_agent_id",
                        "target_queue",
                        "status",
                        "retry_count",
                        "max_retries",
                        "latest_run_id",
                        "result_artifact_id",
                    ),
                )
                for job in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.get("/jobs/{job_id}", response_model=dict)
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    return _ok(
        _serialize(
            job,
            (
                "job_id",
                "message_id",
                "target_agent_id",
                "target_queue",
                "status",
                "retry_count",
                "max_retries",
                "latest_run_id",
                "result_artifact_id",
                "created_at",
                "updated_at",
            ),
        )
    )


@router.get("/jobs/{job_id}/events", response_model=dict)
def get_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    _require_job(db, job_id)
    cursor_payload = _decode_cursor(cursor)
    query = select(Event).where(Event.job_id == job_id)
    if cursor_payload is not None:
        query = query.where(Event.event_seq > int(cursor_payload["event_seq"]))
    events = db.scalars(query.order_by(Event.event_seq).limit(limit + 1)).all()
    page_items = events[:limit]
    next_cursor = None
    if len(events) > limit:
        last = page_items[-1]
        next_cursor = _encode_cursor({"event_seq": last.event_seq})
    return _ok(
        _page(
            [
                {
                    "event_id": event.event_id,
                    "event_seq": event.event_seq,
                    "event_type": event.event_type,
                    "body": event.body_json,
                    "created_at": event.created_at.isoformat(),
                }
                for event in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.post("/jobs/{job_id}/interrupt", response_model=dict)
def interrupt_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    if job.status == JobStatus.QUEUED.value:
        job.status = JobStatus.CANCELLED.value
        event_type = "job.cancelled"
    elif job.status == JobStatus.RUNNING.value:
        job.status = JobStatus.INTERRUPT_REQUESTED.value
        event_type = "job.interrupt_requested"
    else:
        raise HTTPException(status_code=409, detail=f"job cannot be interrupted from state {job.status}")
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type=event_type, body={"status": job.status})
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status})


@router.post("/jobs/{job_id}/block", response_model=dict)
def block_job(job_id: str, reason: str = Query(default="operator_blocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _block_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/unblock", response_model=dict)
def unblock_job(job_id: str, reason: str = Query(default="operator_unblocked"), db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    _unblock_job(db, job=job, reason=reason)
    db.commit()
    return _ok({"job_id": job.job_id, "status": job.status, "reason": reason})


@router.post("/jobs/{job_id}/handoff", response_model=dict)
def handoff_job(job_id: str, request: HandoffRequest, db: Session = Depends(get_db)) -> dict:
    source_job = _require_job(db, job_id)
    handoff = Handoff(handoff_id=_new_id("hnd"), source_job_id=source_job.job_id)
    db.add(handoff)
    db.flush()
    child_job_ids: list[str] = []
    for target in request.targets:
        if target.type == "agent":
            _require_agent(db, target.id)
        else:
            _require_capability(db, target.id)
        message = Message(
            message_id=_new_id("msg"),
            target_type=target.type,
            target_id=target.id,
            text=request.message.text,
            metadata_json=request.message.metadata,
        )
        db.add(message)
        db.flush()
        child = Job(
            job_id=_new_id("job"),
            message_id=message.message_id,
            target_agent_id=target.id if target.type == "agent" else None,
            target_queue=(
                _queue_for_target(target.type, target.id)
                if target.type == "agent"
                else _capability_queue_for(db, target.id)
            ),
            status=JobStatus.QUEUED.value,
            max_retries=3,
        )
        db.add(child)
        db.flush()
        db.add(HandoffJob(handoff_id=handoff.handoff_id, job_id=child.job_id))
        child_job_ids.append(child.job_id)
        _queue_backend().enqueue_job(db, job=child)
    for artifact_id in request.artifact_ids:
        db.add(HandoffArtifact(handoff_id=handoff.handoff_id, artifact_id=artifact_id))
    _create_event(
        db,
        job_id=source_job.job_id,
        event_type="handoff.created",
        body={
            "handoff_id": handoff.handoff_id,
            "source_job_id": source_job.job_id,
            "source_artifact_ids": request.artifact_ids,
            "created_job_ids": child_job_ids,
        },
        related_jobs=[(source_job.job_id, "source"), *[(child_id, "child") for child_id in child_job_ids]],
    )
    db.commit()
    return _ok({"handoff_id": handoff.handoff_id, "source_job_id": job_id, "child_job_ids": child_job_ids})


@router.get("/agents", response_model=dict)
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
            [
                _serialize(
                    agent,
                    ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref"),
                )
                for agent in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.post("/agents/up", response_model=dict)
def agent_up(request: AgentUpRequest, db: Session = Depends(get_db)) -> dict:
    _require_capability(db, request.capability_id)
    agent_id = request.agent_id or _new_id("agt")
    if db.get(Agent, agent_id) is not None:
        raise HTTPException(status_code=409, detail=f"agent already exists: {agent_id}")
    if request.assigned_runtime_id is not None:
        _require_runtime(db, request.assigned_runtime_id)
    agent = Agent(
        agent_id=agent_id,
        capability_id=request.capability_id,
        assigned_runtime_id=request.assigned_runtime_id,
        queue_id=f"agent:{agent_id}",
        status=AgentStatus.PROVISIONING.value,
        workspace_ref=request.workspace_ref,
        last_seen_at=utc_now(),
    )
    db.add(agent)
    _create_event(db, agent_id=agent.agent_id, event_type="agent.provisioning", body={"capability_id": agent.capability_id})
    agent.status = AgentStatus.IDLE.value
    _create_event(db, agent_id=agent.agent_id, event_type="agent.idle", body={"capability_id": agent.capability_id})
    db.commit()
    return _ok(_serialize(agent, ("agent_id", "capability_id", "assigned_runtime_id", "queue_id", "status", "workspace_ref")))


@router.post("/agents/{agent_id}/down", response_model=dict)
def agent_down(agent_id: str, request: AgentDownRequest, db: Session = Depends(get_db)) -> dict:
    agent = _require_agent(db, agent_id)
    if request.mode == "drain":
        agent.status = AgentStatus.DRAINING.value
        event_type = "agent.draining"
    else:
        agent.status = AgentStatus.TERMINATED.value
        event_type = "agent.terminated"
        if request.mode == "force":
            running_jobs = db.scalars(
                select(Job).where(Job.target_agent_id == agent_id, Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value]))
            ).all()
            for job in running_jobs:
                job.status = JobStatus.CANCELLED.value
                job.updated_at = utc_now()
                _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    agent.updated_at = utc_now()
    _create_event(db, agent_id=agent.agent_id, event_type=event_type, body={"mode": request.mode})
    db.commit()
    return _ok({"agent_id": agent.agent_id, "status": agent.status, "mode": request.mode})


@router.get("/capabilities", response_model=dict)
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
    return _ok(
        _page(
            [
                _serialize(
                    capability,
                    (
                        "capability_id",
                        "name",
                        "version",
                        "image_ref",
                        "model_ref",
                        "resource_tier",
                        "permission_profile",
                        "queue_mode",
                        "runtime_requirements_json",
                    ),
                )
                for capability in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.post("/runtimes/register", response_model=dict)
def register_runtime(request: RuntimeRegisterRequest, db: Session = Depends(get_db)) -> dict:
    _assert_supported_runtime_skew(db, request.release_version)
    runtime_id = request.runtime_id or _new_id("rtm")
    runtime = db.get(Runtime, runtime_id)
    if runtime is None:
        runtime = Runtime(
            runtime_id=runtime_id,
            hostname=request.hostname,
            release_version=request.release_version,
            status=RuntimeStatus.IDLE.value,
            health_status=HealthStatus.HEALTHY.value,
            metadata_json=request.metadata,
            last_seen_at=utc_now(),
            last_heartbeat_at=utc_now(),
        )
        db.add(runtime)
        _create_event(
            db,
            runtime_id=runtime.runtime_id,
            event_type="runtime.registered",
            body={"hostname": runtime.hostname, "release_version": runtime.release_version},
        )
    else:
        runtime.hostname = request.hostname
        runtime.release_version = request.release_version
        runtime.metadata_json = request.metadata
        runtime.status = RuntimeStatus.IDLE.value
        runtime.health_status = HealthStatus.HEALTHY.value
        runtime.last_seen_at = utc_now()
        runtime.last_heartbeat_at = utc_now()
    db.commit()
    return _ok(
        _serialize(
            runtime,
            ("runtime_id", "hostname", "release_version", "status", "health_status", "metadata_json", "last_heartbeat_at"),
        )
    )


@router.get("/runtimes", response_model=dict)
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
    return _ok(
        _page(
            [
                _serialize(
                    runtime,
                    ("runtime_id", "hostname", "release_version", "status", "health_status", "metadata_json", "last_heartbeat_at"),
                )
                for runtime in page_items
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


@router.post("/runs/claim", response_model=dict)
def claim_run(request: ClaimRunRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, request.runtime_id)
    if runtime.health_status == HealthStatus.UNREACHABLE.value:
        raise HTTPException(status_code=409, detail="runtime is unreachable")
    if runtime.status == RuntimeStatus.DRAINING.value:
        raise HTTPException(status_code=409, detail="runtime is draining")
    if request.agent_id is not None:
        agent = _require_agent(db, request.agent_id)
        if agent.status != AgentStatus.IDLE.value:
            return _ok({"claimed": False, "agent_id": agent.agent_id})
        if agent.assigned_runtime_id is not None and agent.assigned_runtime_id != runtime.runtime_id:
            return _ok({"claimed": False, "agent_id": agent.agent_id})
    elif request.capability_id is not None:
        agent = db.scalar(
            select(Agent).where(
                Agent.capability_id == request.capability_id,
                Agent.status == AgentStatus.IDLE.value,
                or_(Agent.assigned_runtime_id.is_(None), Agent.assigned_runtime_id == runtime.runtime_id),
            )
        )
        if agent is None:
            return _ok({"claimed": False})
    else:
        raise HTTPException(status_code=400, detail="claim requires agent_id or capability_id")

    capability_queue = _capability_queue_for(db, agent.capability_id)
    target_queues = [f"agent:{agent.agent_id}", capability_queue]
    exhausted_count = _fail_exhausted_queued_jobs(db, target_queues=target_queues)

    delivery = _queue_backend().dequeue_candidate(db, target_queues=target_queues)
    if delivery is None:
        if exhausted_count:
            db.commit()
        return _ok({"claimed": False, "agent_id": agent.agent_id})
    job = db.get(Job, delivery.job_id)
    if job is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise HTTPException(status_code=500, detail=f"job missing for delivery {delivery.job_id}")
    if job.status != JobStatus.QUEUED.value or job.retry_count >= job.max_retries:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        if exhausted_count:
            db.commit()
        return _ok({"claimed": False, "agent_id": agent.agent_id})
    message = db.get(Message, job.message_id)
    if message is None:
        _queue_backend().release_unclaimed(db, delivery=delivery)
        raise HTTPException(status_code=500, detail=f"message missing for job {job.job_id}")

    attempt = (db.scalar(select(func.max(Run.attempt)).where(Run.job_id == job.job_id)) or 0) + 1
    run = Run(
        run_id=_new_id("run"),
        job_id=job.job_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        attempt=attempt,
        status=RunStatus.LEASED.value,
    )
    db.add(run)
    db.flush()
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="run.created",
        body={"attempt": attempt},
    )
    lease = Lease(
        lease_id=_new_id("lease"),
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        fencing_token=attempt,
        status=LeaseStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(seconds=request.lease_ttl_seconds),
    )
    db.add(lease)
    job.status = JobStatus.RUNNING.value
    job.latest_run_id = run.run_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.BUSY.value
    agent.assigned_runtime_id = runtime.runtime_id
    runtime.status = RuntimeStatus.BUSY.value
    runtime.last_heartbeat_at = utc_now()
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="agent.busy",
        body={"run_id": run.run_id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="lease.acquired",
        body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()},
    )
    _queue_backend().ack_claim(db, delivery=delivery, job=job)
    db.commit()
    return _ok(
        {
            "claimed": True,
            "job": _serialize(job, ("job_id", "message_id", "target_queue", "status")),
            "message": {
                "message_id": message.message_id,
                "target_type": message.target_type,
                "target_id": message.target_id,
                "text": message.text,
                "metadata": message.metadata_json,
            },
            "run": _serialize(run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status")),
            "lease": _serialize(lease, ("lease_id", "run_id", "agent_id", "runtime_id", "fencing_token", "status", "expires_at")),
            "agent_id": agent.agent_id,
        }
    )


@router.post("/runs/{run_id}/heartbeat", response_model=dict)
def heartbeat_run(run_id: str, request: HeartbeatRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _require_runtime(db, request.runtime_id)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
        _create_event(
            db,
            job_id=run.job_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            runtime_id=runtime.runtime_id,
            event_type="run.running",
            body={"started_by": runtime.runtime_id},
        )
    lease.expires_at = utc_now() + timedelta(seconds=request.extend_seconds)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=runtime.runtime_id,
        event_type="lease.heartbeat",
        body={"lease_id": lease.lease_id, "expires_at": lease.expires_at.isoformat()},
    )
    db.commit()
    return _ok({"run_id": run_id, "lease_id": lease.lease_id, "status": run.status, "expires_at": lease.expires_at})


@router.post("/runs/{run_id}/progress", response_model=dict)
def progress_run(run_id: str, request: ProgressRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status == RunStatus.LEASED.value:
        run.status = RunStatus.RUNNING.value
        run.started_at = utc_now()
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.progress",
        body={"message": request.message, "details": request.details},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/recovering", response_model=dict)
def recovering_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    runtime = _require_runtime(db, request.runtime_id)
    if run.status not in {RunStatus.RUNNING.value, RunStatus.LEASED.value}:
        raise HTTPException(status_code=409, detail=f"run cannot enter recovering from state {run.status}")
    run.status = RunStatus.RECOVERING.value
    lease.expires_at = utc_now() + timedelta(seconds=30)
    runtime.last_seen_at = utc_now()
    runtime.last_heartbeat_at = utc_now()
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.recovering",
        body={"details": request.details, "expires_at": lease.expires_at.isoformat()},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/resumed", response_model=dict)
def resumed_run(run_id: str, request: RecoveryRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.status != RunStatus.RECOVERING.value:
        raise HTTPException(status_code=409, detail=f"run cannot resume from state {run.status}")
    run.status = RunStatus.RUNNING.value
    event = _create_event(
        db,
        job_id=run.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=request.runtime_id,
        event_type="run.resumed",
        body={"details": request.details},
    )
    db.commit()
    return _ok({"run_id": run_id, "event_id": event.event_id, "status": run.status})


@router.post("/runs/{run_id}/cancel", response_model=dict)
def cancel_run(run_id: str, request: CancelRunRequest, db: Session = Depends(get_db)) -> dict:
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    run.status = RunStatus.CANCELLED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.CANCELLED.value
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="lease.released",
        body={"lease_id": lease.lease_id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="run.cancelled",
        body={"reason": request.reason},
    )
    _create_event(db, job_id=job.job_id, event_type="job.cancelled", body={"status": job.status})
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status})


@router.post("/runs/{run_id}/complete", response_model=dict)
def complete_run(run_id: str, request: CompleteRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(
        request.artifacts,
        {
            ArtifactKind.PROMPT.value,
            ArtifactKind.TRANSCRIPT_LOG.value,
            ArtifactKind.EXEC_LOG.value,
            ArtifactKind.RESULT.value,
        },
    )
    _validate_artifact_store_refs(request.artifacts)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=request.artifacts)
    run.status = RunStatus.COMPLETED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.COMPLETED.value
    job.result_artifact_id = result_artifact_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="lease.released",
        body={"lease_id": lease.lease_id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="run.completed",
        body={"summary": request.summary, "artifact_ids": [artifact.artifact_id for artifact in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
    db.commit()
    return _ok({"run_id": run_id, "job_id": job.job_id, "status": run.status, "result_artifact_id": result_artifact_id})


@router.post("/runs/{run_id}/fail", response_model=dict)
def fail_run(run_id: str, request: FailRunRequest, db: Session = Depends(get_db)) -> dict:
    _validate_terminal_artifact_roles(
        request.artifacts,
        {
            ArtifactKind.PROMPT.value,
            ArtifactKind.TRANSCRIPT_LOG.value,
            ArtifactKind.EXEC_LOG.value,
            ArtifactKind.FAILURE_EVIDENCE.value,
        },
    )
    _validate_artifact_store_refs(request.artifacts)
    lease = _active_lease_for_run(db, run_id, request.lease_id)
    _assert_lease_owner(lease, request.runtime_id, request.fencing_token)
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    job = _require_job(db, run.job_id)
    agent = _require_agent(db, run.agent_id)
    runtime = _require_runtime(db, run.runtime_id)
    _, failure_artifact_id = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=request.artifacts)
    run.status = RunStatus.FAILED.value
    run.finished_at = utc_now()
    run.error_artifact_id = failure_artifact_id
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.FAILED.value
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value if agent.status != AgentStatus.TERMINATED.value else agent.status
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="lease.released",
        body={"lease_id": lease.lease_id},
    )
    _create_event(
        db,
        job_id=job.job_id,
        run_id=run.run_id,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        event_type="run.failed",
        body={"error": request.error, "summary": request.summary, "artifact_ids": [artifact.artifact_id for artifact in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]},
    )
    _create_event(db, job_id=job.job_id, event_type="job.failed", body={"status": job.status})
    db.commit()
    return _ok(
        {
            "run_id": run_id,
            "job_id": job.job_id,
            "run_status": run.status,
            "job_status": job.status,
            "retry_count": job.retry_count,
        }
    )


@router.get("/artifacts/{artifact_id}", response_model=dict)
def get_artifact(artifact_id: str, db: Session = Depends(get_db)) -> dict:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {artifact_id}")
    return _ok(
        _serialize(
            artifact,
            ("artifact_id", "job_id", "run_id", "kind", "content_type", "storage_ref", "checksum", "size_bytes", "created_at"),
        )
    )


@router.get("/jobs/{job_id}/artifacts", response_model=dict)
def list_job_artifacts(job_id: str, role: str | None = Query(default=None), db: Session = Depends(get_db)) -> dict:
    _require_job(db, job_id)
    rows = db.execute(
        select(JobArtifact, Artifact)
        .join(Artifact, Artifact.artifact_id == JobArtifact.artifact_id)
        .where(JobArtifact.job_id == job_id)
        .order_by(JobArtifact.role.asc(), Artifact.created_at.asc())
    ).all()
    items = [
        _serialize_artifact_with_role(artifact, link.role)
        for link, artifact in rows
        if role is None or link.role == role
    ]
    return _ok({"items": items, "job_id": job_id, "role": role})


@router.get("/runs/{run_id}/artifacts", response_model=dict)
def list_run_artifacts(run_id: str, role: str | None = Query(default=None), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    rows = db.execute(
        select(RunArtifact, Artifact)
        .join(Artifact, Artifact.artifact_id == RunArtifact.artifact_id)
        .where(RunArtifact.run_id == run_id)
        .order_by(RunArtifact.role.asc(), Artifact.created_at.asc())
    ).all()
    items = [
        _serialize_artifact_with_role(artifact, link.role)
        for link, artifact in rows
        if role is None or link.role == role
    ]
    return _ok({"items": items, "run_id": run_id, "role": role})


@router.get("/artifacts/{artifact_id}/content", response_model=dict)
def get_artifact_content(artifact_id: str, db: Session = Depends(get_db)) -> dict:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {artifact_id}")
    content = _artifact_store().read_text(storage_ref=artifact.storage_ref)
    payload = {"artifact_id": artifact.artifact_id, "storage_ref": artifact.storage_ref, "content_type": artifact.content_type}
    if content is not None:
        payload["content"] = content
    return _ok(payload)


@router.get("/observability/summary", response_model=dict)
def observability_summary(db: Session = Depends(get_db)) -> dict:
    latest_event_seq = int(db.scalar(select(func.max(Event.event_seq))) or 0)
    queue_depth = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED.value)) or 0)
    active_leases = int(
        db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.ACTIVE.value)) or 0
    )
    dead_lettered_deliveries = int(
        db.scalar(select(func.count()).select_from(QueueDeliveryRecord).where(QueueDeliveryRecord.state == "dead_lettered"))
        or 0
    )

    return _ok(
        {
            "jobs": _count_by(
                db,
                Job,
                Job.status,
                [
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.BLOCKED.value,
                ],
            ),
            "runtimes": _count_by(
                db,
                Runtime,
                Runtime.status,
                [
                    RuntimeStatus.IDLE.value,
                    RuntimeStatus.BUSY.value,
                    RuntimeStatus.DEGRADED.value,
                    RuntimeStatus.DRAINING.value,
                    RuntimeStatus.OFFLINE.value,
                ],
            ),
            "agents": _count_by(
                db,
                Agent,
                Agent.status,
                [
                    AgentStatus.IDLE.value,
                    AgentStatus.BUSY.value,
                    AgentStatus.DEGRADED.value,
                    AgentStatus.DRAINING.value,
                    AgentStatus.TERMINATED.value,
                ],
            ),
            "leases": {
                "active": active_leases,
                "expired": int(
                    db.scalar(select(func.count()).select_from(Lease).where(Lease.status == LeaseStatus.EXPIRED.value)) or 0
                ),
            },
            "queue": {
                "depth": queue_depth,
                "dead_lettered_deliveries": dead_lettered_deliveries,
            },
            "events": {"latest_event_seq": latest_event_seq},
        }
    )


@router.get("/observability/alerts", response_model=dict)
def observability_alerts(db: Session = Depends(get_db)) -> dict:
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

    return _ok(
        {
            "items": alerts,
            "counts": {
                "active": len(alerts),
                "expired_leases": expired_leases,
                "dead_lettered_deliveries": dead_lettered_deliveries,
                "unreachable_runtimes": unreachable_runtimes,
            },
        }
    )


@router.get("/observability/jobs/{job_id}/trace", response_model=dict)
def observability_job_trace(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _require_job(db, job_id)
    events = db.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.event_seq.asc())).all()
    if not events:
        raise HTTPException(status_code=404, detail=f"no events found for job: {job_id}")

    first_by_type: dict[str, Event] = {}
    for event in events:
        first_by_type.setdefault(event.event_type, event)

    trace_started_at = events[0].created_at
    trace_finished_at = events[-1].created_at if job.status in {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    } else None

    accepted_at = first_by_type.get("job.accepted", events[0]).created_at
    queued_at = first_by_type.get("job.queued").created_at if "job.queued" in first_by_type else None
    lease_at = first_by_type.get("lease.acquired").created_at if "lease.acquired" in first_by_type else None
    run_at = first_by_type.get("run.running").created_at if "run.running" in first_by_type else None
    terminal_event = (
        first_by_type.get("job.completed")
        or first_by_type.get("job.failed")
        or first_by_type.get("job.cancelled")
    )
    terminal_at = terminal_event.created_at if terminal_event is not None else None

    return _ok(
        {
            "job": _serialize(
                job,
                (
                    "job_id",
                    "message_id",
                    "target_agent_id",
                    "target_queue",
                    "status",
                    "retry_count",
                    "max_retries",
                    "latest_run_id",
                    "result_artifact_id",
                    "created_at",
                    "updated_at",
                ),
            ),
            "runs": [
                _serialize(run, ("run_id", "job_id", "agent_id", "runtime_id", "attempt", "status", "started_at", "finished_at"))
                for run in db.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.attempt.asc())).all()
            ],
            "timeline": [
                {
                    "event_id": event.event_id,
                    "event_seq": event.event_seq,
                    "event_type": event.event_type,
                    "created_at": event.created_at,
                    "run_id": event.run_id,
                    "agent_id": event.agent_id,
                    "runtime_id": event.runtime_id,
                    "body": event.body_json,
                }
                for event in events
            ],
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
        }
    )


@router.get("/observability/logs/control-plane", response_model=dict)
def observability_control_plane_logs(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return _ok(
        {
            "items": read_tail_jsonl_family(_control_plane_log_path(), limit=limit),
            "source": str(_control_plane_log_path()),
            "limit": limit,
        }
    )


@router.get("/observability/logs/runtimes/{runtime_id}", response_model=dict)
def observability_runtime_logs(runtime_id: str, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    path = settings.log_root / f"runtime-{runtime_id}.jsonl"
    return _ok(
        {
            "items": read_tail_jsonl_family(path, limit=limit),
            "source": str(path),
            "runtime_id": runtime_id,
            "limit": limit,
        }
    )


@router.get("/queue/deliveries", response_model=dict)
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
    rows = db.scalars(
        query.order_by(QueueDeliveryRecord.created_at.desc(), QueueDeliveryRecord.delivery_id.desc()).limit(limit + 1)
    ).all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        tail = page_rows[-1]
        next_cursor = _encode_cursor({"created_at": tail.created_at.isoformat(), "id": tail.delivery_id})
    return _ok(
        _page(
            [
                _serialize(
                    row,
                    (
                        "delivery_id",
                        "job_id",
                        "target_queue",
                        "state",
                        "delivery_attempt",
                        "available_at",
                        "last_delivered_at",
                        "acked_at",
                        "dead_lettered_at",
                        "created_at",
                    ),
                )
                for row in page_rows
            ],
            limit=limit,
            next_cursor=next_cursor,
        )
    )


def build_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0")

    _load_persisted_auth_settings()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        method = request.method.upper()
        if path == "/health":
            return await call_next(request)

        token = _extract_bearer_token(request.headers.get("Authorization"))
        is_runtime_write = path == "/runtimes/register" or path.startswith("/runs/")
        required_operator_role = _required_operator_role(method, path)
        is_operator_surface = required_operator_role is not None

        if is_runtime_write and (settings.runtime_bearer_token or settings.runtime_active_tokens_json):
            if not _runtime_token_allowed(token):
                return _error_response(401, "unauthenticated", "runtime authentication required", False)
        if is_operator_surface and (settings.operator_bearer_token or settings.operator_token_roles_json):
            role = _operator_role_for_token(token)
            if role is None:
                return _error_response(401, "unauthenticated", "operator authentication required", False)
            if _OPERATOR_ROLE_RANK[role] < _OPERATOR_ROLE_RANK[required_operator_role]:
                return _error_response(403, "forbidden", "operator role insufficient for requested action", False)

        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        code_map = {
            400: ("invalid_request", False),
            401: ("unauthenticated", False),
            403: ("forbidden", False),
            404: ("not_found", False),
            409: ("conflict", False),
            429: ("rate_limited", True),
        }
        code, retryable = code_map.get(exc.status_code, ("internal_error", False))
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if "stale fencing token" in detail:
            code = "stale_fencing_token"
        if "lease" in detail and "expired" in detail:
            code = "lease_expired"
        return _error_response(exc.status_code, code, detail, retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, "invalid_request", str(exc), False)

    @app.exception_handler(Exception)
    async def generic_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        return _error_response(500, "internal_error", str(exc), False)

    app.include_router(router)
    return app
