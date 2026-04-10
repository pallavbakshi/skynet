"""Codex Tui implementation."""

from __future__ import annotations

from smallops._types import IdleReason, ParsedResponse, Status
from smallops.tui.codex._classify import classify_idle
from smallops.tui.codex._gates import gate_response
from smallops.tui.codex._parse import capture, parse, parse_response, parse_status


class CodexTui:
    """Tui implementation for Codex CLI."""

    kind = "codex"

    def __init__(self, *, cli: str = "codex", flags: str = "") -> None:
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
        from smallops.tui.codex._gates import FATAL_GATE_PATTERNS
        return any(pat in screen.lower() for pat in FATAL_GATE_PATTERNS)

    def parse_status(self, screen: str) -> Status:
        return parse_status(screen)
