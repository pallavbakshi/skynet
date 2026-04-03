"""Turn parsing and content extraction for Claude Code TUI output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agp.plugins.claude_code._markers import (
    COMPACTION_RE,
    FEEDBACK_RE,
    PROMPT_PREFIX,
    RESPONSE_PREFIXES,
    SEPARATOR_RE,
    STATUS_BAR_RE,
    STATUS_LINE_RE,
    THINKING_PREFIXES,
    TOOL_RESULT_PREFIX,
)
from agp.plugins.claude_code._normalize import is_noise_line

# Timing markers: ✻ Brewed for 2m 4s, ✻ Churned for 55s, etc.
# These are chrome (turn duration), not response content.
# Pattern: {thinking_prefix} {PastTenseVerb} for {duration}
_TIMING_RE = re.compile(r"for\s+\d+[hms\d\s]+$", re.IGNORECASE)


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


def _is_timing_line(line: str) -> bool:
    """Return True for turn-duration markers like '✻ Brewed for 2m 4s'.

    The TUI renders timing as: {spinner_prefix} {PastTenseVerb} for {duration}
    where duration matches patterns like "2m 4s", "55s", "1m 41s".
    """
    s = line.strip()
    for prefix in THINKING_PREFIXES:
        if s.startswith(prefix):
            if _TIMING_RE.search(s):
                return True
    return False


def _is_tool_result(line: str) -> bool:
    s = line.strip()
    return s.startswith(TOOL_RESULT_PREFIX)


def _strip_footer(lines: list[str]) -> list[str]:
    """Remove the TUI footer block from the end of the screen.

    The footer is the 4-line block at the bottom::

        ────────────────────  (separator, may include branch name)
        ❯ [user input]       (idle prompt with in-progress text)
        ────────────────────  (separator)
        sTAT | ...           (custom status line)
        ⏵⏵ ...               (built-in status bar)

    Text on the ❯ line inside this block is in-progress user input,
    not a submitted prompt — it must not be parsed as a turn.
    """
    if not lines:
        return lines

    # Scan backwards to find the footer boundary.
    # The footer ends with status lines, then separator, then ❯, then separator.
    i = len(lines) - 1

    # Skip trailing blank lines
    while i >= 0 and not lines[i].strip():
        i -= 1

    # Skip status bar lines (⏵⏵ and sTAT)
    while i >= 0:
        s = lines[i].strip()
        if STATUS_BAR_RE.match(s) or STATUS_LINE_RE.match(s):
            i -= 1
        else:
            break

    # Expect a separator
    if i >= 0 and SEPARATOR_RE.match(lines[i].strip()):
        i -= 1
    else:
        return lines  # No footer found

    # Expect ❯ line (the idle prompt)
    if i >= 0 and lines[i].strip().startswith(PROMPT_PREFIX):
        prompt_line_idx = i
        i -= 1
    else:
        return lines  # No footer found

    # Expect another separator above the prompt
    if i >= 0 and SEPARATOR_RE.match(lines[i].strip()):
        # Found the full footer block — strip everything from this separator down
        return lines[:i]
    else:
        return lines  # No footer found


def parse_turns(text: str) -> list[Turn]:
    """Parse Claude Code TUI output into structured prompt/response turns.

    Handles:
    - Prompt lines (❯ prefix)
    - Response lines (⏺/● prefix)
    - Tool result lines (⎿ prefix) as part of the response
    - Indented continuation lines
    - Conversation compaction (✻ marker) — only parses post-compaction
    - Footer block (separator/prompt/separator/status) — stripped before parsing
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()

    # Strip the TUI footer so in-progress prompt text isn't parsed as a turn
    lines = _strip_footer(lines)

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
            content = _response_content(line)
            # Filter feedback survey lines that appear with ⏺ prefix
            if FEEDBACK_RE.search(content):
                in_response = False
                continue
            if current is None:
                # Orphan response: prompt scrolled off the visible screen.
                # Create an implicit turn so the response is captured.
                current = Turn()
                turns.append(current)
            current.response_lines.append(content)
            in_response = True
            continue

        # Tool result (part of response)
        if _is_tool_result(line):
            if current is None:
                current = Turn()
                turns.append(current)
            content = s[len(TOOL_RESULT_PREFIX):].strip()
            current.response_lines.append(f"  {TOOL_RESULT_PREFIX} {content}")
            in_response = True
            continue

        # Skip timing markers (✻ Brewed for 2m 4s)
        if _is_timing_line(line):
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

    # Filter out empty turns (bare ❯ with no prompt text and no response)
    turns = [t for t in turns if t.prompt or t.response_lines]

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
