"""Data classes and exceptions for the runtime package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExecutionResult:
    artifacts: list["ArtifactPayload"]
    summary: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] | None = None


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

    def __init__(self, message: str, *, snapshot_text: str = "", visible_text: str = "") -> None:
        super().__init__(message)
        self.snapshot_text = snapshot_text
        self.visible_text = visible_text


class ExecutionTimeout(RecoverableExecutionError):
    """Raised when execution or readiness polling timed out."""


class AuthFailure(RecoverableExecutionError):
    """Raised when execution is blocked on authentication."""


class BootstrapFailure(RecoverableExecutionError):
    """Raised when adapter bootstrap fails in a known way."""


class StableButIndeterminate(RecoverableExecutionError):
    """Raised when the screen is stable but the adapter cannot determine the state.

    The screen stopped changing, the agent isn't visibly working, but the adapter
    can't tell whether the task completed, a permission prompt appeared, or
    something else happened.  The caller should inspect the screen snapshot and
    decide what to do.
    """

    def __init__(self, message: str, *, screen: str = "", last_good_screen: str = "") -> None:
        super().__init__(message)
        self.screen = screen
        self.last_good_screen = last_good_screen


class AdapterExecutionFailed(Exception):
    """Raised when an adapter observed a terminal task-level failure."""

    def __init__(self, message: str, *, transcript: str = "", output: str = "") -> None:
        super().__init__(message)
        self.transcript = transcript
        self.output = output
