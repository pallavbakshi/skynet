"""Claude Code Tui implementation."""

from __future__ import annotations

from smallops._types import IdleReason, ParsedResponse, Status
from smallops.tui.claude_code._classify import classify_idle
from smallops.tui.claude_code._gates import gate_response
from smallops.tui.claude_code._parse import capture, parse, parse_response, parse_status


class ClaudeCodeTui:
    """Tui implementation for Claude Code CLI."""

    kind = "claude_code"

    def __init__(self, *, cli: str = "claude", flags: str = "--dangerously-skip-permissions") -> None:
        self._cli = cli
        self._flags = flags

    def launch_command(self, *, cwd: str | None = None) -> str:
        return f"{self._cli} {self._flags}".strip()

    def classify_idle(self, screen: str) -> IdleReason:
        return classify_idle(screen)

    def parse_response(self, text: str, marker: str) -> str:
        return parse_response(text, marker)

    def parse_blocks(self, text: str, marker: str) -> ParsedResponse:
        return parse(capture(text, marker))

    def gate_response(self, screen: str) -> str | None:
        return gate_response(screen)

    def is_fatal_gate(self, screen: str) -> bool:
        from smallops.tui.claude_code._gates import FATAL_GATE_PATTERNS
        return any(pat in screen.lower() for pat in FATAL_GATE_PATTERNS)

    def parse_status(self, screen: str) -> Status:
        return parse_status(screen)
