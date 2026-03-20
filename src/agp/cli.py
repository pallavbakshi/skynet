"""CLI entrypoint for the AGP scaffold."""

import json
import socket
import shutil
from datetime import timedelta
from threading import Event
from time import sleep
from pathlib import Path

import httpx
import uvicorn
import typer
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from agp.config import settings
from agp.artifact_store import get_artifact_store
from agp.control_plane import (
    _block_job,
    _require_job,
    _unblock_job,
    build_app,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_idle_agents,
    sweep_draining_agents,
    sweep_stale_runtimes,
)
from agp.db import Base, SessionLocal, current_release_version, engine, init_db
from agp.enums import JobStatus
from agp.logs import prune_rotated_jsonl_family
from agp.models import Artifact, Capability, CapabilityPool, Job, Lease, QueueDeliveryRecord, Run, SystemMetadata, utc_now
from agp.queue_backend import get_queue_backend
from agp.plugins import build_terminal_host, build_agent_adapter
from agp.runtime import (
    RuntimeClient,
    RuntimeIdentity,
    RuntimeSupervisor,
    StandalonePluginRunner,
    TerminalSession,
)
from agp.sweeper import LeaseSweeperService, SweeperService

app = typer.Typer(help="AGP control plane scaffold")
host_app = typer.Typer(help="Standalone terminal host debugging commands")
adapter_app = typer.Typer(help="Standalone agent adapter debugging commands")
plugin_app = typer.Typer(help="Standalone integrated plugin runner commands")
app.add_typer(host_app, name="host")
app.add_typer(adapter_app, name="adapter")
app.add_typer(plugin_app, name="plugin")


def _sqlite_db_path() -> Path:
    prefix = "sqlite+pysqlite:///"
    if not settings.database_url.startswith(prefix):
        raise typer.BadParameter("backup/restore currently supports sqlite+pysqlite URLs only")
    return Path(settings.database_url.removeprefix(prefix))


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _host_kwargs(kind: str, workspace: str | None = None, runner: object | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if kind == "wezterm" and workspace:
        kwargs["workspace"] = workspace
    if runner is not None:
        kwargs["runner"] = runner
    return kwargs


def _session(
    *,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
) -> TerminalSession:
    return TerminalSession(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)


def _read_task(*, task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise typer.BadParameter("provide either task or task-file, not both")
    if task_file:
        return Path(task_file).read_text(encoding="utf-8")
    if task is not None:
        return task
    raise typer.BadParameter("one of task or task-file is required")


def create_backup_snapshot(*, backup_dir: str | Path) -> dict:
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    db_path = _sqlite_db_path()
    db_backup_path = backup_path / "agp.db"
    artifact_backup_path = backup_path / "artifacts"

    engine.dispose()
    if db_path.exists():
        shutil.copy2(db_path, db_backup_path)

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
    (backup_path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def restore_backup_snapshot(*, backup_dir: str | Path) -> dict:
    backup_path = Path(backup_dir)
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"missing backup manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    db_path = _sqlite_db_path()
    db_backup_path = Path(manifest["db_snapshot"])
    artifact_backup_path = Path(manifest["artifact_snapshot"])

    engine.dispose()
    if db_backup_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_backup_path, db_path)
    else:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

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


def restore_and_recover_snapshot(*, backup_dir: str | Path, validate_limit: int | None = None) -> dict:
    restored = restore_backup_snapshot(backup_dir=backup_dir)
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
        raise typer.BadParameter(f"unsupported failure-injection scenario: {scenario}")

    stamp = utc_now().strftime("%H%M%S%f")
    agent_id = f"agt_fi_{stamp}"
    runtime_id = f"rtm_fi_{stamp}"
    idempotency_key = f"failure-injection-{stamp}"

    if scenario == "control_plane_restart_active_work":
        original_backend = settings.queue_backend
        settings.queue_backend = "delivery_table"
        client = TestClient(build_app())
        try:
            agent = client.post("/agents/up", json={"agent_id": agent_id, "capability_id": "cap_python"})
            if agent.status_code != 200:
                raise typer.BadParameter(f"failed to provision drill agent: {agent.text}")
            sent = client.post(
                "/messages/send",
                json={
                    "target": {"type": "agent", "id": agent_id},
                    "message": {"text": "failure injection control plane restart", "metadata": {"scenario": scenario}},
                },
                headers={"Idempotency-Key": idempotency_key},
            )
            if sent.status_code != 200:
                raise typer.BadParameter(f"failed to queue drill job: {sent.text}")
            job_id = sent.json()["data"]["job_id"]
            runtime = client.post(
                "/runtimes/register",
                json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
            )
            if runtime.status_code != 200:
                raise typer.BadParameter(f"failed to register drill runtime: {runtime.text}")
            claim = client.post(
                "/runs/claim",
                json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 1},
            )
            if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
                raise typer.BadParameter(f"failed to claim pre-restart drill job: {claim.text}")
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
                raise typer.BadParameter(f"failed to start pre-restart drill run: {heartbeat.text}")
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
                raise typer.BadParameter(f"failed to register post-restart drill runtime: {runtime2.text}")
            reclaim = restarted.post(
                "/runs/claim",
                json={"runtime_id": runtime2_id, "agent_id": agent_id, "lease_ttl_seconds": 30},
            )
            if reclaim.status_code != 200 or not reclaim.json()["data"]["claimed"]:
                raise typer.BadParameter(f"failed to reclaim post-restart drill job: {reclaim.text}")
            second_claim = reclaim.json()["data"]
            second_run_id = second_claim["run"]["run_id"]

            artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
            prompt_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="prompt.txt",
                content="prompt\n",
                role="prompt",
            )
            transcript_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="transcript.txt",
                content="transcript\n",
                role="transcript_log",
            )
            exec_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="exec.txt",
                content="exec\n",
                role="exec_log",
            )
            result_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="result.txt",
                content="result\n",
                role="result",
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
        agent = client.post("/agents/up", json={"agent_id": agent_id, "capability_id": "cap_python"})
        if agent.status_code != 200:
            raise typer.BadParameter(f"failed to provision drill agent: {agent.text}")

        sent = client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": agent_id},
                "message": {"text": "failure injection lease expiry", "metadata": {"scenario": scenario}},
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        if sent.status_code != 200:
            raise typer.BadParameter(f"failed to queue drill job: {sent.text}")
        job_id = sent.json()["data"]["job_id"]

        if scenario == "queue_redelivery_after_consumer_restart":
            original_backend = settings.queue_backend
            settings.queue_backend = "delivery_table"
            session = SessionLocal()
            try:
                backend = get_queue_backend(settings.queue_backend)
                delivery = backend.dequeue_candidate(session, target_queues=[f"agent:{agent_id}"])
                if delivery is None:
                    raise typer.BadParameter("failed to dequeue drill delivery before simulated restart")
                record = session.get(QueueDeliveryRecord, delivery.delivery_id)
                if record is None:
                    raise typer.BadParameter("missing delivery record for simulated restart")
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
                raise typer.BadParameter(f"failed to register drill runtime: {runtime.text}")

            claim = client.post(
                "/runs/claim",
                json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 30},
            )
            if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
                settings.queue_backend = original_backend
                raise typer.BadParameter(f"failed to reclaim redriven drill job: {claim.text}")
            claim_data = claim.json()["data"]
            run_id = claim_data["run"]["run_id"]

            artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
            prompt_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="prompt.txt",
                content="prompt\n",
                role="prompt",
            )
            transcript_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="transcript.txt",
                content="transcript\n",
                role="transcript_log",
            )
            exec_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="exec.txt",
                content="exec\n",
                role="exec_log",
            )
            result_ref = artifact_store.write_text(
                namespace="failure-injection",
                job_id=job_id,
                name="result.txt",
                content="result\n",
                role="result",
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
                raise typer.BadParameter(f"failed to register drill runtime: {runtime.text}")
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
                    raise typer.BadParameter(f"failed to claim fencing drill job: {claim.text}")
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
                    {
                        "status_code": stale.status_code,
                        "body": stale.json(),
                    }
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
            raise typer.BadParameter(f"failed to register drill runtime: {runtime.text}")

        claim = client.post(
            "/runs/claim",
            json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": 1},
        )
        if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
            raise typer.BadParameter(f"failed to claim drill job: {claim.text}")
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
                raise typer.BadParameter(f"failed to heartbeat drill run: {heartbeat.text}")
            missing_refs = [
                {
                    "role": "prompt",
                    "storage_ref": "file:///definitely-missing-fi/prompt.txt",
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": 7,
                },
                {
                    "role": "transcript_log",
                    "storage_ref": "file:///definitely-missing-fi/transcript.txt",
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": 11,
                },
                {
                    "role": "exec_log",
                    "storage_ref": "file:///definitely-missing-fi/exec.txt",
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": 5,
                },
                {
                    "role": "result",
                    "storage_ref": "file:///definitely-missing-fi/result.txt",
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": 7,
                },
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
        prompt_ref = artifact_store.write_text(
            namespace="failure-injection",
            job_id=job_id,
            name="prompt.txt",
            content="prompt\n",
            role="prompt",
        )
        transcript_ref = artifact_store.write_text(
            namespace="failure-injection",
            job_id=job_id,
            name="transcript.txt",
            content="transcript\n",
            role="transcript_log",
        )
        exec_ref = artifact_store.write_text(
            namespace="failure-injection",
            job_id=job_id,
            name="exec.txt",
            content="exec\n",
            role="exec_log",
        )
        result_ref = artifact_store.write_text(
            namespace="failure-injection",
            job_id=job_id,
            name="result.txt",
            content="result\n",
            role="result",
        )
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
            raise typer.BadParameter("no rollback target is currently recorded")
        if current_release is None or current_schema is None:
            raise typer.BadParameter("current upgrade metadata is incomplete")

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


def _build_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def send_message_via_api(
    client: httpx.Client,
    *,
    target_type: str,
    target_id: str,
    text: str,
    metadata: dict | None = None,
    detach_mode: str = "auto",
    idempotency_key: str | None = None,
) -> dict:
    headers = {}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    response = client.post(
        "/messages/send",
        json={
            "target": {"type": target_type, "id": target_id},
            "message": {"text": text, "metadata": metadata or {}},
            "detach_policy": {"mode": detach_mode},
        },
        headers=headers,
    )
    response.raise_for_status()
    return response.json()["data"]


def list_jobs_via_api(
    client: httpx.Client,
    *,
    status: str | None = None,
    target_agent_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    params: dict[str, object] = {"limit": limit}
    if status is not None:
        params["status"] = status
    if target_agent_id is not None:
        params["target_agent_id"] = target_agent_id
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get("/jobs", params=params)
    response.raise_for_status()
    return response.json()["data"]


def list_agents_via_api(
    client: httpx.Client,
    *,
    status: str | None = None,
    capability_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    params: dict[str, object] = {"limit": limit}
    if status is not None:
        params["status"] = status
    if capability_id is not None:
        params["capability_id"] = capability_id
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get("/agents", params=params)
    response.raise_for_status()
    return response.json()["data"]


def interrupt_job_via_api(client: httpx.Client, *, job_id: str) -> dict:
    response = client.post(f"/jobs/{job_id}/interrupt")
    response.raise_for_status()
    return response.json()["data"]


def fetch_artifact_via_api(client: httpx.Client, *, artifact_id: str, content: bool = False) -> dict:
    path = f"/artifacts/{artifact_id}/content" if content else f"/artifacts/{artifact_id}"
    response = client.get(path)
    response.raise_for_status()
    return response.json()["data"]


def list_job_artifacts_via_api(client: httpx.Client, *, job_id: str, role: str | None = None) -> dict:
    params: dict[str, object] = {}
    if role is not None:
        params["role"] = role
    response = client.get(f"/jobs/{job_id}/artifacts", params=params)
    response.raise_for_status()
    return response.json()["data"]


def list_run_artifacts_via_api(client: httpx.Client, *, run_id: str, role: str | None = None) -> dict:
    params: dict[str, object] = {}
    if role is not None:
        params["role"] = role
    response = client.get(f"/runs/{run_id}/artifacts", params=params)
    response.raise_for_status()
    return response.json()["data"]


def list_queue_deliveries_via_api(
    client: httpx.Client,
    *,
    state: str | None = None,
    job_id: str | None = None,
    target_queue: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    params: dict[str, object] = {"limit": limit}
    if state is not None:
        params["state"] = state
    if job_id is not None:
        params["job_id"] = job_id
    if target_queue is not None:
        params["target_queue"] = target_queue
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get("/queue/deliveries", params=params)
    response.raise_for_status()
    return response.json()["data"]


def observability_summary_via_api(client: httpx.Client) -> dict:
    response = client.get("/observability/summary")
    response.raise_for_status()
    return response.json()["data"]


def observability_job_trace_via_api(client: httpx.Client, *, job_id: str) -> dict:
    response = client.get(f"/observability/jobs/{job_id}/trace")
    response.raise_for_status()
    return response.json()["data"]


def observability_control_plane_logs_via_api(client: httpx.Client, *, limit: int = 100) -> dict:
    response = client.get("/observability/logs/control-plane", params={"limit": limit})
    response.raise_for_status()
    return response.json()["data"]


def observability_runtime_logs_via_api(client: httpx.Client, *, runtime_id: str, limit: int = 100) -> dict:
    response = client.get(f"/observability/logs/runtimes/{runtime_id}", params={"limit": limit})
    response.raise_for_status()
    return response.json()["data"]


def observability_alerts_via_api(client: httpx.Client) -> dict:
    response = client.get("/observability/alerts")
    response.raise_for_status()
    return response.json()["data"]


def observability_metrics_via_api(client: httpx.Client) -> str:
    response = client.get("/observability/metrics")
    response.raise_for_status()
    return response.text


def observability_dispatch_alerts_via_api(client: httpx.Client) -> dict:
    response = client.post("/observability/alerts/dispatch")
    response.raise_for_status()
    return response.json()["data"]


def system_auth_status_via_api(client: httpx.Client) -> dict:
    response = client.get("/system/auth-status")
    response.raise_for_status()
    return response.json()["data"]


def rotate_operator_tokens_via_api(
    client: httpx.Client,
    *,
    operator_bearer_token: str | None,
    operator_token_roles_json: dict[str, str],
) -> dict:
    response = client.post(
        "/system/tokens/operator",
        json={
            "operator_bearer_token": operator_bearer_token,
            "operator_token_roles_json": operator_token_roles_json,
        },
    )
    response.raise_for_status()
    return response.json()["data"]


def rotate_runtime_tokens_via_api(
    client: httpx.Client,
    *,
    runtime_bearer_token: str | None,
    runtime_active_tokens_json: list[str],
) -> dict:
    response = client.post(
        "/system/tokens/runtime",
        json={
            "runtime_bearer_token": runtime_bearer_token,
            "runtime_active_tokens_json": runtime_active_tokens_json,
        },
    )
    response.raise_for_status()
    return response.json()["data"]


def watch_job_until_terminal(
    client: httpx.Client,
    *,
    job_id: str,
    poll_interval_seconds: float = 0.25,
    limit: int = 100,
    max_polls: int | None = None,
) -> list[dict]:
    """Poll job state and ordered events until the job reaches terminal state."""

    terminal_statuses = {"completed", "failed", "cancelled"}
    cursor: str | None = None
    polls = 0
    snapshots: list[dict] = []

    while True:
        job_response = client.get(f"/jobs/{job_id}")
        job_response.raise_for_status()
        job_payload = job_response.json()["data"]

        params: dict[str, object] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        events_response = client.get(f"/jobs/{job_id}/events", params=params)
        events_response.raise_for_status()
        events_payload = events_response.json()["data"]
        cursor = events_payload["page"]["next_cursor"]

        snapshot = {"job": job_payload, "events": events_payload["items"]}
        snapshots.append(snapshot)

        if job_payload["status"] in terminal_statuses:
            return snapshots

        polls += 1
        if max_polls is not None and polls >= max_polls:
            return snapshots
        sleep(poll_interval_seconds)


@host_app.command("list-hosts")
def host_list_hosts() -> None:
    """List supported terminal host kinds."""

    _emit({"items": ["inprocess", "wezterm", "tmux"]})


@host_app.command("create")
def host_create(
    host_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session for one agent."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    _emit(
        {
            "host_kind": host.kind,
            "agent_id": agent_id,
            "session_id": session.session_id,
            "workspace_ref": session.workspace_ref,
            "metadata": session.metadata,
        }
    )


@host_app.command("exists")
def host_exists(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Check whether a session currently exists."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    _emit(
        {
            "host_kind": host.kind,
            "session_id": session_id,
            "agent_id": agent_id,
            "exists": host.session_exists(
                _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
            ),
        }
    )


@host_app.command("health")
def host_health(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Fetch session health."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    health = host.health(_session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref))
    _emit(
        {
            "host_kind": host.kind,
            "session_id": health.session_id,
            "exists": health.exists,
            "healthy": health.healthy,
            "reason": health.reason,
            "metadata": health.metadata,
        }
    )


@host_app.command("send")
def host_send(
    host_kind: str,
    session_id: str,
    agent_id: str,
    text: str,
    enter: bool = True,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Send text to an existing session."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.send_text(session, text, enter=enter)
    _emit(
        {
            "host_kind": host.kind,
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "sent": True,
            "enter": enter,
            "text": text,
        }
    )


@host_app.command("read")
def host_read(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Read visible output and one incremental output pass from a session."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    cursor = host.load_cursor(session) or host.create_cursor(session)
    read = host.read_output(session, cursor)
    visible_text = host.read_visible(session)
    _emit(
        {
            "host_kind": host.kind,
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "changed": read.changed,
            "text": read.text,
            "full_text": read.full_text or visible_text,
            "visible_text": visible_text,
            "cursor_metadata": read.cursor.metadata,
        }
    )


@host_app.command("snapshot")
def host_snapshot(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Capture a session snapshot."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    _emit(host.snapshot(session))


@host_app.command("interrupt")
def host_interrupt(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Interrupt a session."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.interrupt(session)
    _emit({"host_kind": host.kind, "session_id": session.session_id, "agent_id": session.agent_id, "interrupted": True})


@host_app.command("terminate")
def host_terminate(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Terminate a session."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.terminate_session(session)
    _emit({"host_kind": host.kind, "session_id": session.session_id, "agent_id": session.agent_id, "terminated": True})


@adapter_app.command("list-adapters")
def adapter_list_adapters() -> None:
    """List supported adapter kinds."""

    _emit({"items": ["default", "codex"]})


@adapter_app.command("bootstrap")
def adapter_bootstrap(
    adapter_kind: str,
    host_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session and bootstrap the adapter into it."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    adapter = build_agent_adapter(adapter_kind)
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    claimed = {
        "agent_id": agent_id,
        "job": {"job_id": "local-bootstrap"},
        "run": {"run_id": "local-bootstrap"},
        "message": {"text": "bootstrap", "metadata": {"standalone": True}},
        "lease": {"lease_id": "local-bootstrap", "fencing_token": 1},
    }
    adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
    _emit(
        {
            "host_kind": host.kind,
            "adapter_kind": adapter.kind,
            "session_id": session.session_id,
            "agent_id": agent_id,
            "metadata": session.metadata,
        }
    )


@adapter_app.command("inspect")
def adapter_inspect(
    adapter_kind: str,
    path: str,
    run_id: str | None = None,
) -> None:
    """Inspect a transcript or raw output file through one adapter."""

    adapter = build_agent_adapter(adapter_kind)
    text = Path(path).read_text(encoding="utf-8")
    _emit(adapter.inspect_output(text=text, run_id=run_id))


@adapter_app.command("run-once")
def adapter_run_once(
    adapter_kind: str,
    host_kind: str,
    agent_id: str,
    task: str | None = None,
    task_file: str | None = None,
    workspace_ref: str | None = None,
    output_root: str = ".agp-plugin-runs",
    keep_session: bool = False,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Run one standalone task through a host and adapter."""

    runner = StandalonePluginRunner(
        host=build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace)),
        adapter=build_agent_adapter(adapter_kind),
        output_root=output_root,
    )
    result = runner.run_once(
        agent_id=agent_id,
        task=_read_task(task=task, task_file=task_file),
        workspace_ref=workspace_ref,
        keep_session=keep_session,
    )
    _emit(result.to_dict())


@plugin_app.command("run")
def plugin_run(
    host_kind: str,
    adapter_kind: str,
    agent_id: str,
    task: str | None = None,
    task_file: str | None = None,
    workspace_ref: str | None = None,
    output_root: str = ".agp-plugin-runs",
    keep_session: bool = False,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Run one standalone task through the shared plugin interfaces."""

    runner = StandalonePluginRunner(
        host=build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace)),
        adapter=build_agent_adapter(adapter_kind),
        output_root=output_root,
    )
    result = runner.run_once(
        agent_id=agent_id,
        task=_read_task(task=task, task_file=task_file),
        workspace_ref=workspace_ref,
        keep_session=keep_session,
    )
    _emit(result.to_dict())


@plugin_app.command("repl")
def plugin_repl(
    host_kind: str,
    adapter_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session, bootstrap it, and stream tasks from stdin until exit."""

    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    adapter = build_agent_adapter(adapter_kind)
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    claimed = {
        "agent_id": agent_id,
        "job": {"job_id": "local-repl"},
        "run": {"run_id": "local-repl"},
        "message": {"text": "bootstrap", "metadata": {"standalone": True, "repl": True}},
        "lease": {"lease_id": "local-repl", "fencing_token": 1},
    }
    adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
    typer.echo(json.dumps({"host_kind": host.kind, "adapter_kind": adapter.kind, "session_id": session.session_id, "agent_id": agent_id}, sort_keys=True))
    while True:
        line = typer.prompt("task", prompt_suffix="> ", default="", show_default=False)
        if not line.strip():
            continue
        if line.strip() in {"/exit", "exit", "quit"}:
            break
        result = StandalonePluginRunner(host=host, adapter=adapter).run_once(
            agent_id=agent_id,
            task=line,
            workspace_ref=workspace_ref,
            keep_session=True,
        )
        _emit(result.to_dict())


@app.command()
def initdb() -> None:
    """Initialize the local database schema."""

    init_db()
    typer.echo("Initialized database schema.")


@app.command()
def backup_create(
    backup_dir: str,
) -> None:
    """Create a local backup snapshot of sqlite state and artifact root."""

    payload = create_backup_snapshot(backup_dir=backup_dir)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def backup_restore(
    backup_dir: str,
) -> None:
    """Restore a local backup snapshot of sqlite state and artifact root."""

    payload = restore_backup_snapshot(backup_dir=backup_dir)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def backup_recover(
    backup_dir: str,
    validate_limit: int | None = None,
) -> None:
    """Restore a backup, validate restored artifacts, and reconstruct queued backlog."""

    payload = restore_and_recover_snapshot(backup_dir=backup_dir, validate_limit=validate_limit)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def backup_validate(
    limit: int | None = None,
) -> None:
    """Validate restored artifact references against the configured artifact store."""

    payload = validate_restored_state(limit=limit)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def queue_reconstruct() -> None:
    """Reconstruct queued backlog in the configured queue backend from authoritative state."""

    payload = reconstruct_queue_from_state()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def upgrade_status() -> None:
    """Report persisted release/schema version state and rollback target."""

    typer.echo(json.dumps(get_upgrade_status(), indent=2, sort_keys=True))


@app.command()
def upgrade_mark(
    schema_version: str,
    release_version: str = current_release_version(),
) -> None:
    """Persist the current release/schema as the active upgrade state."""

    payload = mark_upgrade(schema_version=schema_version, release_version=release_version)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def upgrade_rollback() -> None:
    """Roll back persisted release/schema metadata to the immediately previous target."""

    payload = rollback_to_previous_version()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def serve(
    host: str = settings.host,
    port: int = settings.port,
) -> None:
    """Run the AGP control plane API server."""

    uvicorn.run(build_app(), host=host, port=port)


@app.command()
def add_capability(
    capability_id: str,
    name: str,
    image_ref: str,
    model_ref: str,
    version: str = "v1",
    resource_tier: str = "default",
    permission_profile: str = "default",
    queue_mode: str = "agent",
) -> None:
    """Insert a capability record for local development."""

    session = SessionLocal()
    try:
        if session.get(Capability, capability_id) is not None:
            raise typer.BadParameter(f"capability already exists: {capability_id}")
        session.add(
            Capability(
                capability_id=capability_id,
                name=name,
                version=version,
                image_ref=image_ref,
                model_ref=model_ref,
                resource_tier=resource_tier,
                permission_profile=permission_profile,
                queue_mode=queue_mode,
                runtime_requirements_json={},
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.flush()
        if session.get(CapabilityPool, capability_id) is None:
            session.add(
                CapabilityPool(
                    capability_id=capability_id,
                    queue_id=f"capability:{capability_id}:{version}",
                    routing_policy="least_recent",
                )
            )
        session.commit()
    finally:
        session.close()
    typer.echo(f"Created capability {capability_id}.")


@app.command()
def runtime_register(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    hostname: str = socket.gethostname(),
) -> None:
    """Register a runtime with the control plane."""

    client = RuntimeClient(RuntimeIdentity(runtime_id=runtime_id, hostname=hostname, server_url=server_url))
    try:
        payload = client.register()
    finally:
        client.close()
    typer.echo(payload)


@app.command()
def runtime_claim_once(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    agent_id: str | None = None,
    capability_id: str | None = None,
) -> None:
    """Claim one queued run for a runtime."""

    client = RuntimeClient(
        RuntimeIdentity(runtime_id=runtime_id, hostname=socket.gethostname(), server_url=server_url)
    )
    try:
        payload = client.claim(agent_id=agent_id, capability_id=capability_id)
    finally:
        client.close()
    typer.echo(payload)


@app.command()
def runtime_work_once(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    hostname: str = socket.gethostname(),
    agent_id: str | None = None,
    capability_id: str | None = None,
    artifact_root: str = ".agp-artifacts",
    max_local_recoveries: int = 1,
    host_kind: str = settings.runtime_terminal_host_kind,
    adapter_kind: str = settings.runtime_agent_adapter_kind,
) -> None:
    """Register a runtime, claim one job, heartbeat it, and complete/fail it."""

    client = RuntimeClient(
        RuntimeIdentity(runtime_id=runtime_id, hostname=hostname, server_url=server_url)
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(host_kind, workspace=settings.wezterm_workspace),
        adapter=build_agent_adapter(adapter_kind),
        artifact_root=artifact_root,
    )
    try:
        payload = worker.run_once(
            agent_id=agent_id,
            capability_id=capability_id,
            max_local_recoveries=max_local_recoveries,
        )
    finally:
        client.close()
    typer.echo(payload)


@app.command()
def runtime_work_loop(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    hostname: str = socket.gethostname(),
    agent_id: str | None = None,
    capability_id: str | None = None,
    artifact_root: str = ".agp-artifacts",
    idle_sleep_seconds: float = 0.25,
    max_iterations: int | None = None,
    max_local_recoveries: int = 1,
    host_kind: str = settings.runtime_terminal_host_kind,
    adapter_kind: str = settings.runtime_agent_adapter_kind,
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""

    client = RuntimeClient(
        RuntimeIdentity(runtime_id=runtime_id, hostname=hostname, server_url=server_url)
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(host_kind, workspace=settings.wezterm_workspace),
        adapter=build_agent_adapter(adapter_kind),
        artifact_root=artifact_root,
    )
    stop_event = Event()
    try:
        payload = worker.run_forever(
            agent_id=agent_id,
            capability_id=capability_id,
            idle_sleep_seconds=idle_sleep_seconds,
            max_iterations=max_iterations,
            stop_event=stop_event,
            max_local_recoveries=max_local_recoveries,
        )
    finally:
        stop_event.set()
        client.close()
    typer.echo(payload)


@app.command()
def sweep() -> None:
    """Expire stale leases and requeue or fail affected jobs."""

    session = SessionLocal()
    try:
        payload = sweep_expired_leases(session)
    finally:
        session.close()
    typer.echo(payload)


@app.command()
def sweep_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
) -> None:
    """Continuously expire stale leases on a fixed interval."""

    service = LeaseSweeperService(
        session_factory=SessionLocal,
        sweep_fn=sweep_expired_leases,
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


@app.command()
def queue_redrive(
    visibility_timeout_seconds: int = settings.queue_visibility_timeout_seconds,
    max_delivery_attempts: int = settings.queue_max_delivery_attempts,
) -> None:
    """Return stale in-flight queue deliveries back to pending state."""

    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        payload = backend.redrive_stale_deliveries(
            session,
            visibility_timeout_seconds=visibility_timeout_seconds,
            max_delivery_attempts=max_delivery_attempts,
        )
        session.commit()
        typer.echo({**payload, "queue_backend": settings.queue_backend})
    finally:
        session.close()


@app.command()
def sweep_idle(
    idle_timeout_seconds: int = settings.agent_idle_timeout_seconds,
) -> None:
    """Terminate truly idle agents that have exceeded the idle timeout."""

    session = SessionLocal()
    try:
        payload = sweep_idle_agents(session, idle_timeout_seconds=idle_timeout_seconds)
    finally:
        session.close()
    typer.echo(payload)


@app.command()
def sweep_runtimes(
    stale_timeout_seconds: int = settings.runtime_stale_timeout_seconds,
) -> None:
    """Mark stale runtimes offline and detach or degrade bound agents."""

    session = SessionLocal()
    try:
        payload = sweep_stale_runtimes(session, stale_timeout_seconds=stale_timeout_seconds)
    finally:
        session.close()
    typer.echo(payload)


@app.command()
def sweep_draining() -> None:
    """Terminate draining agents whose work and leases have fully cleared."""

    session = SessionLocal()
    try:
        payload = sweep_draining_agents(session)
    finally:
        session.close()
    typer.echo(payload)


@app.command()
def sweep_runtime_draining() -> None:
    """Return draining runtimes to idle once active leases have cleared."""

    session = SessionLocal()
    try:
        payload = sweep_draining_runtimes(session)
    finally:
        session.close()
    typer.echo(payload)


@app.command()
def sweep_runtimes_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
    stale_timeout_seconds: int = settings.runtime_stale_timeout_seconds,
) -> None:
    """Continuously mark stale runtimes offline and detach or degrade bound agents."""

    service = SweeperService(
        session_factory=SessionLocal,
        sweep_fn=lambda session: sweep_stale_runtimes(
            session,
            stale_timeout_seconds=stale_timeout_seconds,
        ),
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


@app.command()
def job_block(job_id: str, reason: str = "operator_blocked") -> None:
    """Move a queued job into blocked state."""

    session = SessionLocal()
    try:
        job = _require_job(session, job_id)
        _block_job(session, job=job, reason=reason)
        session.commit()
        typer.echo({"job_id": job_id, "status": job.status})
    finally:
        session.close()


@app.command()
def job_unblock(job_id: str, reason: str = "operator_unblocked") -> None:
    """Move a blocked job back to queued state."""

    session = SessionLocal()
    try:
        job = _require_job(session, job_id)
        _unblock_job(session, job=job, reason=reason)
        session.commit()
        typer.echo({"job_id": job_id, "status": job.status})
    finally:
        session.close()


@app.command()
def watch_job(
    job_id: str,
    server_url: str = "http://127.0.0.1:7860",
    poll_interval_seconds: float = 0.25,
    limit: int = 100,
    operator_token: str | None = settings.operator_bearer_token,
    max_polls: int | None = None,
) -> None:
    """Poll job status and ordered job events until terminal state."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        snapshots = watch_job_until_terminal(
            client,
            job_id=job_id,
            poll_interval_seconds=poll_interval_seconds,
            limit=limit,
            max_polls=max_polls,
        )
    for snapshot in snapshots:
        typer.echo(json.dumps(snapshot, default=str))


@app.command()
def send(
    target_type: str,
    target_id: str,
    text: str,
    server_url: str = "http://127.0.0.1:7860",
    detach_mode: str = "auto",
    operator_token: str | None = settings.operator_bearer_token,
    idempotency_key: str | None = None,
) -> None:
    """Send orchestration work to an agent or capability target."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = send_message_via_api(
            client,
            target_type=target_type,
            target_id=target_id,
            text=text,
            detach_mode=detach_mode,
            idempotency_key=idempotency_key,
        )
    typer.echo(json.dumps(payload, default=str))


@app.command("list-jobs")
def list_jobs(
    server_url: str = "http://127.0.0.1:7860",
    status: str | None = None,
    target_agent_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """List jobs for orchestration inspection."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = list_jobs_via_api(
            client,
            status=status,
            target_agent_id=target_agent_id,
            limit=limit,
            cursor=cursor,
        )
    typer.echo(json.dumps(payload, default=str))


@app.command("list-agents")
def list_agents(
    server_url: str = "http://127.0.0.1:7860",
    status: str | None = None,
    capability_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """List durable agents for orchestration inspection."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = list_agents_via_api(
            client,
            status=status,
            capability_id=capability_id,
            limit=limit,
            cursor=cursor,
        )
    typer.echo(json.dumps(payload, default=str))


@app.command()
def interrupt(
    job_id: str,
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Interrupt queued or running work."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = interrupt_job_via_api(client, job_id=job_id)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def fetch(
    artifact_id: str,
    server_url: str = "http://127.0.0.1:7860",
    content: bool = False,
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch artifact metadata or content."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = fetch_artifact_via_api(client, artifact_id=artifact_id, content=content)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def list_deliveries(
    server_url: str = "http://127.0.0.1:7860",
    state: str | None = None,
    job_id: str | None = None,
    target_queue: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """List queue deliveries for operator inspection."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = list_queue_deliveries_via_api(
            client,
            state=state,
            job_id=job_id,
            target_queue=target_queue,
            limit=limit,
            cursor=cursor,
        )
    typer.echo(json.dumps(payload, default=str))


@app.command()
def observability(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch aggregated operator observability counters."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_summary_via_api(client)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def trace_job(
    job_id: str,
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch an ordered execution trace for one job."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_job_trace_via_api(client, job_id=job_id)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def logs_control_plane(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
    limit: int = 100,
) -> None:
    """Fetch recent structured control-plane lifecycle logs."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_control_plane_logs_via_api(client, limit=limit)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def logs_runtime(
    runtime_id: str,
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
    limit: int = 100,
) -> None:
    """Fetch recent structured runtime supervision logs for one runtime."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_runtime_logs_via_api(client, runtime_id=runtime_id, limit=limit)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def logs_prune() -> None:
    """Prune rotated observability logs older than configured retention windows."""

    payload = prune_observability_logs()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def failure_injection_run(
    scenario: str,
) -> None:
    """Run one built-in failure-injection drill against the local control-plane stack."""

    payload = run_failure_injection_scenario(scenario=scenario)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def observability_alerts(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch derived active observability alerts."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_alerts_via_api(client)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def observability_metrics(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch Prometheus-style metrics for AGP."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_metrics_via_api(client)
    typer.echo(payload, nl=False)


@app.command()
def observability_dispatch_alerts(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Dispatch current active alerts to the configured webhook."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = observability_dispatch_alerts_via_api(client)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def security_auth_status(
    server_url: str = "http://127.0.0.1:7860",
    operator_token: str | None = settings.operator_bearer_token,
) -> None:
    """Fetch security-admin auth posture without exposing token values."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(operator_token), timeout=10.0) as client:
        payload = system_auth_status_via_api(client)
    typer.echo(json.dumps(payload, default=str))


@app.command()
def security_rotate_operator(
    roles_json: str,
    server_url: str = "http://127.0.0.1:7860",
    operator_bearer_token: str | None = None,
    admin_token: str | None = settings.operator_bearer_token,
) -> None:
    """Rotate managed operator tokens and optional legacy admin token."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(admin_token), timeout=10.0) as client:
        payload = rotate_operator_tokens_via_api(
            client,
            operator_bearer_token=operator_bearer_token,
            operator_token_roles_json=json.loads(roles_json),
        )
    typer.echo(json.dumps(payload, default=str))


@app.command()
def security_rotate_runtime(
    active_tokens_json: str,
    server_url: str = "http://127.0.0.1:7860",
    runtime_bearer_token: str | None = None,
    admin_token: str | None = settings.operator_bearer_token,
) -> None:
    """Rotate runtime active tokens and optional legacy runtime token."""

    with httpx.Client(base_url=server_url.rstrip("/"), headers=_build_headers(admin_token), timeout=10.0) as client:
        payload = rotate_runtime_tokens_via_api(
            client,
            runtime_bearer_token=runtime_bearer_token,
            runtime_active_tokens_json=list(json.loads(active_tokens_json)),
        )
    typer.echo(json.dumps(payload, default=str))


if __name__ == "__main__":
    app()
