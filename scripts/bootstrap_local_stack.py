from __future__ import annotations

import os
import time

import httpx

from agp.db import SessionLocal, init_db
from agp.models import Capability, CapabilityPool, utc_now


def _server_url() -> str:
    return os.environ.get("AGP_SERVER_URL", "http://127.0.0.1:7860")


def _bootstrap_capability_id() -> str:
    return os.environ.get("AGP_BOOTSTRAP_CAPABILITY_ID", "cap_python")


def _bootstrap_agent_id() -> str:
    return os.environ.get("AGP_BOOTSTRAP_AGENT_ID", "agt_local")


def _bootstrap_runtime_id() -> str | None:
    return os.environ.get("AGP_BOOTSTRAP_RUNTIME_ID") or None


def wait_for_health(timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(base_url=_server_url(), timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
    raise RuntimeError("control plane did not become healthy before bootstrap timeout")


def ensure_capability() -> None:
    session = SessionLocal()
    try:
        capability_id = _bootstrap_capability_id()
        if session.get(Capability, capability_id) is None:
            now = utc_now()
            session.add(
                Capability(
                    capability_id=capability_id,
                    name="python",
                    version="v1",
                    image_ref="local/agp:dev",
                    model_ref="local",
                    resource_tier="small",
                    permission_profile="default",
                    queue_mode="agent",
                    runtime_requirements_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            if session.get(CapabilityPool, capability_id) is None:
                session.add(
                    CapabilityPool(
                        capability_id=capability_id,
                        queue_id=f"capability:{capability_id}:v1",
                        routing_policy="least_recent",
                    )
                )
            session.commit()
    finally:
        session.close()


def ensure_agent() -> None:
    with httpx.Client(base_url=_server_url(), timeout=5.0) as client:
        agents = client.get("/agents", params={"capability_id": _bootstrap_capability_id(), "limit": 200})
        agents.raise_for_status()
        items = agents.json()["data"]["items"]
        if any(item["agent_id"] == _bootstrap_agent_id() for item in items):
            return
        response = client.post(
            "/agents/up",
            json={
                "agent_id": _bootstrap_agent_id(),
                "capability_id": _bootstrap_capability_id(),
                "assigned_runtime_id": _bootstrap_runtime_id(),
            },
        )
        response.raise_for_status()


def main() -> None:
    init_db()
    wait_for_health()
    ensure_capability()
    ensure_agent()


if __name__ == "__main__":
    main()
