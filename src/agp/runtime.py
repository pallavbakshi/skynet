"""Runtime-side HTTP client for the AGP MVP."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import httpx

from agp.artifact_store import ArtifactStore, get_artifact_store
from agp.config import settings
from agp.db import current_release_version
from agp.logs import append_jsonl_log


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


@dataclass(slots=True)
class RuntimeIdentity:
    runtime_id: str
    hostname: str
    server_url: str = "http://127.0.0.1:7860"
    release_version: str = field(default_factory=current_release_version)
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeClient:
    """Thin runtime client over the control-plane HTTP API."""

    def __init__(self, identity: RuntimeIdentity, timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self.identity = identity
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=identity.server_url.rstrip("/"), timeout=timeout)

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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "register", "hostname": self.identity.hostname},
        )
        return payload

    def claim(self, *, agent_id: str | None = None, capability_id: str | None = None, lease_ttl_seconds: int = 30) -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {
                "kind": "runtime_client",
                "action": "claim",
                "agent_id": agent_id,
                "capability_id": capability_id,
                "claimed": payload.get("claimed", False),
                "job_id": payload.get("job", {}).get("job_id"),
                "run_id": payload.get("run", {}).get("run_id"),
            },
        )
        return payload

    def heartbeat(self, *, run_id: str, lease_id: str, fencing_token: int, extend_seconds: int = 30) -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "heartbeat", "run_id": run_id, "lease_id": lease_id},
        )
        return payload

    def progress(self, *, run_id: str, lease_id: str, fencing_token: int, message: str, details: dict[str, Any] | None = None) -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "progress", "run_id": run_id, "lease_id": lease_id, "message": message},
        )
        return payload

    def recovering(self, *, run_id: str, lease_id: str, fencing_token: int, details: dict[str, Any] | None = None) -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "recovering", "run_id": run_id, "lease_id": lease_id},
        )
        return payload

    def resumed(self, *, run_id: str, lease_id: str, fencing_token: int, details: dict[str, Any] | None = None) -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "resumed", "run_id": run_id, "lease_id": lease_id},
        )
        return payload

    def get_job(self, job_id: str) -> dict:
        response = self._client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()["data"]
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "get_job", "job_id": job_id, "job_status": payload.get("status")},
        )
        return payload

    def cancel(self, *, run_id: str, lease_id: str, fencing_token: int, reason: str = "interrupt_requested") -> dict:
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
        _append_runtime_log(
            self.identity.runtime_id,
            {"kind": "runtime_client", "action": "cancel", "run_id": run_id, "lease_id": lease_id, "reason": reason},
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
        _append_runtime_log(
            self.identity.runtime_id,
            {
                "kind": "runtime_client",
                "action": "complete",
                "run_id": run_id,
                "lease_id": lease_id,
                "artifact_roles": [artifact["role"] for artifact in artifacts],
            },
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
        _append_runtime_log(
            self.identity.runtime_id,
            {
                "kind": "runtime_client",
                "action": "fail",
                "run_id": run_id,
                "lease_id": lease_id,
                "error": error,
                "artifact_roles": [artifact["role"] for artifact in artifacts or []],
            },
        )
        return payload


def register_runtime(identity: RuntimeIdentity) -> dict:
    """Register a runtime with the control plane."""

    client = RuntimeClient(identity)
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

    @abstractmethod
    def health(self, session: TerminalSession) -> SessionHealth:
        raise NotImplementedError


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:
        return None

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


class InProcessTerminalHost(TerminalHost):
    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._history: dict[str, list[str]] = {}

    @property
    def kind(self) -> str:
        return "inprocess"

    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        session = self._sessions.get(agent_id)
        if session is None:
            session = TerminalSession(session_id=f"inproc-{agent_id}", agent_id=agent_id, workspace_ref=workspace_ref)
            self._sessions[agent_id] = session
            self._history[session.session_id] = []
        return session

    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        suffix = "\n" if enter else ""
        self._history.setdefault(session.session_id, []).append(f"SEND:{text}{suffix}")

    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        history = "".join(self._history.get(session.session_id, []))
        return OutputCursor(session_id=session.session_id, checkpoint=history)

    def read_output(self, session: TerminalSession, cursor: OutputCursor) -> OutputReadResult:
        full_text = "".join(self._history.get(session.session_id, []))
        prior = cursor.checkpoint
        if full_text.startswith(prior):
            delta = full_text[len(prior):]
        else:
            delta = full_text
        updated = OutputCursor(session_id=session.session_id, checkpoint=full_text, metadata=dict(cursor.metadata))
        return OutputReadResult(
            session_id=session.session_id,
            cursor=updated,
            text=delta,
            full_text=full_text,
            changed=bool(delta),
        )

    def interrupt(self, session: TerminalSession) -> None:
        self._history.setdefault(session.session_id, []).append("INTERRUPT")

    def reset_session(self, session: TerminalSession) -> TerminalSession:
        reset = TerminalSession(
            session_id=f"{session.session_id}-reset-{int(monotonic() * 1000)}",
            agent_id=session.agent_id,
            workspace_ref=session.workspace_ref,
            metadata=dict(session.metadata),
        )
        self._sessions[session.agent_id] = reset
        self._history[reset.session_id] = []
        return reset

    def terminate_session(self, session: TerminalSession) -> None:
        self._sessions.pop(session.agent_id, None)

    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "workspace_ref": session.workspace_ref,
            "history": list(self._history.get(session.session_id, [])),
        }

    def session_exists(self, session: TerminalSession) -> bool:
        existing = self._sessions.get(session.agent_id)
        return existing is not None and existing.session_id == session.session_id

    def health(self, session: TerminalSession) -> SessionHealth:
        exists = self.session_exists(session)
        return SessionHealth(
            session_id=session.session_id,
            exists=exists,
            healthy=exists,
            reason=None if exists else "session_missing",
            metadata={"host_kind": self.kind},
        )


class WezTermHost(TerminalHost):
    def __init__(
        self,
        *,
        wezterm_bin: str = "wezterm",
        workspace: str = "agp",
        shell_argv: list[str] | None = None,
        runner: Any | None = None,
    ) -> None:
        self.wezterm_bin = wezterm_bin
        self.workspace = workspace
        self.shell_argv = shell_argv or ["zsh", "-l"]
        self._runner = runner or subprocess.run

    @property
    def kind(self) -> str:
        return "wezterm"

    def _run(self, args: list[str], *, stdin_text: str | None = None) -> str:
        completed = self._runner(
            [self.wezterm_bin, "cli", *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(f"wezterm command failed: {' '.join(args)} :: {stderr}")
        return completed.stdout or ""

    def _marker(self, agent_id: str) -> str:
        return f"AGP:{agent_id}"

    def _list_panes(self) -> list[dict[str, Any]]:
        raw = self._run(["list", "--format", "json"])
        if not raw:
            return []
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else []

    def _find_existing(self, *, agent_id: str) -> TerminalSession | None:
        marker = self._marker(agent_id)
        for pane in self._list_panes():
            if pane.get("workspace") != self.workspace:
                continue
            if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                return TerminalSession(
                    session_id=str(pane["pane_id"]),
                    agent_id=agent_id,
                    workspace_ref=pane.get("cwd"),
                    metadata={
                        "pane_id": pane["pane_id"],
                        "tab_id": pane.get("tab_id"),
                        "window_id": pane.get("window_id"),
                        "workspace": pane.get("workspace"),
                    },
                )
        return None

    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        existing = self._find_existing(agent_id=agent_id)
        if existing is not None:
            return existing
        args = ["spawn", "--new-window", "--workspace", self.workspace]
        if workspace_ref:
            args.extend(["--cwd", workspace_ref])
        args.extend(["--", *self.shell_argv])
        pane_id = self._run(args).strip()
        session = TerminalSession(
            session_id=pane_id,
            agent_id=agent_id,
            workspace_ref=workspace_ref,
            metadata={"pane_id": int(pane_id), "workspace": self.workspace},
        )
        self._run(["set-window-title", "--pane-id", pane_id, self._marker(agent_id)])
        self._run(["set-tab-title", "--pane-id", pane_id, self._marker(agent_id)])
        return session

    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        self._run(["send-text", "--pane-id", session.session_id, "--no-paste", text])
        if enter:
            self._run(["send-text", "--pane-id", session.session_id, "--no-paste", "\r"])

    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        baseline = self._run(["get-text", "--pane-id", session.session_id, "--start-line", "-200"])
        return OutputCursor(session_id=session.session_id, checkpoint=baseline)

    def read_output(self, session: TerminalSession, cursor: OutputCursor) -> OutputReadResult:
        full_text = self._run(["get-text", "--pane-id", session.session_id, "--start-line", "-200"])
        prior = cursor.checkpoint
        if full_text.startswith(prior):
            delta = full_text[len(prior):]
        else:
            delta = full_text
        updated = OutputCursor(session_id=session.session_id, checkpoint=full_text, metadata=dict(cursor.metadata))
        return OutputReadResult(
            session_id=session.session_id,
            cursor=updated,
            text=delta,
            full_text=full_text,
            changed=bool(delta),
        )

    def interrupt(self, session: TerminalSession) -> None:
        self._run(["send-text", "--pane-id", session.session_id, "--no-paste", "\u0003"])

    def reset_session(self, session: TerminalSession) -> TerminalSession:
        try:
            self.terminate_session(session)
        except Exception:  # noqa: BLE001
            pass
        return self.get_or_create_session(agent_id=session.agent_id, workspace_ref=session.workspace_ref)

    def terminate_session(self, session: TerminalSession) -> None:
        self._run(["kill-pane", "--pane-id", session.session_id])

    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        pane = next((item for item in self._list_panes() if str(item.get("pane_id")) == session.session_id), None)
        text = self._run(["get-text", "--pane-id", session.session_id, "--start-line", "-200"])
        return {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "pane": pane,
            "text": text,
        }

    def session_exists(self, session: TerminalSession) -> bool:
        return any(str(item.get("pane_id")) == session.session_id for item in self._list_panes())

    def health(self, session: TerminalSession) -> SessionHealth:
        pane = next((item for item in self._list_panes() if str(item.get("pane_id")) == session.session_id), None)
        if pane is None:
            return SessionHealth(
                session_id=session.session_id,
                exists=False,
                healthy=False,
                reason="pane_missing",
                metadata={"host_kind": self.kind},
            )
        return SessionHealth(
            session_id=session.session_id,
            exists=True,
            healthy=True,
            reason=None,
            metadata={
                "host_kind": self.kind,
                "workspace": pane.get("workspace"),
                "pane_id": pane.get("pane_id"),
                "tab_id": pane.get("tab_id"),
                "window_id": pane.get("window_id"),
            },
        )


def build_terminal_host(kind: str, **kwargs: Any) -> TerminalHost:
    if kind == "inprocess":
        return InProcessTerminalHost()
    if kind == "wezterm":
        return WezTermHost(**kwargs)
    raise ValueError(f"unsupported terminal host kind: {kind}")


def build_agent_adapter(kind: str, **kwargs: Any) -> AgentAdapter:
    if kind == "default":
        return DefaultAgentAdapter(**kwargs)
    if kind == "codex":
        return CodexAdapter(
            begin_marker=kwargs.get("begin_marker", settings.codex_begin_marker),
            result_marker=kwargs.get("result_marker", settings.codex_result_marker),
            max_polls=kwargs.get("max_polls", settings.codex_max_polls),
            poll_interval_seconds=kwargs.get("poll_interval_seconds", settings.codex_poll_interval_seconds),
        )
    raise ValueError(f"unsupported agent adapter kind: {kind}")


class DefaultAgentAdapter(AgentAdapter):
    def __init__(self, *, execute: Any | None = None, recover: Any | None = None) -> None:
        self._execute = execute
        self._recover = recover

    @property
    def kind(self) -> str:
        return "default"

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        if self._execute is not None:
            custom = self._execute(claimed)
            if isinstance(custom, ExecutionResult):
                return custom
            return custom  # type: ignore[return-value]

        host.send_text(session, claimed["message"]["text"], enter=True)
        artifacts = [
            ArtifactPayload(role="prompt", name="prompt.txt", content=claimed["message"]["text"]),
            ArtifactPayload(
                role="transcript_log",
                name="transcript.txt",
                content=f"runtime.started\nmessage={claimed['message']['text']}\n",
            ),
        ]
        for step in range(3):
            supervisor.check_interrupt(claimed)
            sleep(0.02)
            supervisor.emit_progress(claimed, message="runtime.step", details={"step": step + 1, "session_id": session.session_id})
        artifacts.append(ArtifactPayload(role="exec_log", name="exec.txt", content="step=1\nstep=2\nstep=3\n"))
        content = (
            f"runtime={supervisor.client.identity.runtime_id}\n"
            f"job_id={claimed['job']['job_id']}\n"
            f"message={claimed['message']['text']}\n"
            f"session_id={session.session_id}\n"
            f"host_kind={host.kind}\n"
            f"adapter_kind={self.kind}\n"
        )
        artifacts.append(ArtifactPayload(role="result", name="result.txt", content=content))
        return ExecutionResult(artifacts=artifacts, summary={"adapter": self.kind, "host": host.kind})

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
        if self._recover is not None:
            self._recover(claimed, attempt=attempt, error=error)
            return
        sleep(0.01)


class CodexAdapter(AgentAdapter):
    def __init__(
        self,
        *,
        begin_marker: str = "AGP_RUN_BEGIN",
        result_marker: str = "AGP_RUN_RESULT",
        max_polls: int = 20,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.begin_marker = begin_marker
        self.result_marker = result_marker
        self.max_polls = max_polls
        self.poll_interval_seconds = poll_interval_seconds

    @property
    def kind(self) -> str:
        return "codex"

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        if session.metadata.get("codex_bootstrapped"):
            return
        bootstrap = (
            "You are running inside AGP. "
            "Each AGP task will provide a run envelope. "
            f"When you see a line starting with {self.begin_marker} followed by a run id, treat that as the current task context. "
            f"When that task reaches a terminal outcome, emit exactly one line beginning with "
            f"{self.result_marker} <run_id> "
            'followed by compact JSON like {"status":"success","result":"..."} '
            'or {"status":"failure","error":"..."}. '
            f"Do not emit lines beginning with {self.result_marker} except as the single terminal line for the active AGP task."
        )
        host.send_text(session, bootstrap, enter=True)
        session.metadata["codex_bootstrapped"] = True

    def _begin_line(self, run_id: str) -> str:
        return f"{self.begin_marker} {run_id}"

    def _result_prefix(self, run_id: str) -> str:
        return f"{self.result_marker} {run_id} "

    def _task_payload(self, *, run_id: str, prompt: str) -> str:
        return (
            f"{self._begin_line(run_id)}\n"
            "AGP task instructions:\n"
            f"{prompt}\n\n"
            "Terminal contract:\n"
            f"- Finalize this task by emitting exactly one line that starts with {self._result_prefix(run_id)}\n"
            '- Use JSON payload {"status":"success","result":"..."} or {"status":"failure","error":"..."}\n'
            "- Do not emit terminal lines for any other run id.\n"
        )

    def _extract_terminal_payload(self, *, run_id: str, output: str) -> dict[str, Any] | None:
        prefix = self._result_prefix(run_id)
        for line in reversed(output.splitlines()):
            if not line.startswith(prefix):
                continue
            raw = line.removeprefix(prefix).strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid codex terminal payload for run {run_id}") from None
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid codex terminal payload type for run {run_id}")
            return payload
        return None

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        prompt = claimed["message"]["text"]
        run_id = claimed["run"]["run_id"]
        cursor = host.create_cursor(session)
        host.send_text(session, self._task_payload(run_id=run_id, prompt=prompt), enter=True)
        transcript_parts: list[str] = [f"prompt={prompt}\n"]
        for attempt in range(self.max_polls):
            supervisor.check_interrupt(claimed)
            read = host.read_output(session, cursor)
            cursor = read.cursor
            if read.changed and read.text:
                transcript_parts.append(read.text)
                supervisor.emit_progress(
                    claimed,
                    message="runtime.output",
                    details={
                        "adapter": self.kind,
                        "session_id": session.session_id,
                        "poll": attempt + 1,
                        "changed": True,
                    },
                )
                payload = self._extract_terminal_payload(run_id=run_id, output=read.full_text)
                if payload is not None:
                    transcript = "".join(transcript_parts)
                    status = str(payload.get("status", "")).strip().lower()
                    if status == "failure":
                        raise AdapterExecutionFailed(
                            str(payload.get("error") or "codex adapter reported task failure"),
                            transcript=transcript,
                            output=read.full_text,
                        )
                    if status != "success":
                        raise RuntimeError(f"invalid codex terminal status for run {run_id}: {status or 'missing'}")
                    result_text = str(payload.get("result") or "").strip()
                    return ExecutionResult(
                        artifacts=[
                            ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                            ArtifactPayload(role="transcript_log", name="transcript.txt", content=transcript),
                            ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                            ArtifactPayload(role="result", name="result.txt", content=result_text or json.dumps(payload, sort_keys=True)),
                        ],
                        summary={"adapter": self.kind, "host": host.kind, "run_id": run_id},
                    )
            sleep(self.poll_interval_seconds)
        raise RecoverableExecutionError("codex adapter did not observe completion marker before poll budget exhausted")

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        if isinstance(error, AdapterExecutionFailed):
            return ExecutionResult(
                artifacts=[
                    ArtifactPayload(role="prompt", name="prompt.txt", content=claimed["message"]["text"]),
                    ArtifactPayload(role="transcript_log", name="transcript.txt", content=error.transcript),
                    ArtifactPayload(role="exec_log", name="exec.txt", content=error.output),
                    ArtifactPayload(role="failure_evidence", name="failure.txt", content=str(error)),
                ],
                summary={"adapter": self.kind, "host": host.kind, "exception_type": type(error).__name__},
            )
        return super().build_failure_result(
            host=host,
            session=session,
            claimed=claimed,
            error=error,
            supervisor=supervisor,
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
        snapshot = self.host.snapshot(session)
        health = self.host.health(session)
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
