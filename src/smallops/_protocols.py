"""Protocol definitions for Mux and Tui plugins."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from smallops._types import IdleReason, ParsedResponse, SessionInfo, Status


@runtime_checkable
class Mux(Protocol):
    """Terminal multiplexer that owns panes.

    Implementations: TmuxMux, WezTermMux, HerdrMux.
    """

    kind: str  # e.g. "tmux", "wezterm"

    def create_session(self, *, name: str, cwd: str | None = None) -> SessionInfo: ...

    def destroy_session(self, session: SessionInfo) -> None: ...

    def session_exists(self, session: SessionInfo) -> bool: ...

    def send_text(self, session: SessionInfo, text: str, *, enter: bool = True) -> None: ...

    def peek(self, session: SessionInfo, n: int | None = None) -> str:
        """Raw screen capture.  None = visible only, n = last n lines from scrollback."""
        ...

    def shell_idle(self, session: SessionInfo) -> bool:
        """True if the foreground process is a shell (not a TUI agent)."""
        ...

    def respawn(self, session: SessionInfo, command: str, *, env: dict[str, str] | None = None) -> SessionInfo:
        """Replace the pane's process with command. No TTY echo. Returns (possibly new) SessionInfo."""
        ...

    def interrupt(self, session: SessionInfo) -> None:
        """Send Ctrl-C to the pane."""
        ...


@runtime_checkable
class Tui(Protocol):
    """Agent CLI lifecycle and output parsing.

    Implementations: ClaudeCodeTui, CodexTui.
    """

    kind: str  # e.g. "claude_code", "codex"

    def launch_command(self, *, cwd: str | None = None) -> str:
        """Shell command to start the agent CLI."""
        ...

    def classify_idle(self, screen: str) -> IdleReason:
        """Given a static screen, classify why it's idle."""
        ...

    def parse_response(self, text: str, marker: str) -> str:
        """Extract the agent's LLM text from raw output, using marker as anchor."""
        ...

    def parse_blocks(self, text: str, marker: str) -> ParsedResponse:
        """Capture + parse raw output into structured blocks."""
        ...

    def gate_response(self, screen: str) -> str | None:
        """If screen shows a gate prompt, return the key to send.  None = no gate."""
        ...

    def ends_with_prompt(self, screen: str) -> bool:
        """True if the last meaningful screen line is an idle prompt."""
        ...

    def is_shell_returned(self, screen: str) -> bool:
        """True if the agent TUI exited and the shell prompt is visible."""
        ...

    def is_fatal_gate(self, screen: str) -> bool:
        """True if screen shows a gate that cannot be auto-dismissed."""
        ...

    def parse_status(self, screen: str) -> Status:
        """Extract structured status from the TUI status line."""
        ...
