"""Codex Tui implementation."""

from __future__ import annotations

from pathlib import Path
from shlex import quote
from uuid import uuid4

from smallops._types import IdleReason, ParsedResponse, Status
from smallops.tui.codex._classify import (
    classify_idle,
    ends_with_prompt,
    is_shell_returned,
)
from smallops.tui.codex._gates import gate_response
from smallops.tui.codex._parse import capture, parse, parse_response, parse_status


class CodexTui:
    """Tui implementation for Codex CLI."""

    kind = "codex"

    def __init__(
        self,
        *,
        cli: str = "codex",
        flags: str = "",
        send_via_launch: bool = False,
        defer_launch_until_send: bool = False,
        script_pty: bool = False,
    ) -> None:
        self._cli = cli
        self._flags = flags
        self.send_via_launch = send_via_launch
        self.defer_launch_until_send = defer_launch_until_send
        self.script_pty = script_pty

    def launch_command(self, *, cwd: str | None = None) -> str:
        parts = [self._cli]
        if self._flags:
            parts.append(self._flags)
        if cwd:
            escaped_cwd = cwd.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f"-C {quote(cwd)}")
            parts.append(f"-c {quote(f'projects.\"{escaped_cwd}\".trust_level=\"trusted\"')}")
        return " ".join(parts).strip()

    def launch_prompt_command(self, prompt: str, *, cwd: str | None = None) -> str:
        command = f"{self.launch_command(cwd=cwd)} {quote(prompt)}"
        if self.script_pty:
            return f"script -qfec {quote(command)} /tmp/smallops-codex.typescript"
        return command

    def format_send(
        self,
        prompt: str,
        *,
        file: str | None = None,
        sections: str | None = None,
        directory: str = "/tmp/smallops",
    ) -> tuple[str, str, str | None]:
        if file is not None:
            prompt = Path(file).read_text(encoding="utf-8")

        marker = f"SMALLOPS-CODEX-TASK-{uuid4().hex[:12]}"
        send_text = f"{marker} {prompt}"
        if sections:
            send_text += f"\n\n{sections}"
        return marker, send_text, None

    def classify_idle(self, screen: str) -> IdleReason:
        return classify_idle(screen)

    def parse_response(self, text: str, marker: str) -> str:
        return parse_response(text, marker)

    def parse_blocks(self, text: str, marker: str) -> ParsedResponse:
        return parse(capture(text, marker))

    def gate_response(self, screen: str) -> str | None:
        return gate_response(screen)

    def ends_with_prompt(self, screen: str) -> bool:
        return ends_with_prompt(screen)

    def is_shell_returned(self, screen: str) -> bool:
        return is_shell_returned(screen)

    def is_fatal_gate(self, screen: str) -> bool:
        from smallops.tui.codex._gates import FATAL_GATE_PATTERNS
        return any(pat in screen.lower() for pat in FATAL_GATE_PATTERNS)

    def parse_status(self, screen: str) -> Status:
        return parse_status(screen)
