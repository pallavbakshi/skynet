"""Operator-side AGP client SDK."""

from __future__ import annotations

from time import sleep
from typing import Any

import httpx

from agp.client._profile import AgpProfile

_UNSET = object()


class AgpClient:
    """SDK client for the AGP control plane.

    Wraps all control-plane API operations.  Loads connection context
    from an :class:`AgpProfile` or accepts an injected ``httpx.Client``
    (useful for tests with FastAPI ``TestClient``).
    """

    def __init__(
        self,
        profile: AgpProfile | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
            self._headers: dict[str, str] = {}
        else:
            resolved = profile or AgpProfile.load()
            self._headers = (
                {"Authorization": f"Bearer {resolved.token}"} if resolved.token else {}
            )
            self._client = httpx.Client(
                base_url=resolved.server_url.rstrip("/"),
                headers=self._headers,
                timeout=10.0,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AgpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Health ────────────────────────────────────────────────────

    def health(self) -> dict:
        response = self._client.get("/health")
        response.raise_for_status()
        return response.json()

    # ── Capability seeding ──────────────────────────────────────────

    def seed_capability(
        self,
        capability_id: str,
        name: str,
        *,
        version: str = "v1",
        image_ref: str = "",
        model_ref: str = "",
        resource_tier: str = "small",
        permission_profile: str = "default",
        queue_mode: str = "agent",
        runtime_requirements: dict | None = None,
    ) -> dict:
        """Seed (create or update) a capability via the admin API."""
        response = self._client.post(
            "/capabilities/seed",
            json={
                "capability_id": capability_id,
                "name": name,
                "version": version,
                "image_ref": image_ref,
                "model_ref": model_ref,
                "resource_tier": resource_tier,
                "permission_profile": permission_profile,
                "queue_mode": queue_mode,
                "runtime_requirements": runtime_requirements or {},
            },
        )
        response.raise_for_status()
        return response.json()["data"]

    # ── Work dispatch ─────────────────────────────────────────────

    def send(
        self,
        target_type: str,
        target_id: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        detach_mode: str = "auto",
        idempotency_key: str | None = None,
    ) -> dict:
        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.post(
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

    def interrupt(self, job_id: str) -> dict:
        response = self._client.post(f"/jobs/{job_id}/interrupt")
        response.raise_for_status()
        return response.json()["data"]

    # ── Inspection ────────────────────────────────────────────────

    def get_job(self, job_id: str) -> dict:
        response = self._client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        return response.json()["data"]

    def list_jobs(
        self,
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
        response = self._client.get("/jobs", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def get_agent(self, agent_id: str) -> dict:
        response = self._client.get(f"/agents/{agent_id}")
        response.raise_for_status()
        return response.json()["data"]

    def get_capability(self, capability_id: str) -> dict:
        response = self._client.get(f"/capabilities/{capability_id}")
        response.raise_for_status()
        return response.json()["data"]

    def register_agent(
        self,
        agent_id: str | None,
        capability_id: str,
        *,
        assigned_runtime_id: str | None = None,
        workspace_ref: str | None = None,
    ) -> dict:
        """Register (bring up) an agent with the control plane."""
        payload: dict[str, Any] = {
            "capability_id": capability_id,
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if assigned_runtime_id is not None:
            payload["assigned_runtime_id"] = assigned_runtime_id
        if workspace_ref is not None:
            payload["workspace_ref"] = workspace_ref
        response = self._client.post("/agents/up", json=payload)
        response.raise_for_status()
        return response.json()["data"]

    def patch_agent(
        self,
        agent_id: str,
        *,
        workspace_ref: str | None | object = _UNSET,
    ) -> dict:
        """Update mutable fields on an existing agent."""
        payload: dict[str, Any] = {}
        if workspace_ref is not _UNSET:
            payload["workspace_ref"] = workspace_ref
        response = self._client.patch(f"/agents/{agent_id}", json=payload)
        response.raise_for_status()
        return response.json()["data"]

    def list_agents(
        self,
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
        response = self._client.get("/agents", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def list_capabilities(
        self,
        *,
        name: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit}
        if name is not None:
            params["name"] = name
        if cursor is not None:
            params["cursor"] = cursor
        response = self._client.get("/capabilities", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def resolve_capability_by_name(self, name: str) -> dict | None:
        """Find a capability by display name.

        Returns None if not found and raises ValueError if the name is ambiguous.
        """
        data = self.list_capabilities(name=name)
        items = data.get("items", [])
        matches = [item for item in items if item.get("name") == name]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(
                "capability name is ambiguous: "
                + ", ".join(
                    f"{item.get('capability_id')} ({item.get('name')}:{item.get('version')})"
                    for item in matches
                )
            )
        return matches[0]

    def list_deliveries(
        self,
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
        response = self._client.get("/queue/deliveries", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def get_job_events(
        self,
        job_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = self._client.get(f"/jobs/{job_id}/events", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def watch_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 0.25,
        limit: int = 100,
        max_polls: int | None = None,
    ) -> list[dict]:
        """Poll job state and ordered events until the job reaches terminal state."""
        terminal_statuses = {"completed", "failed", "cancelled"}
        event_cursor: str | None = None
        polls = 0
        snapshots: list[dict] = []

        while True:
            job_response = self._client.get(f"/jobs/{job_id}")
            job_response.raise_for_status()
            job_payload = job_response.json()["data"]

            params: dict[str, object] = {"limit": limit}
            if event_cursor is not None:
                params["cursor"] = event_cursor
            events_response = self._client.get(f"/jobs/{job_id}/events", params=params)
            events_response.raise_for_status()
            events_payload = events_response.json()["data"]
            event_cursor = events_payload["page"]["next_cursor"]

            snapshot = {"job": job_payload, "events": events_payload["items"]}
            snapshots.append(snapshot)

            if job_payload["status"] in terminal_statuses:
                return snapshots

            polls += 1
            if max_polls is not None and polls >= max_polls:
                return snapshots
            sleep(poll_interval)

    # ── Artifacts ─────────────────────────────────────────────────

    def fetch_artifact(self, artifact_id: str, *, content: bool = False) -> dict:
        path = f"/artifacts/{artifact_id}/content" if content else f"/artifacts/{artifact_id}"
        response = self._client.get(path)
        response.raise_for_status()
        return response.json()["data"]

    def list_job_artifacts(self, job_id: str, *, role: str | None = None) -> dict:
        params: dict[str, object] = {}
        if role is not None:
            params["role"] = role
        response = self._client.get(f"/jobs/{job_id}/artifacts", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def list_run_artifacts(self, run_id: str, *, role: str | None = None) -> dict:
        params: dict[str, object] = {}
        if role is not None:
            params["role"] = role
        response = self._client.get(f"/runs/{run_id}/artifacts", params=params)
        response.raise_for_status()
        return response.json()["data"]

    # ── Observability ─────────────────────────────────────────────

    def observability_summary(self) -> dict:
        response = self._client.get("/observability/summary")
        response.raise_for_status()
        return response.json()["data"]

    def observability_alerts(self) -> dict:
        response = self._client.get("/observability/alerts")
        response.raise_for_status()
        return response.json()["data"]

    def observability_metrics(self) -> str:
        response = self._client.get("/observability/metrics")
        response.raise_for_status()
        return response.text

    def observability_dispatch_alerts(self) -> dict:
        response = self._client.post("/observability/alerts/dispatch")
        response.raise_for_status()
        return response.json()["data"]

    def job_trace(self, job_id: str) -> dict:
        response = self._client.get(f"/observability/jobs/{job_id}/trace")
        response.raise_for_status()
        return response.json()["data"]

    def get_runtime(self, runtime_id: str) -> dict | None:
        """Get a runtime by ID. Returns None if not found."""
        response = self._client.get(f"/runtimes/{runtime_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["data"]

    def list_runtimes(
        self,
        *,
        status: str | None = None,
        health_status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, object] = {"limit": limit}
        if status is not None:
            params["status"] = status
        if health_status is not None:
            params["health_status"] = health_status
        if cursor is not None:
            params["cursor"] = cursor
        response = self._client.get("/runtimes", params=params)
        response.raise_for_status()
        return response.json()["data"]

    def observability_audit(self, *, limit: int = 100) -> dict:
        """Fetch the audit trail (security and lifecycle events)."""
        response = self._client.get("/observability/audit", params={"limit": limit})
        response.raise_for_status()
        return response.json()["data"]

    def logs_control_plane(self, *, limit: int = 100) -> dict:
        response = self._client.get("/observability/logs/control-plane", params={"limit": limit})
        response.raise_for_status()
        return response.json()["data"]

    def logs_runtime(self, runtime_id: str, *, limit: int = 100) -> dict:
        response = self._client.get(
            f"/observability/logs/runtimes/{runtime_id}", params={"limit": limit}
        )
        response.raise_for_status()
        return response.json()["data"]

    # ── Nudges ───────────────────────────────────────────────────

    def create_nudge(
        self,
        target_agent_id: str,
        payload: str,
        *,
        priority: int = 1,
        source: str = "human",
        job_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "target_agent_id": target_agent_id,
            "priority": priority,
            "source": source,
            "payload": payload,
        }
        if job_id is not None:
            body["job_id"] = job_id
        response = self._client.post("/nudges", json=body)
        response.raise_for_status()
        return response.json()["data"]

    def next_nudge(self, target_agent_id: str) -> dict | None:
        """Pop the next pending nudge.  Returns None if queue is empty."""
        response = self._client.get("/nudges/next", params={"target_agent_id": target_agent_id})
        response.raise_for_status()
        return response.json()["data"]

    def list_nudges(
        self,
        *,
        target_agent_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict:
        params: dict[str, object] = {"limit": limit}
        if target_agent_id is not None:
            params["target_agent_id"] = target_agent_id
        if status is not None:
            params["status"] = status
        response = self._client.get("/nudges", params=params)
        response.raise_for_status()
        return response.json()["data"]

    # ── Handoff ───────────────────────────────────────────────────

    def handoff(
        self,
        job_id: str,
        targets: list[dict[str, str]],
        message: dict[str, Any],
        *,
        artifact_ids: list[str] | None = None,
    ) -> dict:
        """Create a handoff from a source job to one or more child jobs.

        Parameters
        ----------
        job_id : str
            Source job ID.
        targets : list[dict]
            List of ``{"type": "agent"|"capability", "id": "..."}`` dicts.
        message : dict
            ``{"text": "...", "metadata": {...}}`` payload for child jobs.
        artifact_ids : list[str] | None
            Artifact IDs from the source job to attach to the handoff.
        """
        response = self._client.post(
            f"/jobs/{job_id}/handoff",
            json={
                "targets": targets,
                "message": message,
                "artifact_ids": artifact_ids or [],
            },
        )
        response.raise_for_status()
        return response.json()["data"]

    # ── Agent lifecycle ──────────────────────────────────────────

    def agent_down(
        self,
        agent_id: str,
        *,
        mode: str = "drain",
    ) -> dict:
        """Take an agent down (drain, terminate, or force)."""
        response = self._client.post(
            f"/agents/{agent_id}/down",
            json={"mode": mode},
        )
        response.raise_for_status()
        return response.json()["data"]

    def agent_interrupt(
        self,
        agent_id: str,
        *,
        purge: bool = False,
    ) -> dict:
        """Interrupt active execution on an agent, optionally purging its queue."""
        response = self._client.post(
            f"/agents/{agent_id}/interrupt",
            json={"purge": purge},
        )
        response.raise_for_status()
        return response.json()["data"]

    def agent_undrain(self, agent_id: str) -> dict:
        """Lift draining status and return the agent to IDLE."""
        response = self._client.post(f"/agents/{agent_id}/undrain")
        response.raise_for_status()
        return response.json()["data"]

    # ── Security ──────────────────────────────────────────────────

    def auth_status(self) -> dict:
        response = self._client.get("/system/auth-status")
        response.raise_for_status()
        return response.json()["data"]

    def rotate_operator_tokens(
        self,
        *,
        operator_bearer_token: str | None,
        operator_token_roles_json: dict[str, str],
    ) -> dict:
        response = self._client.post(
            "/system/tokens/operator",
            json={
                "operator_bearer_token": operator_bearer_token,
                "operator_token_roles_json": operator_token_roles_json,
            },
        )
        response.raise_for_status()
        return response.json()["data"]

    def rotate_runtime_tokens(
        self,
        *,
        runtime_bearer_token: str | None,
        runtime_active_tokens_json: list[str],
    ) -> dict:
        response = self._client.post(
            "/system/tokens/runtime",
            json={
                "runtime_bearer_token": runtime_bearer_token,
                "runtime_active_tokens_json": runtime_active_tokens_json,
            },
        )
        response.raise_for_status()
        return response.json()["data"]
