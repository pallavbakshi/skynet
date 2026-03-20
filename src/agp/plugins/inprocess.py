"""In-process terminal host and default agent adapter for testing."""
from __future__ import annotations
from time import monotonic, sleep
from typing import Any

from agp.runtime import (
    AgentAdapter, ArtifactPayload, ExecutionResult, OutputCursor, OutputReadResult,
    SessionHealth, TerminalHost, TerminalSession,
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

    def read_visible(self, session: TerminalSession) -> str:
        return "".join(self._history.get(session.session_id, []))

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
