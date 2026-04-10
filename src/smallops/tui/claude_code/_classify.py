"""Screen state classification for Claude Code TUI.

Pure functions that inspect normalized screen text and answer
"what state is the TUI in?"
"""

from __future__ import annotations

from smallops._types import IdleReason
from smallops.tui.claude_code._markers import (
    PROMPT_PREFIX,
    RESPONSE_PREFIXES,
    SEPARATOR_RE,
    STATUS_BAR_RE,
    STATUS_LINE_RE,
)
from smallops.tui.claude_code._parse import is_noise, is_status


def classify_idle(screen: str) -> IdleReason:
    """Given a static screen, classify why it's idle."""
    from smallops.tui.claude_code._gates import (
        AUTO_GATE_PATTERNS,
        FATAL_GATE_PATTERNS,
    )

    # If the screen shows a completed turn (prompt → response → prompt),
    # gate-like text in the response body is NOT a real gate.
    if has_completed_turn(screen):
        return IdleReason.READY

    lower = screen.lower()

    # Fatal gates first
    if any(pat in lower for pat in FATAL_GATE_PATTERNS):
        return IdleReason.ERROR

    # Auto-dismissable gates
    if any(pat in lower for pat in AUTO_GATE_PATTERNS):
        return IdleReason.GATE

    # Ready prompt (❯ + TUI indicator)
    if has_prompt(screen) and has_tui_indicator(screen):
        return IdleReason.READY

    # Shell returned (TUI exited)
    if is_shell_returned(screen):
        return IdleReason.ERROR

    # TUI indicators but no prompt — could be unrecognized gate
    if has_tui_indicator(screen):
        return IdleReason.GATE

    # Nothing recognizable
    return IdleReason.GATE


def has_completed_turn(screen: str) -> bool:
    """Return True when the screen shows a completed turn.

    A completed turn means: at least one response block (⏺/●) AND
    the screen ends with an idle prompt (❯).  Gate-like text in the
    response body should not trigger gate dismissal.
    """
    if not ends_with_prompt(screen):
        return False
    for line in screen.splitlines():
        s = line.strip()
        if any(s.startswith(p) for p in RESPONSE_PREFIXES):
            return True
    return False


def ends_with_prompt(text: str) -> bool:
    """Return True when the last meaningful line is an idle ❯ prompt.

    Looks past status bars, separators, and noise.
    """
    for line in reversed(text.splitlines()):
        s = line.strip()
        if not s:
            continue
        if is_status(s):
            continue
        if is_noise(s):
            continue
        return s.startswith(PROMPT_PREFIX)
    return False


def has_prompt(text: str) -> bool:
    """Check if ❯ prompt is visible anywhere on screen."""
    return PROMPT_PREFIX in text


def has_tui_indicator(text: str) -> bool:
    """Check for Claude Code TUI markers (separator, status bar, response, welcome box)."""
    for line in text.splitlines():
        s = line.strip()
        if SEPARATOR_RE.match(s):
            return True
        if STATUS_BAR_RE.match(s):
            return True
        if any(s.startswith(p) for p in RESPONSE_PREFIXES):
            return True
        if any(s.startswith(c) for c in ("\u256d", "\u2570")):
            return True
    return False


def is_shell_returned(text: str) -> bool:
    """Check if the TUI exited and shell prompt is visible."""
    lines = text.strip().splitlines()
    if not lines:
        return False

    tail = [ln.strip() for ln in lines[-3:] if ln.strip()]
    if not tail:
        return False

    # If bottom lines have TUI indicators, TUI is still running
    for ln in tail:
        if STATUS_BAR_RE.match(ln):
            return False
        if STATUS_LINE_RE.match(ln):
            return False
        if any(ln.startswith(p) for p in RESPONSE_PREFIXES):
            return False

    last = tail[-1]
    if last.endswith("$") or last.endswith("%") or last.endswith("#"):
        return True
    if last == PROMPT_PREFIX or last == PROMPT_PREFIX + " ":
        has_tui = any(SEPARATOR_RE.match(ln) or STATUS_BAR_RE.match(ln) for ln in tail)
        return not has_tui

    return False
