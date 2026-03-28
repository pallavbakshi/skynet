"""Data classes and exceptions for the runtime package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


class PaneDied(RecoverableExecutionError):
    """Raised when the terminal pane or foreground CLI died."""


class ExecutionTimeout(RecoverableExecutionError):
    """Raised when execution or readiness polling timed out."""


class AuthFailure(RecoverableExecutionError):
    """Raised when execution is blocked on authentication."""


class BootstrapFailure(RecoverableExecutionError):
    """Raised when adapter bootstrap fails in a known way."""


class AdapterExecutionFailed(Exception):
    """Raised when an adapter observed a terminal task-level failure."""

    def __init__(self, message: str, *, transcript: str = "", output: str = "") -> None:
        super().__init__(message)
        self.transcript = transcript
        self.output = output
