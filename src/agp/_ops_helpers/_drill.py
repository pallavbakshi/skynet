"""Failure injection drill scenarios."""

from __future__ import annotations

import socket
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agp.artifact_store import get_artifact_store
from agp.config import settings
from agp.control_plane import build_app, sweep_expired_leases
from agp.db import SessionLocal
from agp.enums import JobStatus
from agp.models import Artifact, Job, Lease, QueueDeliveryRecord, Run, utc_now
from agp.queue_backend import get_queue_backend

_SCENARIOS = {
    "lease_expiry_requeue",
    "duplicate_terminal_replay",
    "artifact_store_write_failure",
    "queue_redelivery_after_consumer_restart",
    "repeated_fencing_stale_owner",
    "control_plane_restart_active_work",
}


def _write_drill_artifacts(job_id: str) -> list[dict]:
    """Write standard drill artifacts and return the payload list."""
    store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
    refs = [
        store.write_text(namespace="failure-injection", job_id=job_id, name="prompt.txt", content="prompt\n", role="prompt"),
        store.write_text(namespace="failure-injection", job_id=job_id, name="transcript.txt", content="transcript\n", role="transcript_log"),
        store.write_text(namespace="failure-injection", job_id=job_id, name="exec.txt", content="exec\n", role="exec_log"),
        store.write_text(namespace="failure-injection", job_id=job_id, name="result.txt", content="result\n", role="result"),
    ]
    return [
        {"role": r.role, "storage_ref": r.storage_ref, "content_type": r.content_type,
         "checksum": r.checksum, "size_bytes": r.size_bytes}
        for r in refs
    ]


def _provision_and_send(client: TestClient, *, agent_id: str, idempotency_key: str, scenario: str) -> str:
    """Provision agent, send a drill job, return job_id."""
    agent = client.post("/agents/up", json={"agent_id": agent_id, "capabilities": ["python"]})
    if agent.status_code != 200:
        raise ValueError(f"failed to provision drill agent: {agent.text}")
    sent = client.post(
        "/messages/send",
        json={
            "target": {"type": "agent", "id": agent_id},
            "message": {"text": f"failure injection {scenario}", "metadata": {"scenario": scenario}},
        },
        headers={"Idempotency-Key": idempotency_key},
    )
    if sent.status_code != 200:
        raise ValueError(f"failed to queue drill job: {sent.text}")
    return sent.json()["data"]["job_id"]


def _register_and_claim(client: TestClient, *, runtime_id: str, agent_id: str, lease_ttl: int = 1) -> dict:
    """Register runtime and claim a job, return claim data."""
    runtime = client.post(
        "/runtimes/register",
        json={"runtime_id": runtime_id, "hostname": socket.gethostname()},
    )
    if runtime.status_code != 200:
        raise ValueError(f"failed to register drill runtime: {runtime.text}")
    claim = client.post(
        "/runs/claim",
        json={"runtime_id": runtime_id, "agent_id": agent_id, "lease_ttl_seconds": lease_ttl},
    )
    if claim.status_code != 200 or not claim.json()["data"]["claimed"]:
        raise ValueError(f"failed to claim drill job: {claim.text}")
    return claim.json()["data"]


def _sweep_and_requeue(job_id: str) -> dict:
    """Sweep expired leases and re-enqueue the job if still queued."""
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
    return sweep


def _job_events(client: TestClient, job_id: str) -> list[str]:
    return [item["event_type"] for item in client.get(f"/jobs/{job_id}/events").json()["data"]["items"]]


# ── Scenario implementations ────────────────────────────────────────


def _drill_control_plane_restart(*, agent_id: str, runtime_id: str, idempotency_key: str) -> dict:
    original_backend = settings.queue_backend
    settings.queue_backend = "delivery_table"
    client = TestClient(build_app())
    try:
        job_id = _provision_and_send(client, agent_id=agent_id, idempotency_key=idempotency_key, scenario="control_plane_restart_active_work")
        claim_data = _register_and_claim(client, runtime_id=runtime_id, agent_id=agent_id, lease_ttl=1)
        first_run_id = claim_data["run"]["run_id"]

        heartbeat = client.post(
            f"/runs/{first_run_id}/heartbeat",
            json={
                "runtime_id": runtime_id,
                "lease_id": claim_data["lease"]["lease_id"],
                "fencing_token": claim_data["lease"]["fencing_token"],
                "extend_seconds": 1,
            },
        )
        if heartbeat.status_code != 200:
            raise ValueError(f"failed to start pre-restart drill run: {heartbeat.text}")
    finally:
        client.close()

    restarted = TestClient(build_app())
    try:
        sweep = _sweep_and_requeue(job_id)

        runtime2_id = f"{runtime_id}_restart"
        second_claim = _register_and_claim(restarted, runtime_id=runtime2_id, agent_id=agent_id, lease_ttl=30)
        second_run_id = second_claim["run"]["run_id"]

        artifacts = _write_drill_artifacts(job_id)
        complete = restarted.post(
            f"/runs/{second_run_id}/complete",
            json={
                "runtime_id": runtime2_id,
                "lease_id": second_claim["lease"]["lease_id"],
                "fencing_token": second_claim["lease"]["fencing_token"],
                "artifacts": artifacts,
                "summary": {"scenario": "control_plane_restart_active_work"},
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
        return {
            "scenario": "control_plane_restart_active_work",
            "job_id": job_id,
            "first_run_id": first_run_id,
            "second_run_id": second_run_id,
            "job_status": job["status"],
            "sweep": sweep,
            "run_count": run_count,
            "first_run_status": first_run.status if first_run is not None else None,
            "second_run_status": second_run.status if second_run is not None else None,
            "complete_status_code": complete.status_code,
            "event_types": _job_events(restarted, job_id),
        }
    finally:
        restarted.close()
        settings.queue_backend = original_backend


def _drill_queue_redelivery(client: TestClient, *, job_id: str, agent_id: str, runtime_id: str) -> dict:
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

    try:
        claim_data = _register_and_claim(client, runtime_id=runtime_id, agent_id=agent_id, lease_ttl=30)
        run_id = claim_data["run"]["run_id"]

        artifacts = _write_drill_artifacts(job_id)
        complete = client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": runtime_id,
                "lease_id": claim_data["lease"]["lease_id"],
                "fencing_token": claim_data["lease"]["fencing_token"],
                "artifacts": artifacts,
                "summary": {"scenario": "queue_redelivery_after_consumer_restart"},
            },
        )
        session = SessionLocal()
        try:
            run_count = int(session.scalar(select(func.count()).select_from(Run).where(Run.job_id == job_id)) or 0)
        finally:
            session.close()
        job = client.get(f"/jobs/{job_id}").json()["data"]
        return {
            "scenario": "queue_redelivery_after_consumer_restart",
            "job_id": job_id,
            "run_id": run_id,
            "job_status": job["status"],
            "redrive": redrive,
            "claim_succeeded": True,
            "run_count": run_count,
            "complete_status_code": complete.status_code,
            "event_types": _job_events(client, job_id),
        }
    finally:
        settings.queue_backend = original_backend


def _drill_repeated_fencing(client: TestClient, *, job_id: str, agent_id: str, runtime_id: str) -> dict:
    # Register runtime (don't claim yet — the loop handles claiming)
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

        sweep = _sweep_and_requeue(job_id)
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
        stale_attempts.append({"status_code": stale.status_code, "body": stale.json()})
        final_job_status = client.get(f"/jobs/{job_id}").json()["data"]["status"]
        if final_job_status != "queued":
            break

    alerts = client.get("/ops/alerts").json()["data"]
    return {
        "scenario": "repeated_fencing_stale_owner",
        "job_id": job_id,
        "run_ids": run_ids,
        "job_status": final_job_status,
        "expired_leases": expired_total,
        "stale_attempts": stale_attempts,
        "alert_codes": [item["code"] for item in alerts["items"]],
        "event_types": _job_events(client, job_id),
    }


def _sweep_only() -> dict:
    """Sweep expired leases without re-enqueueing."""
    session = SessionLocal()
    try:
        return sweep_expired_leases(
            session,
            now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
        )
    finally:
        session.close()


def _drill_lease_expiry(client: TestClient, *, job_id: str, run_id: str) -> dict:
    sweep = _sweep_only()
    job = client.get(f"/jobs/{job_id}").json()["data"]
    return {
        "scenario": "lease_expiry_requeue",
        "job_id": job_id,
        "run_id": run_id,
        "status": job["status"],
        "retry_count": job["retry_count"],
        "sweep": sweep,
        "event_types": _job_events(client, job_id),
    }


def _drill_artifact_write_failure(client: TestClient, *, job_id: str, run_id: str, claim_data: dict, runtime_id: str) -> dict:
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
            "summary": {"scenario": "artifact_store_write_failure"},
        },
    )
    job = client.get(f"/jobs/{job_id}").json()["data"]
    session = SessionLocal()
    try:
        run = session.get(Run, run_id)
        lease = session.get(Lease, claim_data["lease"]["lease_id"])
        artifact_count = int(session.scalar(select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id)) or 0)
    finally:
        session.close()
    return {
        "scenario": "artifact_store_write_failure",
        "job_id": job_id,
        "run_id": run_id,
        "job_status": job["status"],
        "run_status": run.status if run is not None else None,
        "lease_status": lease.status if lease is not None else None,
        "artifact_count": artifact_count,
        "terminal_status_code": terminal.status_code,
        "terminal_body": terminal.json(),
    }


def _drill_duplicate_replay(client: TestClient, *, job_id: str, run_id: str, claim_data: dict, runtime_id: str) -> dict:
    artifacts = _write_drill_artifacts(job_id)
    first = client.post(
        f"/runs/{run_id}/complete",
        json={
            "runtime_id": runtime_id,
            "lease_id": claim_data["lease"]["lease_id"],
            "fencing_token": claim_data["lease"]["fencing_token"],
            "artifacts": artifacts,
            "summary": {"scenario": "duplicate_terminal_replay"},
        },
    )
    second = client.post(
        f"/runs/{run_id}/complete",
        json={
            "runtime_id": runtime_id,
            "lease_id": claim_data["lease"]["lease_id"],
            "fencing_token": claim_data["lease"]["fencing_token"],
            "artifacts": artifacts,
            "summary": {"scenario": "duplicate_terminal_replay", "replay": True},
        },
    )
    job = client.get(f"/jobs/{job_id}").json()["data"]
    return {
        "scenario": "duplicate_terminal_replay",
        "job_id": job_id,
        "run_id": run_id,
        "status": job["status"],
        "result_artifact_id": job.get("result_artifact_id"),
        "first_terminal_status_code": first.status_code,
        "second_terminal_status_code": second.status_code,
        "second_terminal_body": second.json(),
        "event_types": _job_events(client, job_id),
    }


# ── Main dispatcher ─────────────────────────────────────────────────


def run_failure_injection_scenario(*, scenario: str) -> dict:
    """Run a named failure injection drill and return the result payload."""
    if scenario not in _SCENARIOS:
        raise ValueError(f"unsupported failure-injection scenario: {scenario}")

    stamp = utc_now().strftime("%H%M%S%f")
    agent_id = f"agt_fi_{stamp}"
    runtime_id = f"rtm_fi_{stamp}"
    idempotency_key = f"failure-injection-{stamp}"

    if scenario == "control_plane_restart_active_work":
        return _drill_control_plane_restart(agent_id=agent_id, runtime_id=runtime_id, idempotency_key=idempotency_key)

    with TestClient(build_app()) as client:
        job_id = _provision_and_send(client, agent_id=agent_id, idempotency_key=idempotency_key, scenario=scenario)

        if scenario == "queue_redelivery_after_consumer_restart":
            return _drill_queue_redelivery(client, job_id=job_id, agent_id=agent_id, runtime_id=runtime_id)

        if scenario == "repeated_fencing_stale_owner":
            return _drill_repeated_fencing(client, job_id=job_id, agent_id=agent_id, runtime_id=runtime_id)

        claim_data = _register_and_claim(client, runtime_id=runtime_id, agent_id=agent_id, lease_ttl=1)
        run_id = claim_data["run"]["run_id"]

        if scenario == "lease_expiry_requeue":
            return _drill_lease_expiry(client, job_id=job_id, run_id=run_id)

        if scenario == "artifact_store_write_failure":
            return _drill_artifact_write_failure(client, job_id=job_id, run_id=run_id, claim_data=claim_data, runtime_id=runtime_id)

        # duplicate_terminal_replay
        return _drill_duplicate_replay(client, job_id=job_id, run_id=run_id, claim_data=claim_data, runtime_id=runtime_id)
