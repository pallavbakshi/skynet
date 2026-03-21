"""Abstract base classes for terminal hosts and agent adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from time import sleep
from typing import Any

from agp.runtime._types import (
    ArtifactPayload,
    ExecutionResult,
    OutputCursor,
    OutputReadResult,
    SessionHealth,
    TerminalSession,
)


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


# Avoid circular import — use string annotation above and resolve here
from agp.runtime._supervisor import RuntimeSupervisor as RuntimeSupervisor  # noqa: E402, F401
