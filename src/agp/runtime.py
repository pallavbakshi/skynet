"""Runtime supervision, terminal host/adapter ABCs, and plugin runner.

``RuntimeClient`` and ``RuntimeIdentity`` are re-exported from
``agp.client`` for backward compatibility.  The canonical import is::

    from agp.client import RuntimeClient, RuntimeIdentity
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
import uuid
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import httpx

from agp.artifact_store import ArtifactStore, get_artifact_store
from agp.config import settings
from agp.db import current_release_version
from agp.logs import append_jsonl_log

# Re-export from SDK package — canonical location is agp.client
from agp.client._runtime import RuntimeClient, RuntimeIdentity  # noqa: F401


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


@dataclass(slots=True)
class ExecutionResult:
    artifacts: list["ArtifactPayload"]
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ArtifactPayload:
    role: str
    name: str
    content: str
    content_type: str = "text/plain"


@dataclass(slots=True)
class TerminalSession:
    session_id: str
    agent_id: str
    workspace_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutputCursor:
    session_id: str
    checkpoint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutputReadResult:
    session_id: str
    cursor: OutputCursor
    text: str
    full_text: str
    changed: bool


@dataclass(slots=True)
class SessionHealth:
    session_id: str
    exists: bool
    healthy: bool
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class InterruptRequested(Exception):
    """Raised when the control plane has requested interruption for a run."""


class RecoverableExecutionError(Exception):
    """Raised when execution should enter bounded local recovery."""


class AdapterExecutionFailed(Exception):
    """Raised when an adapter observed a terminal task-level failure."""

    def __init__(self, message: str, *, transcript: str = "", output: str = "") -> None:
        super().__init__(message)
        self.transcript = transcript
        self.output = output


class _OutputAccumulator:
    """Append-only output log for durable session transcript capture.

    Persists all deltas to a file so the full transcript is available even
    when the terminal scrollback buffer shifts.  The file survives runtime
    restarts — on reload the prior content is recovered automatically.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[str] = []
        if path.exists():
            self._buffer = [path.read_text()]

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._buffer.append(delta)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as fh:
            fh.write(delta)

    @property
    def text(self) -> str:
        return "".join(self._buffer)

    def reset(self) -> None:
        self._buffer = []
        if self._path.exists():
            self._path.unlink()


def _compute_output_delta(current_text: str, prior_text: str) -> str:
    """Compute new output since the last read, surviving scrollback shifts.

    Strategy:
    1. Fast path — prior is a prefix of current (buffer did not shift).
    2. Slow path — find trailing-line anchors from prior in current.
    3. Fallback — return all of current (data gap, best effort).
    """
    if not prior_text:
        return current_text
    if not current_text:
        return ""
    if current_text.startswith(prior_text):
        return current_text[len(prior_text):]

    prior_lines = prior_text.splitlines()
    current_lines = current_text.splitlines()
    if not prior_lines:
        return current_text

    for anchor_size in (20, 10, 5, 3, 2):
        if anchor_size > len(prior_lines):
            continue
        anchor = prior_lines[-anchor_size:]
        for i in range(len(current_lines) - anchor_size + 1):
            if current_lines[i : i + anchor_size] == anchor:
                new_start = i + anchor_size
                if new_start >= len(current_lines):
                    return ""
                return "\n".join(current_lines[new_start:]) + "\n"

    return current_text


_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;]*[A-Za-z]"  # CSI sequences
    r"|\][^\x07]*\x07"  # OSC sequences (terminated by BEL)
    r"|\][^\x1b]*\x1b\\"  # OSC sequences (terminated by ST)
    r"|[^[\]][^\x1b]?"  # two-char escapes
    r")",
    re.DOTALL,
)

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_RE.sub("", text)


class TerminalHost(ABC):
    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        raise NotImplementedError

    @abstractmethod
    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        raise NotImplementedError

    @abstractmethod
    def read_output(self, session: TerminalSession, cursor: OutputCursor) -> OutputReadResult:
        raise NotImplementedError

    @abstractmethod
    def interrupt(self, session: TerminalSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_session(self, session: TerminalSession) -> TerminalSession:
        raise NotImplementedError

    @abstractmethod
    def terminate_session(self, session: TerminalSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def session_exists(self, session: TerminalSession) -> bool:
        raise NotImplementedError

    def load_cursor(self, session: TerminalSession) -> "OutputCursor | None":  # noqa: ARG002
        """Load a persisted cursor from a previous runtime process.

        Returns None if no checkpoint exists.  Hosts that support
        restart-safe cursors should override this.
        """
        return None

    def read_visible(self, session: TerminalSession) -> str:  # noqa: ARG002
        """Read the currently visible screen content (including alternate buffer).

        Default returns empty string.  Hosts that can capture the alternate
        screen buffer should override this.
        """
        return ""

    @abstractmethod
    def health(self, session: TerminalSession) -> SessionHealth:
        raise NotImplementedError

    def wait_for_idle(
        self,
        session: TerminalSession,
        *,
        poll_seconds: float = 2.0,
        idle_after: int = 3,
        timeout_seconds: float = 0.0,
        check_lines: int = 20,
        on_poll: Any | None = None,
    ) -> bool:
        """Block until pane output stops changing.

        Returns True when idle is detected, False on timeout.
        *on_poll* is called each iteration and may raise to abort.
        Default implementation returns True immediately (for in-process hosts).
        """
        return True


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:
        return None

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        return {
            "adapter_kind": self.kind,
            "supported": False,
            "run_id": run_id,
            "text_length": len(text),
        }

    @abstractmethod
    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        raise NotImplementedError

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> None:
        sleep(0.01)

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=claimed["message"]["text"]),
                ArtifactPayload(
                    role="transcript_log",
                    name="transcript.txt",
                    content=f"runtime.failed\nerror={type(error).__name__}: {error}\n",
                ),
                ArtifactPayload(role="exec_log", name="exec.txt", content="failure-path\n"),
                ArtifactPayload(
                    role="failure_evidence",
                    name="failure.txt",
                    content=f"{type(error).__name__}: {error}\n",
                ),
            ],
            summary={"adapter": self.kind, "exception_type": type(error).__name__},
        )


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
        self.artifact_store = artifact_store or get_artifact_store(settings.artifact_backend, self.artifact_root)

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

        def heartbeat_loop() -> None:
            while not stop.wait(heartbeat_interval_seconds):
                try:
                    self.client.heartbeat(
                        run_id=run["run_id"],
                        lease_id=lease["lease_id"],
                        fencing_token=lease["fencing_token"],
                        extend_seconds=lease_ttl_seconds,
                    )
                except Exception:  # noqa: BLE001
                    _append_runtime_log(
                        self.client.identity.runtime_id,
                        {"kind": "runtime_worker", "action": "heartbeat_loop_stopped", "run_id": run["run_id"]},
                    )
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


@dataclass(slots=True)
class StandaloneArtifactRecord:
    role: str
    name: str
    path: str
    content_type: str
    size_bytes: int


@dataclass(slots=True)
class StandaloneRunResult:
    ok: bool
    host_kind: str
    adapter_kind: str
    agent_id: str
    session_id: str
    run_id: str
    output_dir: str
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: list[StandaloneArtifactRecord] = field(default_factory=list)
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    exception_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "host_kind": self.host_kind,
            "adapter_kind": self.adapter_kind,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "output_dir": self.output_dir,
            "summary": self.summary,
            "artifacts": [
                {
                    "role": artifact.role,
                    "name": artifact.name,
                    "path": artifact.path,
                    "content_type": artifact.content_type,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in self.artifacts
            ],
            "progress_events": self.progress_events,
            "error": self.error,
            "exception_type": self.exception_type,
        }


class _StandaloneSupervisorContext:
    def __init__(self) -> None:
        self.progress_events: list[dict[str, Any]] = []
        identity = type("Identity", (), {"runtime_id": "standalone-runtime"})()
        self.client = type("Client", (), {"identity": identity})()

    def check_interrupt(self, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        return None

    def emit_progress(self, claimed: dict[str, Any], *, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "message": message,
            "details": details or {},
            "job_id": claimed["job"]["job_id"],
            "run_id": claimed["run"]["run_id"],
        }
        self.progress_events.append(event)
        return event


def _failure_snapshot_payloads(*, host: TerminalHost, session: TerminalSession, error: Exception) -> list[ArtifactPayload]:
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


class StandalonePluginRunner:
    """Run one host+adapter task locally without control-plane semantics."""

    def __init__(
        self,
        *,
        host: TerminalHost,
        adapter: AgentAdapter,
        output_root: str | Path = ".agp-plugin-runs",
    ) -> None:
        self.host = host
        self.adapter = adapter
        self.output_root = Path(output_root)

    def _build_claimed(self, *, agent_id: str, task: str, run_id: str) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "job": {"job_id": f"local-job-{run_id}"},
            "run": {"run_id": run_id},
            "message": {"text": task, "metadata": {"standalone": True}},
            "lease": {"lease_id": f"local-lease-{run_id}", "fencing_token": 1},
        }

    def _write_artifacts(self, *, run_dir: Path, artifacts: list[ArtifactPayload]) -> list[StandaloneArtifactRecord]:
        run_dir.mkdir(parents=True, exist_ok=True)
        written: list[StandaloneArtifactRecord] = []
        for index, artifact in enumerate(artifacts, start=1):
            path = run_dir / f"{index:02d}-{artifact.role}-{artifact.name}"
            path.write_text(artifact.content, encoding="utf-8")
            written.append(
                StandaloneArtifactRecord(
                    role=artifact.role,
                    name=artifact.name,
                    path=str(path),
                    content_type=artifact.content_type,
                    size_bytes=path.stat().st_size,
                )
            )
        return written

    def run_once(
        self,
        *,
        agent_id: str,
        task: str,
        workspace_ref: str | None = None,
        keep_session: bool = False,
        run_id: str | None = None,
    ) -> StandaloneRunResult:
        actual_run_id = run_id or f"plugin-run-{uuid.uuid4().hex[:12]}"
        claimed = self._build_claimed(agent_id=agent_id, task=task, run_id=actual_run_id)
        session = self.host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
        restored = self.host.load_cursor(session)
        if restored is not None:
            session.metadata["restored_cursor"] = restored
        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
        context = _StandaloneSupervisorContext()
        run_dir = self.output_root / actual_run_id
        try:
            result = self.adapter.execute_run(
                host=self.host,
                session=session,
                claimed=claimed,
                supervisor=context,  # type: ignore[arg-type]
            )
            written = self._write_artifacts(run_dir=run_dir, artifacts=result.artifacts)
            return StandaloneRunResult(
                ok=True,
                host_kind=self.host.kind,
                adapter_kind=self.adapter.kind,
                agent_id=agent_id,
                session_id=session.session_id,
                run_id=actual_run_id,
                output_dir=str(run_dir),
                summary=result.summary,
                artifacts=written,
                progress_events=context.progress_events,
            )
        except Exception as exc:
            failure_result = self.adapter.build_failure_result(
                host=self.host,
                session=session,
                claimed=claimed,
                error=exc,
                supervisor=context,  # type: ignore[arg-type]
            )
            failure_result.artifacts.extend(_failure_snapshot_payloads(host=self.host, session=session, error=exc))
            written = self._write_artifacts(run_dir=run_dir, artifacts=failure_result.artifacts)
            return StandaloneRunResult(
                ok=False,
                host_kind=self.host.kind,
                adapter_kind=self.adapter.kind,
                agent_id=agent_id,
                session_id=session.session_id,
                run_id=actual_run_id,
                output_dir=str(run_dir),
                summary=failure_result.summary,
                artifacts=written,
                progress_events=context.progress_events,
                error=str(exc),
                exception_type=type(exc).__name__,
            )
        finally:
            if not keep_session:
                try:
                    self.host.terminate_session(session)
                except Exception:  # noqa: BLE001
                    pass



# Backward-compatible re-exports — plugins are the canonical location.
# Lazy imports via __getattr__ to avoid circular-import issues when plugin
# modules import core types from this file.

_COMPAT_IMPORTS: dict[str, tuple[str, str]] = {
    "InProcessTerminalHost": ("agp.plugins.inprocess", "InProcessTerminalHost"),
    "DefaultAgentAdapter": ("agp.plugins.inprocess", "DefaultAgentAdapter"),
    "WezTermHost": ("agp.plugins.wezterm", "WezTermHost"),
    "CodexAdapter": ("agp.plugins.codex", "CodexAdapter"),
    "_clean_codex_tui_output": ("agp.plugins.codex", "_clean_codex_tui_output"),
    "build_terminal_host": ("agp.plugins", "build_terminal_host"),
    "build_agent_adapter": ("agp.plugins", "build_agent_adapter"),
}


def __getattr__(name: str):  # noqa: E302
    if name in _COMPAT_IMPORTS:
        import importlib
        mod_path, attr = _COMPAT_IMPORTS[name]
        mod = importlib.import_module(mod_path)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
