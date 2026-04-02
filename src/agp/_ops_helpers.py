"""Operator helper functions that require direct DB/server access.

These were extracted from cli.py and will eventually migrate into
the skyops operator CLI package.
"""

import json
import sqlite3
import shutil
import socket
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from agp.config import settings
from agp.artifact_store import get_artifact_store
from agp.control_plane import (
    _block_job,
    _require_job,
    _unblock_job,
    build_app,
    sweep_expired_leases,
)
from agp._local_state import ensure_local_control_plane_stopped
from agp.db import SessionLocal, current_release_version, engine
from agp.enums import JobStatus
from agp.logs import prune_rotated_jsonl_family
from agp.models import Artifact, HandoffArtifact, Job, JobArtifact, QueueDeliveryRecord, Run, RunArtifact, SystemMetadata, utc_now
from agp.queue_backend import get_queue_backend


def _sqlite_db_path() -> Path:
    prefix = "sqlite+pysqlite:///"
    if not settings.database_url.startswith(prefix):
        raise ValueError("backup/restore currently supports sqlite+pysqlite URLs only")
    return Path(settings.database_url.removeprefix(prefix))


def create_backup_snapshot(*, backup_dir: str | Path) -> dict:
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    db_path = _sqlite_db_path()
    db_backup_path = backup_path / "agp.db"
    artifact_backup_path = backup_path / "artifacts"

    engine.dispose()
    if db_path.exists():
        with sqlite3.connect(db_path) as source_conn:
            with sqlite3.connect(db_backup_path) as backup_conn:
                source_conn.backup(backup_conn)

    if artifact_backup_path.exists():
        shutil.rmtree(artifact_backup_path)
    if settings.artifact_root.exists():
        shutil.copytree(settings.artifact_root, artifact_backup_path)
    else:
        artifact_backup_path.mkdir(parents=True, exist_ok=True)

    manifest = {
        "database_url": settings.database_url,
        "artifact_backend": settings.artifact_backend,
        "artifact_root": str(settings.artifact_root),
        "db_snapshot": str(db_backup_path),
        "artifact_snapshot": str(artifact_backup_path),
    }
    (backup_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def restore_backup_snapshot(
    *,
    backup_dir: str | Path,
    require_stopped_local_cp: bool = True,
) -> dict:
    backup_path = Path(backup_dir)
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"missing backup manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    db_path = _sqlite_db_path()
    db_backup_path = Path(manifest["db_snapshot"])
    artifact_backup_path = Path(manifest["artifact_snapshot"])

    if require_stopped_local_cp:
        ensure_local_control_plane_stopped(root=Path.cwd())
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    if db_backup_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_backup_path, db_path)
    else:
        from agp.migrations import apply_migrations
        apply_migrations()  # schema only; restore will repopulate data

    if settings.artifact_root.exists():
        shutil.rmtree(settings.artifact_root)
    if artifact_backup_path.exists():
        shutil.copytree(artifact_backup_path, settings.artifact_root)
    else:
        settings.artifact_root.mkdir(parents=True, exist_ok=True)

    return {
        "database_url": settings.database_url,
        "artifact_backend": settings.artifact_backend,
        "restored_from": str(backup_path),
    }


def restore_and_recover_snapshot(
    *,
    backup_dir: str | Path,
    validate_limit: int | None = None,
    require_stopped_local_cp: bool = True,
) -> dict:
    restored = restore_backup_snapshot(
        backup_dir=backup_dir,
        require_stopped_local_cp=require_stopped_local_cp,
    )
    validation = validate_restored_state(limit=validate_limit)
    reconstructed = reconstruct_queue_from_state()
    return {
        "restored": restored,
        "validation": validation,
        "queue_reconstruction": reconstructed,
        "ok": validation["ok"],
    }


def validate_restored_state(*, limit: int | None = None) -> dict:
    artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
    session = SessionLocal()
    try:
        query = select(Artifact).order_by(Artifact.created_at.asc())
        if limit is not None:
            query = query.limit(limit)
        artifacts = session.scalars(query).all()
        missing = [
            {
                "artifact_id": artifact.artifact_id,
                "storage_ref": artifact.storage_ref,
                "job_id": artifact.job_id,
                "run_id": artifact.run_id,
                "kind": artifact.kind,
            }
            for artifact in artifacts
            if not artifact_store.exists(storage_ref=artifact.storage_ref)
        ]
        return {
            "checked_artifacts": len(artifacts),
            "missing_artifacts": len(missing),
            "ok": len(missing) == 0,
            "missing": missing,
        }
    finally:
        session.close()


def reconstruct_queue_from_state() -> dict:
    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        jobs = session.scalars(
            select(Job).where(
                Job.status == JobStatus.QUEUED.value,
                Job.retry_count < Job.max_retries,
            )
        ).all()
        reconstructed = 0
        for job in jobs:
            session.execute(
                delete(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)
            )
            backend.enqueue_job(session, job=job)
            reconstructed += 1
        session.commit()
        return {
            "queue_backend": settings.queue_backend,
            "reconstructed_jobs": reconstructed,
        }
    finally:
        session.close()


def prune_observability_logs() -> dict:
    control_plane = prune_rotated_jsonl_family(
        settings.log_root / "control-plane.jsonl",
        retention_days=settings.observability_control_plane_log_retention_days,
    )
    runtime_deleted = 0
    runtime_kept = 0
    runtime_families = {
        settings.log_root / f"{path.stem.split('.', 1)[0]}.jsonl"
        for path in settings.log_root.glob("runtime-*.jsonl")
    }
    for path in runtime_families:
        outcome = prune_rotated_jsonl_family(
            path,
            retention_days=settings.observability_runtime_log_retention_days,
        )
        runtime_deleted += outcome["deleted"]
        runtime_kept += outcome["kept"]
    return {
        "control_plane": control_plane,
        "runtime_logs": {"deleted": runtime_deleted, "kept": runtime_kept},
        "retention_days": {
            "control_plane": settings.observability_control_plane_log_retention_days,
            "runtime": settings.observability_runtime_log_retention_days,
        },
    }


def run_failure_injection_scenario(*, scenario: str) -> dict:
    if scenario not in {
        "lease_expiry_requeue",
        "duplicate_terminal_replay",
        "artifact_store_write_failure",
        "queue_redelivery_after_consumer_restart",
        "repeated_fencing_stale_owner",
        "control_plane_restart_active_work",
    }:
        raise ValueError(f"unsupported failure-injection scenario: {scenario}")

    stamp = utc_now().strftime("%H%M%S%f")
    agent_id = f"agt_fi_{stamp}"
    runtime_id = f"rtm_fi_{stamp}"
    idempotency_key = f"failure-injection-{stamp}"

    if scenario == "control_plane_restart_active_work":
        original_backend = settings.queue_backend
        settings.queue_backend = "delivery_table"
        client = TestClient(build_app())
        try:
            agent = client.post("/agents/up", json={"agent_id": agent_id, "capabilities": ["python"]})
            if agent.status_code != 200:
                raise ValueError(f"failed to provision drill agent: {agent.text}")
            sent = client.post(
                "/messages/send",
                json={
                    "target": {"type": "agent", "id": agent_id},
                    "message": {"text": "failure injection control plane restart", "metadata": {"scenario": scenario}},
                },
                headers={"Idempotency-Key": idempotency_key},
            )
            if sent.status_code != 200:
                raise ValueError(f"failed to queue drill job: {sent.text}")
            job_id = sent.json()["data"]["job_id"]
            runtime = client.post(
                "/runtimes/register",
                json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
            )
            if runtime.status_code != 200:
                raise ValueError(f"failed to register drill runtime: {runtime.text}")
            claim = client.post(
                "/runs/claim",
                json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 1},
            )
            if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
                raise ValueError(f"failed to claim pre-restart drill job: {claim.text}")
            first_claim = claim.json()["data"]
            first_run_id = first_claim["run"]["run_id"]

            heartbeat = client.post(
                f"/runs/{first_run_id}/heartbeat",
                json={
                    "runtime_id": runtime_id,
                    "lease_id": first_claim["lease"]["lease_id"],
                    "fencing_token": first_claim["lease"]["fencing_token"],
                    "extend_seconds": 1,
                },
            )
            if heartbeat.status_code != 200:
                raise ValueError(f"failed to start pre-restart drill run: {heartbeat.text}")
        finally:
            client.close()

        restarted = TestClient(build_app())
        try:
            session = SessionLocal()
            try:
                sweep = sweep_expired_leases(
                    session,
                    now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
                )
                job_row = session.get(Job, job_id)
                if job_row is not None and job_row.status == JobStatus.QUEUED.value:
                    backend = get_queue_backend(settings.queue_backend)
                    backend.enqueue_job(session, job=job_row)
                    session.commit()
            finally:
                session.close()

            runtime2_id = f"{runtime_id}_restart"
            runtime2 = restarted.post(
                "/runtimes/register",
                json={"runtime_id": runtime2_id, "hostname": socket.gethostname()},
            )
            if runtime2.status_code != 200:
                raise ValueError(f"failed to register post-restart drill runtime: {runtime2.text}")
            reclaim = restarted.post(
                "/runs/claim",
                json={"runtime_id": runtime2_id, "agent_id": agent_id, "lease_ttl_seconds": 30},
            )
            if reclaim.status_code != 200 or not reclaim.json()["data"]["claimed"]:
                raise ValueError(f"failed to reclaim post-restart drill job: {reclaim.text}")
            second_claim = reclaim.json()["data"]
            second_run_id = second_claim["run"]["run_id"]

            artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
            prompt_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="prompt.txt", content="prompt\n", role="prompt",
            )
            transcript_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="transcript.txt", content="transcript\n", role="transcript_log",
            )
            exec_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="exec.txt", content="exec\n", role="exec_log",
            )
            result_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="result.txt", content="result\n", role="result",
            )
            artifacts = [
                {"role": prompt_ref.role, "storage_ref": prompt_ref.storage_ref, "content_type": prompt_ref.content_type, "checksum": prompt_ref.checksum, "size_bytes": prompt_ref.size_bytes},
                {"role": transcript_ref.role, "storage_ref": transcript_ref.storage_ref, "content_type": transcript_ref.content_type, "checksum": transcript_ref.checksum, "size_bytes": transcript_ref.size_bytes},
                {"role": exec_ref.role, "storage_ref": exec_ref.storage_ref, "content_type": exec_ref.content_type, "checksum": exec_ref.checksum, "size_bytes": exec_ref.size_bytes},
                {"role": result_ref.role, "storage_ref": result_ref.storage_ref, "content_type": result_ref.content_type, "checksum": result_ref.checksum, "size_bytes": result_ref.size_bytes},
            ]
            complete = restarted.post(
                f"/runs/{second_run_id}/complete",
                json={
                    "runtime_id": runtime2_id,
                    "lease_id": second_claim["lease"]["lease_id"],
                    "fencing_token": second_claim["lease"]["fencing_token"],
                    "artifacts": artifacts,
                    "summary": {"scenario": scenario},
                },
            )
            session = SessionLocal()
            try:
                first_run = session.get(Run, first_run_id)
                second_run = session.get(Run, second_run_id)
                run_count = int(session.scalar(select(func.count()).select_from(Run).where(Run.job_id == job_id)) or 0)
            finally:
                session.close()
            job = restarted.get(f"/jobs/{job_id}").json()["data"]
            events = restarted.get(f"/jobs/{job_id}/events").json()["data"]["items"]
            return {
                "scenario": scenario,
                "job_id": job_id,
                "first_run_id": first_run_id,
                "second_run_id": second_run_id,
                "job_status": job["status"],
                "sweep": sweep,
                "run_count": run_count,
                "first_run_status": first_run.status if first_run is not None else None,
                "second_run_status": second_run.status if second_run is not None else None,
                "complete_status_code": complete.status_code,
                "event_types": [item["event_type"] for item in events],
            }
        finally:
            restarted.close()
            settings.queue_backend = original_backend

    with TestClient(build_app()) as client:
        agent = client.post("/agents/up", json={"agent_id": agent_id, "capabilities": ["python"]})
        if agent.status_code != 200:
            raise ValueError(f"failed to provision drill agent: {agent.text}")

        sent = client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": agent_id},
                "message": {"text": "failure injection lease expiry", "metadata": {"scenario": scenario}},
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        if sent.status_code != 200:
            raise ValueError(f"failed to queue drill job: {sent.text}")
        job_id = sent.json()["data"]["job_id"]

        if scenario == "queue_redelivery_after_consumer_restart":
            original_backend = settings.queue_backend
            settings.queue_backend = "delivery_table"
            session = SessionLocal()
            try:
                backend = get_queue_backend(settings.queue_backend)
                delivery = backend.dequeue_candidate(session, target_queues=[f"agent:{agent_id}"])
                if delivery is None:
                    raise ValueError("failed to dequeue drill delivery before simulated restart")
                record = session.get(QueueDeliveryRecord, delivery.delivery_id)
                if record is None:
                    raise ValueError("missing delivery record for simulated restart")
                record.last_delivered_at = utc_now() - timedelta(seconds=60)
                session.commit()
                redrive = backend.redrive_stale_deliveries(
                    session,
                    visibility_timeout_seconds=30,
                    max_delivery_attempts=settings.queue_max_delivery_attempts,
                )
                session.commit()
            finally:
                session.close()

            runtime = client.post(
                "/runtimes/register",
                json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
            )
            if runtime.status_code != 200:
                settings.queue_backend = original_backend
                raise ValueError(f"failed to register drill runtime: {runtime.text}")

            claim = client.post(
                "/runs/claim",
                json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 30},
            )
            if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
                settings.queue_backend = original_backend
                raise ValueError(f"failed to reclaim redriven drill job: {claim.text}")
            claim_data = claim.json()["data"]
            run_id = claim_data["run"]["run_id"]

            artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
            prompt_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="prompt.txt", content="prompt\n", role="prompt",
            )
            transcript_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="transcript.txt", content="transcript\n", role="transcript_log",
            )
            exec_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="exec.txt", content="exec\n", role="exec_log",
            )
            result_ref = artifact_store.write_text(
                namespace="failure-injection", job_id=job_id, name="result.txt", content="result\n", role="result",
            )
            artifacts = [
                {"role": prompt_ref.role, "storage_ref": prompt_ref.storage_ref, "content_type": prompt_ref.content_type, "checksum": prompt_ref.checksum, "size_bytes": prompt_ref.size_bytes},
                {"role": transcript_ref.role, "storage_ref": transcript_ref.storage_ref, "content_type": transcript_ref.content_type, "checksum": transcript_ref.checksum, "size_bytes": transcript_ref.size_bytes},
                {"role": exec_ref.role, "storage_ref": exec_ref.storage_ref, "content_type": exec_ref.content_type, "checksum": exec_ref.checksum, "size_bytes": exec_ref.size_bytes},
                {"role": result_ref.role, "storage_ref": result_ref.storage_ref, "content_type": result_ref.content_type, "checksum": result_ref.checksum, "size_bytes": result_ref.size_bytes},
            ]
            complete = client.post(
                f"/runs/{run_id}/complete",
                json={
                    "runtime_id": runtime_id,
                    "lease_id": claim_data["lease"]["lease_id"],
                    "fencing_token": claim_data["lease"]["fencing_token"],
                    "artifacts": artifacts,
                    "summary": {"scenario": scenario},
                },
            )
            session = SessionLocal()
            try:
                run_count = int(session.scalar(select(func.count()).select_from(Run).where(Run.job_id == job_id)) or 0)
            finally:
                session.close()
            job = client.get(f"/jobs/{job_id}").json()["data"]
            events = client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
            settings.queue_backend = original_backend
            return {
                "scenario": scenario,
                "job_id": job_id,
                "run_id": run_id,
                "job_status": job["status"],
                "redrive": redrive,
                "claim_succeeded": True,
                "run_count": run_count,
                "complete_status_code": complete.status_code,
                "event_types": [item["event_type"] for item in events],
            }

        if scenario == "repeated_fencing_stale_owner":
            runtime = client.post(
                "/runtimes/register",
                json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
            )
            if runtime.status_code != 200:
                raise ValueError(f"failed to register drill runtime: {runtime.text}")
            stale_attempts: list[dict] = []
            expired_total = 0
            run_ids: list[str] = []
            final_job_status = "queued"
            for _ in range(3):
                claim = client.post(
                    "/runs/claim",
                    json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 1},
                )
                if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
                    raise ValueError(f"failed to claim fencing drill job: {claim.text}")
                claim_data = claim.json()["data"]
                current_run_id = claim_data["run"]["run_id"]
                run_ids.append(current_run_id)

                session = SessionLocal()
                try:
                    sweep = sweep_expired_leases(
                        session,
                        now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
                    )
                    job_row = session.get(Job, job_id)
                    if job_row is not None and job_row.status == JobStatus.QUEUED.value:
                        backend = get_queue_backend(settings.queue_backend)
                        backend.enqueue_job(session, job=job_row)
                        session.commit()
                finally:
                    session.close()
                expired_total += sweep["expired_leases"]

                stale = client.post(
                    f"/runs/{current_run_id}/cancel",
                    json={
                        "runtime_id": runtime_id,
                        "lease_id": claim_data["lease"]["lease_id"],
                        "fencing_token": claim_data["lease"]["fencing_token"],
                        "reason": "stale-owner-drill",
                    },
                )
                stale_attempts.append(
                    {"status_code": stale.status_code, "body": stale.json()}
                )
                final_job_status = client.get(f"/jobs/{job_id}").json()["data"]["status"]
                if final_job_status != "queued":
                    break

            alerts = client.get("/observability/alerts").json()["data"]
            events = client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
            return {
                "scenario": scenario,
                "job_id": job_id,
                "run_ids": run_ids,
                "job_status": final_job_status,
                "expired_leases": expired_total,
                "stale_attempts": stale_attempts,
                "alert_codes": [item["code"] for item in alerts["items"]],
                "event_types": [item["event_type"] for item in events],
            }

        runtime = client.post(
            "/runtimes/register",
            json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
        )
        if runtime.status_code != 200:
            raise ValueError(f"failed to register drill runtime: {runtime.text}")

        claim = client.post(
            "/runs/claim",
            json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 1},
        )
        if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
            raise ValueError(f"failed to claim drill job: {claim.text}")
        claim_data = claim.json()["data"]
        run_id = claim_data["run"]["run_id"]

        if scenario == "lease_expiry_requeue":
            session = SessionLocal()
            try:
                sweep = sweep_expired_leases(
                    session,
                    now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
                )
            finally:
                session.close()

            job = client.get(f"/jobs/{job_id}").json()["data"]
            events = client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
            return {
                "scenario": scenario,
                "job_id": job_id,
                "run_id": run_id,
                "status": job["status"],
                "retry_count": job["retry_count"],
                "sweep": sweep,
                "event_types": [item["event_type"] for item in events],
            }

        if scenario == "artifact_store_write_failure":
            heartbeat = client.post(
                f"/runs/{run_id}/heartbeat",
                json={
                    "runtime_id": runtime_id,
                    "lease_id": claim_data["lease"]["lease_id"],
                    "fencing_token": claim_data["lease"]["fencing_token"],
                    "extend_seconds": 30,
                },
            )
            if heartbeat.status_code != 200:
                raise ValueError(f"failed to heartbeat drill run: {heartbeat.text}")
            missing_refs = [
                {"role": "prompt", "storage_ref": "file:///definitely-missing-fi/prompt.txt", "content_type": "text/plain", "checksum": "", "size_bytes": 7},
                {"role": "transcript_log", "storage_ref": "file:///definitely-missing-fi/transcript.txt", "content_type": "text/plain", "checksum": "", "size_bytes": 11},
                {"role": "exec_log", "storage_ref": "file:///definitely-missing-fi/exec.txt", "content_type": "text/plain", "checksum": "", "size_bytes": 5},
                {"role": "result", "storage_ref": "file:///definitely-missing-fi/result.txt", "content_type": "text/plain", "checksum": "", "size_bytes": 7},
            ]
            terminal = client.post(
                f"/runs/{run_id}/complete",
                json={
                    "runtime_id": runtime_id,
                    "lease_id": claim_data["lease"]["lease_id"],
                    "fencing_token": claim_data["lease"]["fencing_token"],
                    "artifacts": missing_refs,
                    "summary": {"scenario": scenario},
                },
            )
            job = client.get(f"/jobs/{job_id}").json()["data"]
            session = SessionLocal()
            try:
                run = session.get(Run, run_id)
                from agp.models import Lease
                lease = session.get(Lease, claim_data["lease"]["lease_id"])
                artifact_count = int(
                    session.scalar(
                        select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id)
                    )
                    or 0
                )
            finally:
                session.close()
            return {
                "scenario": scenario,
                "job_id": job_id,
                "run_id": run_id,
                "job_status": job["status"],
                "run_status": run.status if run is not None else None,
                "lease_status": lease.status if lease is not None else None,
                "artifact_count": artifact_count,
                "terminal_status_code": terminal.status_code,
                "terminal_body": terminal.json(),
            }

        artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
        prompt_ref = artifact_store.write_text(namespace="failure-injection", job_id=job_id, name="prompt.txt", content="prompt\n", role="prompt")
        transcript_ref = artifact_store.write_text(namespace="failure-injection", job_id=job_id, name="transcript.txt", content="transcript\n", role="transcript_log")
        exec_ref = artifact_store.write_text(namespace="failure-injection", job_id=job_id, name="exec.txt", content="exec\n", role="exec_log")
        result_ref = artifact_store.write_text(namespace="failure-injection", job_id=job_id, name="result.txt", content="result\n", role="result")
        artifacts = [
            {"role": prompt_ref.role, "storage_ref": prompt_ref.storage_ref, "content_type": prompt_ref.content_type, "checksum": prompt_ref.checksum, "size_bytes": prompt_ref.size_bytes},
            {"role": transcript_ref.role, "storage_ref": transcript_ref.storage_ref, "content_type": transcript_ref.content_type, "checksum": transcript_ref.checksum, "size_bytes": transcript_ref.size_bytes},
            {"role": exec_ref.role, "storage_ref": exec_ref.storage_ref, "content_type": exec_ref.content_type, "checksum": exec_ref.checksum, "size_bytes": exec_ref.size_bytes},
            {"role": result_ref.role, "storage_ref": result_ref.storage_ref, "content_type": result_ref.content_type, "checksum": result_ref.checksum, "size_bytes": result_ref.size_bytes},
        ]
        first = client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": runtime_id,
                "lease_id": claim_data["lease"]["lease_id"],
                "fencing_token": claim_data["lease"]["fencing_token"],
                "artifacts": artifacts,
                "summary": {"scenario": scenario},
            },
        )
        second = client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": runtime_id,
                "lease_id": claim_data["lease"]["lease_id"],
                "fencing_token": claim_data["lease"]["fencing_token"],
                "artifacts": artifacts,
                "summary": {"scenario": scenario, "replay": True},
            },
        )
        job = client.get(f"/jobs/{job_id}").json()["data"]
        events = client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        return {
            "scenario": scenario,
            "job_id": job_id,
            "run_id": run_id,
            "status": job["status"],
            "result_artifact_id": job.get("result_artifact_id"),
            "first_terminal_status_code": first.status_code,
            "second_terminal_status_code": second.status_code,
            "second_terminal_body": second.json(),
            "event_types": [item["event_type"] for item in events],
        }


def get_upgrade_status() -> dict:
    session = SessionLocal()
    try:
        entries = {
            row.key: row.value
            for row in session.scalars(select(SystemMetadata)).all()
        }
    finally:
        session.close()
    return {
        "release_version": entries.get("release_version", current_release_version()),
        "schema_version": entries.get("schema_version", "unknown"),
        "previous_release_version": entries.get("previous_release_version"),
        "previous_schema_version": entries.get("previous_schema_version"),
        "package_version": current_release_version(),
        "rollback_target_release_version": entries.get("previous_release_version"),
        "rollback_target_schema_version": entries.get("previous_schema_version"),
    }


def mark_upgrade(*, schema_version: str, release_version: str) -> dict:
    session = SessionLocal()
    try:
        now = utc_now()

        def _get(key: str) -> SystemMetadata | None:
            return session.get(SystemMetadata, key)

        def _set(key: str, value: str) -> None:
            row = _get(key)
            if row is None:
                session.add(SystemMetadata(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

        current_release = _get("release_version")
        current_schema = _get("schema_version")
        if current_release is not None:
            _set("previous_release_version", current_release.value)
        if current_schema is not None:
            _set("previous_schema_version", current_schema.value)
        _set("release_version", release_version)
        _set("schema_version", schema_version)
        session.commit()
    finally:
        session.close()
    return get_upgrade_status()


def rollback_to_previous_version() -> dict:
    session = SessionLocal()
    try:
        now = utc_now()

        def _get(key: str) -> SystemMetadata | None:
            return session.get(SystemMetadata, key)

        def _set(key: str, value: str) -> None:
            row = _get(key)
            if row is None:
                session.add(SystemMetadata(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

        previous_release = _get("previous_release_version")
        previous_schema = _get("previous_schema_version")
        current_release = _get("release_version")
        current_schema = _get("schema_version")
        if previous_release is None or previous_schema is None:
            raise ValueError("no rollback target is currently recorded")
        if current_release is None or current_schema is None:
            raise ValueError("current upgrade metadata is incomplete")

        previous_release_value = previous_release.value
        previous_schema_value = previous_schema.value
        current_release_value = current_release.value
        current_schema_value = current_schema.value

        _set("release_version", previous_release_value)
        _set("schema_version", previous_schema_value)
        _set("previous_release_version", current_release_value)
        _set("previous_schema_version", current_schema_value)
        session.commit()
    finally:
        session.close()
    return get_upgrade_status()


def detect_orphan_artifacts(*, limit: int | None = None) -> dict:
    """Find artifact records not referenced by any job, run, or handoff link.

    Orphans are artifacts in the ``artifacts`` table with no corresponding
    row in ``job_artifacts``, ``run_artifacts``, or ``handoff_artifacts``.
    """
    from sqlalchemy import exists

    session = SessionLocal()
    try:
        is_referenced = (
            exists().where(JobArtifact.artifact_id == Artifact.artifact_id)
            | exists().where(RunArtifact.artifact_id == Artifact.artifact_id)
            | exists().where(HandoffArtifact.artifact_id == Artifact.artifact_id)
        )
        query = (
            select(Artifact.artifact_id, Artifact.job_id, Artifact.run_id, Artifact.kind, Artifact.created_at)
            .where(~is_referenced)
            .order_by(Artifact.created_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        rows = session.execute(query).all()
        orphans = [
            {
                "artifact_id": r.artifact_id,
                "job_id": r.job_id,
                "run_id": r.run_id,
                "kind": r.kind,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"orphan_count": len(orphans), "orphans": orphans}
    finally:
        session.close()
