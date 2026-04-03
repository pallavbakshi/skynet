"""Screen state classification for Claude Code TUI.

Pure functions that take normalized screen text and answer
"what state is the TUI in?" Each function answers exactly
one question — no side effects, no accumulation.
"""

from __future__ import annotations

from agp.plugins.claude_code._markers import (
    BOX_CHARS,
    PROMPT_PREFIX,
    RESPONSE_PREFIXES,
    SEPARATOR_RE,
    STATUS_BAR_RE,
    STATUS_LINE_RE,
    THINKING_PREFIXES,
    THINKING_VERBS,
)
from agp.plugins.claude_code._normalize import is_noise_line, is_status_continuation
from agp.plugins.claude_code._parse import parse_turns


def _is_response_line(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in RESPONSE_PREFIXES)


def _has_tui_indicator(text: str) -> bool:
    """Return True if the screen contains Claude Code TUI markers."""
    for line in text.splitlines():
        s = line.strip()
        if SEPARATOR_RE.match(s):
            return True
        if STATUS_BAR_RE.match(s):
            return True
        if any(s.startswith(c) for c in ("\u256d", "\u2570", "\u2502")):
            # Welcome box border
            return True
    return False


def is_ready(text: str) -> bool:
    """Return True when Claude Code shows an idle prompt.

    Requires ❯ plus at least one TUI indicator (separator, welcome box,
    or status bar) to avoid false positives from shell prompts.
    """
    if PROMPT_PREFIX not in text:
        return False
    return _has_tui_indicator(text)


def ends_with_prompt(text: str) -> bool:
    """Return True when the last meaningful line is an idle ❯ prompt.

    Looks past status bars, separators, and noise to find the true
    last content line.
    """
    lines = text.splitlines()
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if STATUS_BAR_RE.match(s):
            continue
        if STATUS_LINE_RE.match(s):
            continue
        if is_status_continuation(line):
            continue
        if SEPARATOR_RE.match(s):
            continue
        if is_noise_line(line):
            continue
        # This is the last meaningful line
        return s.startswith(PROMPT_PREFIX)
    return False


def is_working(text: str) -> bool:
    """Return True when Claude Code shows active thinking/processing.

    Scans only the bottom ~5 meaningful lines to avoid false positives
    from thinking text that appears in a response.
    """
    lines = text.splitlines()

    # Collect last 5 meaningful lines from the bottom
    meaningful: list[str] = []
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if STATUS_BAR_RE.match(s) or SEPARATOR_RE.match(s):
            continue
        if is_noise_line(line):
            continue
        meaningful.append(s)
        if len(meaningful) >= 5:
            break

    if not meaningful:
        return False

    # Check for thinking indicators first — they take precedence
    # over prompt+response detection because the thinking indicator
    # is the most recent activity.
    for s in meaningful:
        # Thinking prefix + ellipsis or verb
        for prefix in THINKING_PREFIXES:
            if s.startswith(prefix):
                lower = s.lower()
                if "\u2026" in s or "..." in s:  # ellipsis
                    return True
                if any(verb in lower for verb in THINKING_VERBS):
                    return True

        # Agent/Explore/Tool indicators
        if s.startswith("Running") and "\u2026" in s:
            return True

    return False


def is_shell_returned(text: str) -> bool:
    """Return True when Claude Code exited and a shell prompt is visible.

    Focuses on the last 3 non-empty lines to detect shell prompts.
    Only checks those bottom lines for TUI indicators — scrollback
    above may still contain TUI output from the previous session.
    """
    lines = text.strip().splitlines()
    if not lines:
        return False

    # Get last 3 non-empty lines (the visible bottom of the screen)
    tail: list[str] = []
    for ln in reversed(lines):
        s = ln.strip()
        if s:
            tail.append(s)
            if len(tail) >= 3:
                break
    tail.reverse()

    if not tail:
        return False

    # If the bottom lines contain active TUI indicators, TUI is still running
    for ln in tail:
        if STATUS_BAR_RE.match(ln):
            return False
        if STATUS_LINE_RE.match(ln):
            return False
        if _is_response_line(ln):
            return False

    last = tail[-1]

    # Common shell prompt endings
    if last.endswith("$") or last.endswith("%") or last.endswith("#"):
        return True

    # Bare ❯ at the bottom without TUI chrome nearby = zsh prompt after exit
    if last.startswith(PROMPT_PREFIX):
        # Check if any of the bottom lines have TUI-specific content
        has_tui_below = any(
            SEPARATOR_RE.match(ln) or STATUS_BAR_RE.match(ln)
            for ln in tail
        )
        if not has_tui_below:
            return True

    return False


def is_completed_turn(
    text: str,
    *,
    baseline_answered_turns: int,
    baseline_last_response: str | None,
) -> bool:
    """Return True when a new Claude response is complete and prompt returned.

    Compares current turns against a baseline to detect when a new
    response has appeared since the last check.
    """
    if not ends_with_prompt(text):
        return False

    turns = parse_turns(text)
    answered = [t for t in turns if t.response]
    if not answered:
        return False

    # More answered turns than baseline → new turn completed
    if len(answered) > baseline_answered_turns:
        return True

    # Same count but different response content → response updated
    if (
        len(answered) == baseline_answered_turns
        and baseline_last_response is not None
        and answered[-1].response != baseline_last_response
    ):
        return True

    return False
