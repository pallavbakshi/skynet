"""Turn parsing and content extraction for Claude Code TUI output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agp.plugins.claude_code._markers import (
    COMPACTION_RE,
    PROMPT_PREFIX,
    RESPONSE_PREFIXES,
    TOOL_RESULT_PREFIX,
)
from agp.plugins.claude_code._normalize import is_noise_line


@dataclass
class Turn:
    """A single prompt/response turn in a Claude Code conversation."""

    prompt: str = ""
    response_lines: list[str] = field(default_factory=list)

    @property
    def response(self) -> str:
        return "\n".join(self.response_lines)


def _is_response_start(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in RESPONSE_PREFIXES)


def _response_content(line: str) -> str:
    """Strip the response marker prefix from a response line."""
    s = line.strip()
    for prefix in RESPONSE_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s


def _is_tool_result(line: str) -> bool:
    s = line.strip()
    return s.startswith(TOOL_RESULT_PREFIX)


def parse_turns(text: str) -> list[Turn]:
    """Parse Claude Code TUI output into structured prompt/response turns.

    Handles:
    - Prompt lines (❯ prefix)
    - Response lines (⏺/● prefix)
    - Tool result lines (⎿ prefix) as part of the response
    - Indented continuation lines
    - Conversation compaction (✻ marker) — only parses post-compaction
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()

    # Handle compaction: trim everything before the last compaction marker
    # if there are turns after it.
    last_compaction = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if COMPACTION_RE.match(s):
            last_compaction = i
    if last_compaction >= 0:
        post_compaction = lines[last_compaction + 1:]
        # Only trim if there are actual turns after compaction
        has_post_prompt = any(
            ln.strip().startswith(PROMPT_PREFIX) for ln in post_compaction
        )
        if has_post_prompt:
            lines = post_compaction

    turns: list[Turn] = []
    current: Turn | None = None
    in_response = False

    for line in lines:
        s = line.strip()
        if not s:
            # Blank lines: preserve in response as paragraph breaks
            if in_response and current is not None:
                current.response_lines.append("")
            continue

        # Prompt start
        if s.startswith(PROMPT_PREFIX):
            prompt_text = s[len(PROMPT_PREFIX):].strip()
            current = Turn(prompt=prompt_text)
            turns.append(current)
            in_response = False
            continue

        # Response start
        if _is_response_start(line):
            if current is not None:
                content = _response_content(line)
                current.response_lines.append(content)
                in_response = True
            continue

        # Tool result (part of response)
        if _is_tool_result(line):
            if current is not None:
                content = s[len(TOOL_RESULT_PREFIX):].strip()
                current.response_lines.append(f"  {TOOL_RESULT_PREFIX} {content}")
                in_response = True
            continue

        # Skip noise lines (separators, status bar, box chrome)
        if is_noise_line(line):
            continue

        # Continuation: indented text following a response
        if in_response and current is not None:
            current.response_lines.append(s)

    # Clean up trailing blank lines in responses
    for turn in turns:
        while turn.response_lines and not turn.response_lines[-1]:
            turn.response_lines.pop()

    return turns


def extract_last_response(text: str) -> str:
    """Extract the clean text of the last Claude response.

    Returns an empty string if no response is found.
    """
    turns = parse_turns(text)
    answered = [t for t in turns if t.response]
    if not answered:
        return ""
    return answered[-1].response
