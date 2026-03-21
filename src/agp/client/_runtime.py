"""Runtime-side HTTP client for the AGP control plane (SDK version).

Decoupled from agp.config and agp.db — usable without server-side imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


@dataclass(slots=True)
class RuntimeIdentity:
    runtime_id: str
    hostname: str
    server_url: str = "http://127.0.0.1:7860"
    release_version: str = "0.1.0"
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeClient:
    """Thin runtime client over the control-plane HTTP API.

    Parameters
    ----------
    identity : RuntimeIdentity
        Runtime connection and identification info.
    timeout : float
        HTTP request timeout in seconds.
    client : httpx.Client | None
        Optional pre-built HTTP client (for test injection).
    log_fn : callable | None
        Optional ``(runtime_id, entry_dict) -> None`` callback for
        structured logging.  When *None* (the default), logging is
        silently skipped.
    """

    def __init__(
        self,
        identity: RuntimeIdentity,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
        log_fn: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.identity = identity
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=identity.server_url.rstrip("/"), timeout=timeout
        )
        self._log_fn = log_fn

    def _log(self, entry: dict[str, Any]) -> None:
        if self._log_fn is not None:
            self._log_fn(self.identity.runtime_id, entry)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def register(self) -> dict:
        response = self._client.post(
            "/runtimes/register",
            json={
                "runtime_id": self.identity.runtime_id,
                "hostname": self.identity.hostname,
                "release_version": self.identity.release_version,
                "metadata": self.identity.metadata,
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {"kind": "runtime_client", "action": "register", "hostname": self.identity.hostname}
        )
        return payload

    def claim(
        self,
        *,
        agent_id: str | None = None,
        capability_id: str | None = None,
        lease_ttl_seconds: int = 30,
    ) -> dict:
        response = self._client.post(
            "/runs/claim",
            json={
                "runtime_id": self.identity.runtime_id,
                "agent_id": agent_id,
                "capability_id": capability_id,
                "lease_ttl_seconds": lease_ttl_seconds,
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "claim",
                "agent_id": agent_id,
                "capability_id": capability_id,
                "claimed": payload.get("claimed", False),
                "job_id": payload.get("job", {}).get("job_id"),
                "run_id": payload.get("run", {}).get("run_id"),
            }
        )
        return payload

    def heartbeat(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        extend_seconds: int = 30,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/heartbeat",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "extend_seconds": extend_seconds,
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {"kind": "runtime_client", "action": "heartbeat", "run_id": run_id, "lease_id": lease_id}
        )
        return payload

    def progress(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/progress",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "message": message,
                "details": details or {},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "progress",
                "run_id": run_id,
                "lease_id": lease_id,
                "message": message,
            }
        )
        return payload

    def recovering(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        details: dict[str, Any] | None = None,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/recovering",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "details": details or {},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {"kind": "runtime_client", "action": "recovering", "run_id": run_id, "lease_id": lease_id}
        )
        return payload

    def resumed(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        details: dict[str, Any] | None = None,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/resumed",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "details": details or {},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {"kind": "runtime_client", "action": "resumed", "run_id": run_id, "lease_id": lease_id}
        )
        return payload

    def get_job(self, job_id: str) -> dict:
        response = self._client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "get_job",
                "job_id": job_id,
                "job_status": payload.get("status"),
            }
        )
        return payload

    def cancel(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        reason: str = "interrupt_requested",
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/cancel",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "reason": reason,
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "cancel",
                "run_id": run_id,
                "lease_id": lease_id,
                "reason": reason,
            }
        )
        return payload

    def complete(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        artifacts: list[dict[str, Any]],
        summary: dict[str, Any] | None = None,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "artifacts": artifacts,
                "summary": summary or {},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "complete",
                "run_id": run_id,
                "lease_id": lease_id,
                "artifact_roles": [artifact["role"] for artifact in artifacts],
            }
        )
        return payload

    def fail(
        self,
        *,
        run_id: str,
        lease_id: str,
        fencing_token: int,
        error: str,
        artifacts: list[dict[str, Any]] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict:
        response = self._client.post(
            f"/runs/{run_id}/fail",
            json={
                "runtime_id": self.identity.runtime_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "error": error,
                "artifacts": artifacts or [],
                "summary": summary or {},
            },
        )
        response.raise_for_status()
        payload = response.json()["data"]
        self._log(
            {
                "kind": "runtime_client",
                "action": "fail",
                "run_id": run_id,
                "lease_id": lease_id,
                "error": error,
                "artifact_roles": [artifact["role"] for artifact in artifacts or []],
            }
        )
        return payload
