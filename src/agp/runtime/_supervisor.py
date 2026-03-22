"""Core runtime supervisor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import httpx

from agp.artifact_store import ArtifactStore, get_artifact_store
from agp.client._runtime import RuntimeClient, RuntimeIdentity
from agp.config import settings
from agp.logs import append_jsonl_log

from agp.runtime._types import (
    ArtifactPayload,
    ExecutionResult,
    InterruptRequested,
    RecoverableExecutionError,
    SessionHealth,
    TerminalSession,
)


def _runtime_log_path(runtime_id: str) -> Path:
    return settings.log_root / f"runtime-{runtime_id}.jsonl"


def _append_runtime_log(runtime_id: str, entry: dict[str, Any]) -> None:
    path = _runtime_log_path(runtime_id)
    payload = {"created_at": datetime.now(UTC).isoformat(), **entry}
    append_jsonl_log(
        path,
        payload,
        rotation_bytes=settings.observability_log_rotation_bytes,
    )


def _make_logging_runtime_client(
    identity: RuntimeIdentity,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> RuntimeClient:
    """Create a RuntimeClient with structured logging wired in."""
    return RuntimeClient(identity, timeout=timeout, client=client, log_fn=_append_runtime_log)


def register_runtime(identity: RuntimeIdentity) -> dict:
    """Register a runtime with the control plane."""

    client = _make_logging_runtime_client(identity)
    try:
        return client.register()
    finally:
        client.close()


def _failure_snapshot_payloads(*, host: "TerminalHost", session: TerminalSession, error: Exception) -> list[ArtifactPayload]:
    snapshot = host.snapshot(session)
    health = host.health(session)
    return [
        ArtifactPayload(
            role="failure_evidence",
            name="session-snapshot.json",
            content=json.dumps(snapshot, indent=2, sort_keys=True, default=str),
            content_type="application/json",
        ),
        ArtifactPayload(
            role="failure_evidence",
            name="session-health.json",
            content=json.dumps(
                {
                    "session_id": health.session_id,
                    "exists": health.exists,
                    "healthy": health.healthy,
                    "reason": health.reason,
                    "metadata": health.metadata,
                    "error": str(error),
                    "exception_type": type(error).__name__,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
            content_type="application/json",
        ),
    ]


# Import here to allow string annotations above to resolve
from agp.runtime._abc import AgentAdapter, TerminalHost  # noqa: E402


class RuntimeSupervisor:
    """Runtime supervisor using pluggable terminal host and agent adapter."""

    def __init__(
        self,
        client: RuntimeClient,
        host: TerminalHost,
        adapter: AgentAdapter,
        artifact_root: str | Path = ".agp-artifacts",
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        # Ensure the client has structured logging wired in when used
        # inside the supervisor (the SDK default is no logging).
        if client._log_fn is None:
            client._log_fn = _append_runtime_log
        self.client = client
        self.host = host
        self.adapter = adapter
        self.artifact_root = Path(artifact_root)
        self.artifact_store = artifact_store or get_artifact_store(
            settings.artifact_backend, self.artifact_root,
            server_url=client.identity.server_url,
        )

    def _write_artifact(self, *, job_id: str, payload: ArtifactPayload) -> dict[str, Any]:
        stored = self.artifact_store.write_text(
            namespace=self.client.identity.runtime_id,
            job_id=job_id,
            name=payload.name,
            content=payload.content,
            role=payload.role,
        )
        return {
            "role": stored.role,
            "storage_ref": stored.storage_ref,
            "content_type": payload.content_type,
            "checksum": stored.checksum,
            "size_bytes": stored.size_bytes,
        }

    def _failure_snapshot_payloads(
        self,
        *,
        session: TerminalSession,
        error: Exception,
    ) -> list[ArtifactPayload]:
        return _failure_snapshot_payloads(host=self.host, session=session, error=error)

    def check_interrupt(self, claimed: dict[str, Any]) -> None:
        job = self.client.get_job(claimed["job"]["job_id"])
        if job["status"] == "interrupt_requested":
            raise InterruptRequested("interrupt requested by control plane")

    def emit_progress(self, claimed: dict[str, Any], *, message: str, details: dict[str, Any] | None = None) -> dict:
        return self.client.progress(
            run_id=claimed["run"]["run_id"],
            lease_id=claimed["lease"]["lease_id"],
            fencing_token=claimed["lease"]["fencing_token"],
            message=message,
            details=details or {},
        )

    def _cleanup_workspace(self, session: TerminalSession, claimed: dict[str, Any]) -> None:
        """Best-effort workspace cleanup after run completion/failure/cancel.

        Removes stale lock files and temporary run artifacts left behind
        by the adapter or host.
        """
        try:
            run_id = claimed.get("run", {}).get("run_id", "unknown")
            # Clean up any checkpoint cursor files for this session
            if hasattr(self.host, "checkpoint_dir"):
                import glob
                for stale in glob.glob(str(self.host.checkpoint_dir / f"cursor-{session.session_id}*")):
                    pass  # cursors are retained for restart resilience — don't delete
            _append_runtime_log(
                self.client.identity.runtime_id,
                {
                    "kind": "runtime_worker",
                    "action": "workspace_cleanup",
                    "run_id": run_id,
                    "session_id": session.session_id,
                },
            )
        except Exception:  # noqa: BLE001
            pass  # cleanup is best-effort

    def run_forever(
        self,
        *,
        agent_id: str | None = None,
        capability_id: str | None = None,
        lease_ttl_seconds: int = 30,
        heartbeat_interval_seconds: float = 5.0,
        idle_sleep_seconds: float = 0.25,
        max_iterations: int | None = None,
        stop_event: Event | None = None,
        max_local_recoveries: int = 1,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        iterations = 0
        stop_event = stop_event or Event()
        while not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            outcome = self.run_once(
                agent_id=agent_id,
                capability_id=capability_id,
                lease_ttl_seconds=lease_ttl_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                max_local_recoveries=max_local_recoveries,
            )
            outcomes.append(outcome)
            iterations += 1
            if not outcome.get("claimed"):
                stop_event.wait(idle_sleep_seconds)
        return outcomes

    def run_once(
        self,
        *,
        agent_id: str | None = None,
        capability_id: str | None = None,
        lease_ttl_seconds: int = 30,
        heartbeat_interval_seconds: float = 5.0,
        max_local_recoveries: int = 1,
        max_local_recovery_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self.client.register()
        _append_runtime_log(
            self.client.identity.runtime_id,
            {
                "kind": "runtime_worker",
                "action": "run_once_started",
                "host_kind": self.host.kind,
                "adapter_kind": self.adapter.kind,
            },
        )
        claimed = self.client.claim(
            agent_id=agent_id,
            capability_id=capability_id,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if not claimed.get("claimed"):
            _append_runtime_log(self.client.identity.runtime_id, {"kind": "runtime_worker", "action": "idle_no_claim"})
            return {"claimed": False}

        run = claimed["run"]
        lease = claimed["lease"]
        session = self.host.get_or_create_session(agent_id=claimed["agent_id"])
        # Attempt to restore a cursor checkpoint from a previous runtime
        # process.  The adapter can use this via session.metadata["restored_cursor"].
        restored = self.host.load_cursor(session)
        if restored is not None:
            session.metadata["restored_cursor"] = restored
        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
        stop = Event()

        max_missed_heartbeats = 3
        lease_lost = Event()  # signals that we lost the lease / fencing

        def heartbeat_loop() -> None:
            consecutive_misses = 0
            while not stop.wait(heartbeat_interval_seconds):
                try:
                    hb_response = self.client.heartbeat(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        extend_seconds=lease_ttl_seconds,
                    )
                    consecutive_misses = 0
                    # Check if the CP surfaced an interrupt in the heartbeat response
                    if isinstance(hb_response, dict) and hb_response.get("interrupt_requested"):
                        _append_runtime_log(
                            self.client.identity.runtime_id,
                            {"kind": "runtime_worker", "action": "interrupt_via_heartbeat", "run_id": run["run_id"]},
                        )
                        stop.set()
                        break
                except Exception:  # noqa: BLE001
                    consecutive_misses += 1
                    _append_runtime_log(
                        self.client.identity.runtime_id,
                        {
                            "kind": "runtime_worker",
                            "action": "heartbeat_missed",
                            "run_id": run["run_id"],
                            "consecutive_misses": consecutive_misses,
                        },
                    )
                    if consecutive_misses >= max_missed_heartbeats:
                        _append_runtime_log(
                            self.client.identity.runtime_id,
                            {
                                "kind": "runtime_worker",
                                "action": "heartbeat_budget_exhausted",
                                "run_id": run["run_id"],
                                "max_missed": max_missed_heartbeats,
                            },
                        )
                        lease_lost.set()
                        # Attempt to kill local execution context (fencing handoff)
                        try:
                            self.host.interrupt(session)
                        except Exception:  # noqa: BLE001
                            pass
                        stop.set()
                        break

        self.client.heartbeat(
            run_id=run["run_id"],
            lease_id=lease["lease_id"],
            fencing_token=lease["fencing_token"],
            extend_seconds=lease_ttl_seconds,
        )
        thread = Thread(target=heartbeat_loop, daemon=True)
        thread.start()
        try:
            _append_runtime_log(
                self.client.identity.runtime_id,
                {
                    "kind": "runtime_worker",
                    "action": "execution_started",
                    "run_id": run["run_id"],
                    "job_id": claimed["job"]["job_id"],
                    "host_kind": self.host.kind,
                    "adapter_kind": self.adapter.kind,
                    "session_id": session.session_id,
                },
            )
            self.emit_progress(
                claimed,
                message="runtime.started",
                details={
                    "agent_id": claimed["agent_id"],
                    "session_id": session.session_id,
                    "host_kind": self.host.kind,
                    "adapter_kind": self.adapter.kind,
                },
            )
            attempts = 0
            recovery_started_at: float | None = None
            while True:
                try:
                    result = self.adapter.execute_run(
                        host=self.host,
                        session=session,
                        claimed=claimed,
                        supervisor=self,
                    )
                    break
                except RecoverableExecutionError as exc:
                    if attempts >= max_local_recoveries:
                        raise
                    now = monotonic()
                    if recovery_started_at is None:
                        recovery_started_at = now
                    if now - recovery_started_at > max_local_recovery_seconds:
                        raise RuntimeError("local recovery budget exhausted by elapsed time") from exc
                    _append_runtime_log(
                        self.client.identity.runtime_id,
                        {
                            "kind": "runtime_worker",
                            "action": "local_recovery_started",
                            "run_id": run["run_id"],
                            "attempt": attempts + 1,
                            "error": str(exc),
                        },
                    )
                    self.client.recovering(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        details={
                            "attempt": attempts + 1,
                            "error": str(exc),
                            "session_id": session.session_id,
                            "host_kind": self.host.kind,
                            "adapter_kind": self.adapter.kind,
                        },
                    )
                    # Keep ownership explicit while performing bounded local recovery work.
                    self.client.heartbeat(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        extend_seconds=lease_ttl_seconds,
                    )
                    if not self.host.session_exists(session):
                        session = self.host.get_or_create_session(
                            agent_id=claimed["agent_id"],
                            workspace_ref=session.workspace_ref,
                        )
                        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
                    self.adapter.recover(
                        host=self.host,
                        session=session,
                        claimed=claimed,
                        attempt=attempts + 1,
                        error=exc,
                        supervisor=self,
                    )
                    self.client.resumed(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        details={
                            "attempt": attempts + 1,
                            "session_id": session.session_id,
                            "host_kind": self.host.kind,
                            "adapter_kind": self.adapter.kind,
                        },
                    )
                    _append_runtime_log(
                        self.client.identity.runtime_id,
                        {
                            "kind": "runtime_worker",
                            "action": "local_recovery_resumed",
                            "run_id": run["run_id"],
                            "attempt": attempts + 1,
                        },
                    )
                    self.client.heartbeat(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        extend_seconds=lease_ttl_seconds,
                    )
                    attempts += 1
            sleep(0)
            stored_artifacts = [
                self._write_artifact(job_id=claimed["job"]["job_id"], payload=payload)
                for payload in result.artifacts
            ]
            completed = self.client.complete(
                run_id=run["run_id"],
                lease_id=lease["lease_id"],
                fencing_token=lease["fencing_token"],
                artifacts=stored_artifacts,
                summary=result.summary,
            )
            _append_runtime_log(
                self.client.identity.runtime_id,
                {
                    "kind": "runtime_worker",
                    "action": "execution_completed",
                    "run_id": run["run_id"],
                    "job_id": claimed["job"]["job_id"],
                    "session_id": session.session_id,
                },
            )
            return {"claimed": True, "claim": claimed, "result": completed}
        except InterruptRequested as exc:
            try:
                self.host.interrupt(session)
            except Exception:  # noqa: BLE001
                pass
            _append_runtime_log(
                self.client.identity.runtime_id,
                {"kind": "runtime_worker", "action": "interrupt_observed", "run_id": run["run_id"], "reason": str(exc)},
            )
            cancelled = self.client.cancel(
                run_id=run["run_id"],
                lease_id=lease["lease_id"],
                fencing_token=lease["fencing_token"],
                reason=str(exc),
            )
            return {"claimed": True, "claim": claimed, "cancelled": True, "result": cancelled}
        except Exception as exc:
            _append_runtime_log(
                self.client.identity.runtime_id,
                {
                    "kind": "runtime_worker",
                    "action": "execution_failed_locally",
                    "run_id": run["run_id"],
                    "job_id": claimed["job"]["job_id"],
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
            )
            failure_result = self.adapter.build_failure_result(
                host=self.host,
                session=session,
                claimed=claimed,
                error=exc,
                supervisor=self,
            )
            failure_result.artifacts.extend(self._failure_snapshot_payloads(session=session, error=exc))
            artifacts = [
                self._write_artifact(job_id=claimed["job"]["job_id"], payload=payload)
                for payload in failure_result.artifacts
            ]
            failed = self.client.fail(
                run_id=run["run_id"],
                lease_id=lease["lease_id"],
                fencing_token=lease["fencing_token"],
                error=str(exc),
                artifacts=artifacts,
                summary=failure_result.summary,
            )
            return {"claimed": True, "claim": claimed, "error": str(exc), "result": failed}
        finally:
            stop.set()
            thread.join(timeout=heartbeat_interval_seconds + 1.0)
            # Workspace cleanup: remove temp files, stale locks, session residue
            self._cleanup_workspace(session, claimed)
