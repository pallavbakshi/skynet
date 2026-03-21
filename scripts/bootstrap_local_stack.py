# DEPRECATED: Use `skyops db seed` instead.
from __future__ import annotations

import os
import time

import httpx

from agp.client import AgpClient, AgpProfile
from agp.db import SessionLocal, init_db
from agp.models import Capability, CapabilityPool, utc_now
from bootstrap_minio_policy import apply_bucket_policy


def _bootstrap_capability_id() -> str:
    return os.environ.get("AGP_BOOTSTRAP_CAPABILITY_ID", "cap_python")


def _bootstrap_agent_id() -> str:
    return os.environ.get("AGP_BOOTSTRAP_AGENT_ID", "agt_local")


def _bootstrap_runtime_id() -> str | None:
    return os.environ.get("AGP_BOOTSTRAP_RUNTIME_ID") or None


def wait_for_health(timeout_seconds: float = 60.0) -> None:
    profile = AgpProfile.load()
    deadline = time.monotonic() + timeout_seconds
    with AgpClient(profile=profile) as client:
        while time.monotonic() < deadline:
            try:
                client.health()
                return
            except Exception:
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
    profile = AgpProfile.load()
    deadline = time.monotonic() + 60.0
    last_error: str | None = None
    with AgpClient(profile=profile) as client:
        while time.monotonic() < deadline:
            try:
                agents = client.list_agents(capability_id=_bootstrap_capability_id(), limit=200)
                if any(item["agent_id"] == _bootstrap_agent_id() for item in agents["items"]):
                    return
                # Agent doesn't exist yet — create via raw HTTP since /agents/up
                # is not in the SDK (it's a runtime registration endpoint)
                assigned_runtime_id = _bootstrap_runtime_id()
                if assigned_runtime_id:
                    try:
                        client._client.get(f"/runtimes/{assigned_runtime_id}").raise_for_status()
                    except httpx.HTTPStatusError:
                        assigned_runtime_id = None
                response = client._client.post(
                    "/agents/up",
                    json={
                        "agent_id": _bootstrap_agent_id(),
                        "capability_id": _bootstrap_capability_id(),
                        "assigned_runtime_id": assigned_runtime_id,
                    },
                )
                response.raise_for_status()
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1.0)
    raise RuntimeError(last_error or "timed out ensuring bootstrap agent")


def main() -> None:
    init_db()
    wait_for_health()
    apply_bucket_policy()
    ensure_capability()
    ensure_agent()


if __name__ == "__main__":
    main()
