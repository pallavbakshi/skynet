from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx


def _server_url() -> str:
    return os.environ.get("AGP_SERVER_URL", "http://127.0.0.1:7860")


def _agent_id() -> str:
    return os.environ.get("AGP_SMOKE_AGENT_ID", "agt_local")


def _wait_for_health(client: httpx.Client, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get("/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise RuntimeError("control plane did not become healthy in time")


def _poll_job_terminal(client: httpx.Client, job_id: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()["data"]
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(1.0)
    raise RuntimeError(f"job {job_id} did not reach terminal state before timeout")


def main() -> int:
    with httpx.Client(base_url=_server_url(), timeout=10.0) as client:
        _wait_for_health(client)
        response = client.post(
            "/messages/send",
            headers={"Idempotency-Key": f"smoke-{int(time.time())}"},
            json={
                "target": {"type": "agent", "id": _agent_id()},
                "message": {"text": "local deployment smoke test", "metadata": {"kind": "smoke"}},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        if payload.get("kind") == "inline_result":
            artifact_id = payload["result_artifact_id"]
        else:
            job = _poll_job_terminal(client, payload["job_id"])
            if job["status"] != "completed":
                raise RuntimeError(f"smoke job finished in unexpected state: {job['status']}")
            artifact_id = job["result_artifact_id"]

        artifact = client.get(f"/artifacts/{artifact_id}/content")
        artifact.raise_for_status()
        content = artifact.json()["data"].get("content", "")
        if "local deployment smoke test" not in content:
            raise RuntimeError("smoke artifact content did not include expected payload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
