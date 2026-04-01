"""Core runtime supervisor."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse

import logging

import httpx

from agp.artifact_store import ArtifactStore, get_artifact_store
from agp.client._runtime import RuntimeClient, RuntimeIdentity
from agp.config import settings
from agp.logs import append_jsonl_log
from agp.runtime._attachments import staged_attachment_relative_path

_logger = logging.getLogger(__name__)

from agp.runtime._types import (
    AuthFailure,
    ArtifactPayload,
    BootstrapFailure,
    ExecutionResult,
    ExecutionTimeout,
    InterruptRequested,
    PaneDied,
    RecoverableExecutionError,
    SessionHealth,
    StableButIndeterminate,
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
        self._registered = False
        self._session_lock = Lock()

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
        tui_died = getattr(self, "_active_tui_died", None)
        if tui_died is not None and tui_died.is_set():
            reason = getattr(self, "_active_tui_died_reason", "tui exited during execution")
            raise PaneDied(reason)
        # Primary path: check the local interrupt event set by the heartbeat
        # thread.  No HTTP call — instant.
        interrupt_event = getattr(self, "_interrupt_event", None)
        if interrupt_event is not None and interrupt_event.is_set():
            raise InterruptRequested("interrupt requested via heartbeat")
        # Fallback: periodically poll the CP directly for interrupt status.
        # This catches interrupts that arrive between heartbeat cycles or
        # when the heartbeat thread is delayed.  Rate-limited to at most
        # once every 5 calls (~10s at 2s poll intervals).
        counter = getattr(self, "_check_interrupt_counter", 0) + 1
        self._check_interrupt_counter = counter
        if counter % 5 == 0:
            try:
                job = self.client.get_job(claimed["job"]["job_id"])
                if job["status"] == "interrupt_requested":
                    raise InterruptRequested("interrupt requested by control plane")
            except InterruptRequested:
                raise
            except Exception:  # noqa: BLE001
                pass  # transient CP error — rely on heartbeat thread

    def _workspace_dir(self, session: TerminalSession) -> Path | None:
        raw = session.workspace_ref
        if not raw:
            return None
        if "://" in raw:
            parsed = urlparse(raw)
            if parsed.scheme == "file":
                raw = unquote(parsed.path)
        path = Path(raw)
        return path if path.is_dir() else None

    def _stage_job_attachments(self, *, session: TerminalSession, claimed: dict[str, Any]) -> None:
        workspace = self._workspace_dir(session)
        if workspace is None:
            return
        staged_roots: list[str] = list(session.metadata.get("staged_attachment_roots", []))
        for item in claimed.get("job_attachments", []) or []:
            name = str(item.get("name") or "").strip()
            storage_ref = str(item.get("storage_ref") or "").strip()
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not name or not storage_ref:
                continue
            content = self.artifact_store.read_text(storage_ref=storage_ref)
            if content is None and artifact_id:
                artifact = self.client.fetch_artifact_content(artifact_id)
                content = artifact.get("content")
            if content is None:
                continue
            relative = staged_attachment_relative_path(
                artifact_id=artifact_id or "unscoped",
                name=name,
            )
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            staged_root = str((workspace / relative.parts[0] / relative.parts[1]).resolve())
            if staged_root not in staged_roots:
                staged_roots.append(staged_root)
        if staged_roots:
            session.metadata["staged_attachment_roots"] = staged_roots

    def emit_progress(self, claimed: dict[str, Any], *, message: str, details: dict[str, Any] | None = None) -> dict:
        try:
            return self.client.progress(
                run_id=claimed["run"]["run_id"],
                lease_id=claimed["lease"]["lease_id"],
                fencing_token=claimed["lease"]["fencing_token"],
                message=message,
                details=details or {},
            )
        except httpx.HTTPStatusError as exc:
            # 409 = lease genuinely lost — must propagate so the adapter stops.
            if exc.response.status_code == 409:
                raise
            # Auth/client errors are likely config issues — log louder.
            if exc.response.status_code in (401, 403, 422):
                _logger.warning("progress emission failed (HTTP %s): %s", exc.response.status_code, exc)
            else:
                _logger.debug("progress emission failed (non-fatal, HTTP %s): %s", exc.response.status_code, exc)
            return {}
        except httpx.TransportError:
            _logger.debug("progress emission failed (transport error)", exc_info=True)
            return {}

    def _cleanup_workspace(self, session: TerminalSession, claimed: dict[str, Any]) -> None:
        """Best-effort post-run workspace cleanup.

        Checkpoint cursor files are intentionally retained for restart
        resilience. Temporary job attachments staged into the workspace are
        deleted after execution to avoid dirtying the operator's worktree.
        """
        for raw_path in session.metadata.pop("staged_attachment_roots", []):
            try:
                path = Path(raw_path)
                if path.exists() and path.is_dir():
                    shutil.rmtree(path)
            except Exception:  # noqa: BLE001
                pass
        try:
            run_id = claimed.get("run", {}).get("run_id", "unknown")
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
            pass

    def run_forever(
        self,
        *,
        agent_id: str | None = None,
        capability_id: str | None = None,
        capabilities: list[str] | None = None,
        lease_ttl_seconds: int = 120,
        heartbeat_interval_seconds: float = 10.0,
        agent_heartbeat_seconds: float = 15.0,
        idle_sleep_seconds: float = 0.25,
        max_iterations: int | None = None,
        stop_event: Event | None = None,
        max_local_recoveries: int = 1,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        iterations = 0
        restart_attempts = 0
        stop_event = stop_event or Event()

        # Register runtime
        if not self._registered:
            self.client.register()
            self._registered = True

        # Register agent (self-registration / initial heartbeat)
        resolved_caps = capabilities or ([capability_id] if capability_id else [])
        if agent_id is not None:
            self.client.agent_up(
                agent_id=agent_id,
                capabilities=resolved_caps,
                metadata=self.client.identity.metadata,
            )

        last_agent_heartbeat = monotonic()

        try:
            while not stop_event.is_set():
                if max_iterations is not None and iterations >= max_iterations:
                    break

                # Agent heartbeat: refresh presence via /agents/up
                if agent_id is not None:
                    elapsed = monotonic() - last_agent_heartbeat
                    if elapsed >= agent_heartbeat_seconds:
                        try:
                            self.client.agent_up(
                                agent_id=agent_id,
                                capabilities=resolved_caps,
                                metadata=self.client.identity.metadata,
                            )
                        except Exception:  # noqa: BLE001
                            _logger.warning("agent heartbeat failed (CP may be temporarily unreachable)", exc_info=True)
                        last_agent_heartbeat = monotonic()

                try:
                    outcome = self.run_once(
                        agent_id=agent_id,
                        capability_id=capability_id,
                        lease_ttl_seconds=lease_ttl_seconds,
                        heartbeat_interval_seconds=heartbeat_interval_seconds,
                        max_local_recoveries=max_local_recoveries,
                    )
                except Exception as exc:  # noqa: BLE001
                    restart_attempts += 1
                    backoff_seconds = min(30.0, max(idle_sleep_seconds, 0.25) * (2 ** (restart_attempts - 1)))
                    _append_runtime_log(
                        self.client.identity.runtime_id,
                        {
                            "kind": "runtime_worker",
                            "action": "run_forever_restart_scheduled",
                            "error": str(exc),
                            "exception_type": type(exc).__name__,
                            "attempt": restart_attempts,
                            "backoff_seconds": backoff_seconds,
                        },
                    )
                    _logger.exception(
                        "runtime worker run_once failed; restarting after backoff",
                        extra={
                            "runtime_id": self.client.identity.runtime_id,
                            "attempt": restart_attempts,
                            "backoff_seconds": backoff_seconds,
                        },
                    )
                    if stop_event.wait(backoff_seconds):
                        break
                    continue
                restart_attempts = 0
                outcomes.append(outcome)
                iterations += 1
                if outcome.get("claimed"):
                    # Reset heartbeat timer after job completion
                    last_agent_heartbeat = monotonic()
                    if agent_id is not None:
                        try:
                            self.client.agent_up(
                                agent_id=agent_id,
                                capabilities=resolved_caps,
                                metadata=self.client.identity.metadata,
                            )
                        except Exception:  # noqa: BLE001
                            _logger.warning("post-job agent heartbeat failed", exc_info=True)
                else:
                    stop_event.wait(idle_sleep_seconds)
        finally:
            # Graceful shutdown: deregister agent
            if agent_id is not None:
                try:
                    self.client.agent_down(agent_id=agent_id, mode="force")
                except Exception:  # noqa: BLE001
                    pass  # Best-effort; sweeper will clean up

        return outcomes

    def run_once(
        self,
        *,
        agent_id: str | None = None,
        capability_id: str | None = None,
        lease_ttl_seconds: int = 120,
        heartbeat_interval_seconds: float = 10.0,
        max_local_recoveries: int = 1,
        max_local_recovery_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if not self._registered:
            self.client.register()
            self._registered = True
        _append_runtime_log(
            self.client.identity.runtime_id,
            {
                "kind": "runtime_worker",
                "action": "run_once_started",
                "host_kind": self.host.kind,
                "adapter_kind": self.adapter.kind,
            },
        )
        import httpx as _httpx

        try:
            claimed = self.client.claim(
                agent_id=agent_id,
                capability=capability_id,
                lease_ttl_seconds=lease_ttl_seconds,
            )
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                # 409 Conflict: runtime/agent is busy or no claim available.
                # Normal idle behavior — not a failure.
                return {"claimed": False}
            raise
        if not claimed.get("claimed"):
            _append_runtime_log(self.client.identity.runtime_id, {"kind": "runtime_worker", "action": "idle_no_claim"})
            return {"claimed": False}

        run = claimed["run"]
        lease = claimed["lease"]
        session = self.host.get_or_create_session(agent_id=claimed["agent_id"])
        with self._session_lock:
            self._active_session = session
        stop = Event()
        startup_settled = Event()
        tui_died = Event()
        session.metadata["startup_settled_event"] = startup_settled
        self._active_startup_settled = startup_settled

        max_missed_heartbeats = 6
        lease_lost = Event()  # signals that we lost the lease / fencing
        interrupt_event = Event()  # set by heartbeat when CP requests interrupt
        self._interrupt_event = interrupt_event
        thread: Thread | None = None
        _hb_client = None

        def heartbeat_loop() -> None:
            consecutive_misses = 0
            heartbeat_count = 0
            try:
                while not stop.wait(heartbeat_interval_seconds):
                    if (
                        startup_settled.is_set()
                        and hasattr(self.host, "is_foreground_tui")
                        and heartbeat_count % 3 == 2
                    ):
                        with self._session_lock:
                            active_session = getattr(self, "_active_session", session)
                        try:
                            if not self.host.is_foreground_tui(active_session):
                                _append_runtime_log(
                                    self.client.identity.runtime_id,
                                    {
                                        "kind": "runtime_worker",
                                        "action": "tui_died_detected",
                                        "run_id": run["run_id"],
                                        "session_id": active_session.session_id,
                                        "host_kind": self.host.kind,
                                    },
                                )
                                self._active_tui_died_reason = "tui exited during execution"
                                tui_died.set()
                                try:
                                    self.host.interrupt(active_session)
                                except Exception:  # noqa: BLE001
                                    pass
                                stop.set()
                                break
                        except Exception:  # noqa: BLE001
                            _logger.warning("foreground TUI check failed", exc_info=True)
                    try:
                        resp = _hb_client.post(
                            f"/runs/{run['run_id']}/heartbeat",
                            json={
                                "runtime_id": self.client.identity.runtime_id,
                                "lease_id": lease["lease_id"],
                                "fencing_token": lease["fencing_token"],
                                "extend_seconds": lease_ttl_seconds,
                            },
                        )
                        resp.raise_for_status()
                        hb_response = resp.json()["data"]
                        consecutive_misses = 0
                        heartbeat_count += 1
                        if isinstance(hb_response, dict) and hb_response.get("interrupt_requested"):
                            _append_runtime_log(
                                self.client.identity.runtime_id,
                                {"kind": "runtime_worker", "action": "interrupt_via_heartbeat", "run_id": run["run_id"]},
                            )
                            interrupt_event.set()
                            try:
                                with self._session_lock:
                                    s = getattr(self, "_active_session", session)
                                self.host.interrupt(s)
                            except Exception:  # noqa: BLE001
                                pass
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
                            try:
                                with self._session_lock:
                                    s = getattr(self, "_active_session", session)
                                self.host.interrupt(s)
                            except Exception:  # noqa: BLE001
                                pass
                            stop.set()
                            break
            finally:
                if _hb_client is not None:
                    _hb_client.close()

        self._active_tui_died = tui_died
        self._active_tui_died_reason = "tui exited during execution"
        try:
            self._stage_job_attachments(session=session, claimed=claimed)
            # Attempt to restore a cursor checkpoint from a previous runtime
            # process.  The adapter can use this via session.metadata["restored_cursor"].
            restored = self.host.load_cursor(session)
            if restored is not None:
                session.metadata["restored_cursor"] = restored
            self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)

            # Dedicated httpx client for the heartbeat thread so it never blocks
            # on the main thread's connection pool (critical over SSH tunnels).
            import httpx as _httpx
            _hb_headers: dict[str, str] = {}
            if self.client.identity.token:
                _hb_headers["Authorization"] = f"Bearer {self.client.identity.token}"
            _hb_client = _httpx.Client(
                base_url=self.client.identity.server_url.rstrip("/"),
                timeout=10.0,
                headers=_hb_headers,
            )
            initial_hb = self.client.heartbeat(
                run_id=run["run_id"],
                lease_id=lease["lease_id"],
                fencing_token=lease["fencing_token"],
                extend_seconds=lease_ttl_seconds,
            )
            # Check the initial heartbeat response for pre-existing interrupts.
            if isinstance(initial_hb, dict) and initial_hb.get("interrupt_requested"):
                interrupt_event.set()
            thread = Thread(target=heartbeat_loop, daemon=True)
            thread.start()
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
                    if tui_died.is_set():
                        raise PaneDied(self._active_tui_died_reason)
                    break
                except StableButIndeterminate:
                    # The screen is stable but the adapter can't tell what
                    # happened.  Don't retry — surface the screen snapshot
                    # to the caller so they can decide.
                    raise
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
                    if isinstance(exc, (AuthFailure, BootstrapFailure)):
                        # Auth / bootstrap failures won't resolve by retrying
                        # the same session — re-raise to exhaust the budget
                        # and report the real cause.
                        raise
                    prior_session = session
                    startup_settled.clear()
                    prior_session.metadata.pop("startup_settled_event", None)
                    if isinstance(exc, PaneDied) or not self.host.session_exists(session):
                        session = self.host.get_or_create_session(
                            agent_id=claimed["agent_id"],
                            workspace_ref=session.workspace_ref,
                        )
                        session.metadata["startup_settled_event"] = startup_settled
                        self._active_startup_settled = startup_settled
                        with self._session_lock:
                            self._active_session = session
                        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
                    else:
                        session.metadata["startup_settled_event"] = startup_settled
                        self._active_startup_settled = startup_settled
                        with self._session_lock:
                            self._active_session = session
                        self.adapter.recover(
                            host=self.host,
                            session=session,
                            claimed=claimed,
                            attempt=attempts + 1,
                            error=exc,
                            supervisor=self,
                        )
                        # Re-bootstrap if recover() cleared the bootstrap flag
                        # (e.g. Codex TUI crashed and needs re-launch).
                        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
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
            stored_artifacts = [
                self._write_artifact(job_id=claimed["job"]["job_id"], payload=payload)
                for payload in result.artifacts
            ]
            try:
                completed = self.client.complete(
                    run_id=run["run_id"],
                    lease_id=lease["lease_id"],
                    fencing_token=lease["fencing_token"],
                    artifacts=stored_artifacts,
                    summary=result.summary,
                )
            except Exception as comp_exc:  # noqa: BLE001
                # Lease expired or CP rejected — job is lost; log and move on
                # instead of crashing into the death loop.
                _append_runtime_log(
                    self.client.identity.runtime_id,
                    {
                        "kind": "runtime_worker",
                        "action": "complete_rejected",
                        "run_id": run["run_id"],
                        "error": str(comp_exc),
                    },
                )
                _logger.warning("complete() rejected for run %s: %s", run["run_id"], comp_exc)
                return {"claimed": True, "claim": claimed, "error": str(comp_exc)}
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
            try:
                cancelled = self.client.cancel(
                    run_id=run["run_id"],
                    lease_id=lease["lease_id"],
                    fencing_token=lease["fencing_token"],
                    reason=str(exc),
                )
            except Exception as cancel_exc:  # noqa: BLE001
                _logger.warning("cancel() rejected for run %s: %s", run["run_id"], cancel_exc)
                return {"claimed": True, "claim": claimed, "cancelled": True, "error": str(cancel_exc)}
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
            try:
                failed = self.client.fail(
                    run_id=run["run_id"],
                    lease_id=lease["lease_id"],
                    fencing_token=lease["fencing_token"],
                    error=str(exc),
                    artifacts=artifacts,
                    summary=failure_result.summary,
                )
            except Exception as fail_exc:  # noqa: BLE001
                _append_runtime_log(
                    self.client.identity.runtime_id,
                    {
                        "kind": "runtime_worker",
                        "action": "fail_rejected",
                        "run_id": run["run_id"],
                        "error": str(fail_exc),
                    },
                )
                _logger.warning("fail() rejected for run %s: %s", run["run_id"], fail_exc)
                return {"claimed": True, "claim": claimed, "error": str(exc)}
            return {"claimed": True, "claim": claimed, "error": str(exc), "result": failed}
        finally:
            with self._session_lock:
                self._active_session = None
            self._active_startup_settled = None
            self._active_tui_died = None
            self._active_tui_died_reason = None
            session.metadata.pop("startup_settled_event", None)
            stop.set()
            if thread is not None:
                thread.join(timeout=heartbeat_interval_seconds + 1.0)
            if _hb_client is not None:
                _hb_client.close()  # idempotent if thread already closed it
            # Workspace cleanup: remove temp files, stale locks, session residue
            self._cleanup_workspace(session, claimed)
