"""Screen normalization, noise filtering, and tail extraction."""

from __future__ import annotations

from agp.plugins.claude_code._markers import (
    BOX_CHARS,
    FEEDBACK_RE,
    NOISE_PREFIXES,
    SEPARATOR_RE,
    STATUS_BAR_RE,
    STATUS_CONTINUATION_RE,
    STATUS_LINE_RE,
    STATUS_TAIL_RE,
)


def normalize_screen(raw: str) -> str:
    """Normalize line endings and strip trailing blanks.

    Call once per poll cycle before passing to classify/parse functions.
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def is_noise_line(line: str) -> bool:
    """Return True for TUI chrome lines (not content).

    Blank lines are NOT noise — they are preserved as paragraph breaks.
    """
    s = line.strip()
    if not s:
        return False
    if SEPARATOR_RE.match(s):
        return True
    if STATUS_BAR_RE.match(s):
        return True
    if STATUS_LINE_RE.match(s):
        return True
    if FEEDBACK_RE.search(s):
        return True
    # Feedback survey options: "1: Bad    2: Fine   3: Good   0: Dismiss"
    if s.startswith("1:") and "dismiss" in s.lower():
        return True
    for prefix in NOISE_PREFIXES:
        if s.startswith(prefix):
            return True
    if all(ch in BOX_CHARS or ch in " \t" for ch in s):
        return True
    return False


def is_status_continuation(line: str) -> bool:
    """Return True for wrapped status-bar continuation lines."""
    s = line.strip()
    if not s:
        return False
    if STATUS_TAIL_RE.match(s):
        return True
    return bool(STATUS_CONTINUATION_RE.match(s))


def screen_tail(text: str, n: int = 10) -> str:
    """Return the last N meaningful non-chrome lines of the visible screen.

    Excludes status bar lines (⏵⏵), separators (────), and status
    continuations so that token count changes don't affect stability.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    filtered = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if STATUS_BAR_RE.match(s):
            continue
        if STATUS_LINE_RE.match(s):
            continue
        if is_status_continuation(ln):
            continue
        if SEPARATOR_RE.match(s):
            continue
        filtered.append(ln.rstrip())
    return "\n".join(filtered[-n:])
