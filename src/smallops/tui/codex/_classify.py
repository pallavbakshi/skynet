"""Screen state classification for Codex TUI.

Pure functions that inspect normalized screen text and answer
"what state is the TUI in?"
"""

from __future__ import annotations

from smallops._types import IdleReason
from smallops.tui.codex._markers import (
    PROMPT_MARKER,
    SHELL_MARKERS,
    TUI_BOX_CHARS,
    TUI_CONTENT_HINTS,
)
from smallops.tui.codex._parse import (
    _is_working_line,
    is_noise,
    is_placeholder,
    is_status_line,
)


def classify_idle(screen: str) -> IdleReason:
    """Given a static screen, classify why it's idle."""
    from smallops.tui.codex._gates import (
        FATAL_GATE_PATTERNS,
        active_gate_text,
        has_active_gate,
        is_onboarding,
    )

    # If the screen shows a completed turn (prompt → response → prompt),
    # gate-like text in the response body is NOT a real gate.
    if has_completed_turn(screen):
        return IdleReason.READY

    lower = screen.lower()

    # Fatal gates
    if any(pat in lower for pat in FATAL_GATE_PATTERNS):
        return IdleReason.ERROR

    # Onboarding (needs auth — treated as gate, auto-dismissed)
    if is_onboarding(active_gate_text(screen).lower()):
        return IdleReason.GATE

    # Auto-dismissable gates
    if has_active_gate(screen):
        return IdleReason.GATE

    # Ready prompt (› in bottom 10 lines)
    if has_prompt(screen):
        return IdleReason.READY

    # Shell returned (CLI exited)
    if is_shell_returned(screen):
        return IdleReason.ERROR

    # TUI indicators but no prompt — probably a gate
    if has_tui_indicator(screen):
        return IdleReason.GATE

    return IdleReason.GATE


def has_completed_turn(screen: str) -> bool:
    """Return True when the screen shows a completed turn.

    Codex shows completed executions with ✓/✗ markers and the
    prompt (›) returns at the bottom.  If we see both output content
    and a prompt, it's a completed turn.
    """
    if not ends_with_prompt(screen):
        return False
    # Check for any content above the prompt (response, execution output, etc.)
    for line in screen.splitlines():
        s = line.strip()
        # ✓ or ✗ = completed execution
        if s.startswith(("✓", "✗")):
            return True
        # Simple text replies render as a plain bullet followed by the
        # returned composer prompt.
        if s.startswith(("•", "◦")) and not _is_working_line(s):
            return True
        # └ = tree connector (execution details)
        if s.startswith("\u2514"):
            return True
        # ▌ = user message gutter (prior turn)
        if s.startswith("\u2590"):
            return True
    return False


def ends_with_prompt(text: str) -> bool:
    """Return True when the last meaningful line is a › prompt."""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if not s:
            continue
        if is_noise(s):
            continue
        if is_placeholder(s):
            return True
        if is_status_line(s):
            continue
        if _is_separator(s):
            continue
        return s.startswith(PROMPT_MARKER)
    return False


def has_prompt(text: str) -> bool:
    """Check for › prompt in bottom 10 lines."""
    lines = text.strip().splitlines()
    tail = [ln.strip() for ln in lines[-10:] if ln.strip()]
    return any(ln.startswith(PROMPT_MARKER) for ln in tail)


def has_tui_indicator(text: str) -> bool:
    """Check for Codex TUI markers in the screen."""
    lines = text.strip().splitlines()
    tail = [ln.strip() for ln in lines[-10:] if ln.strip()]
    # Check for structural TUI characters
    for ln in tail:
        if any(ch in TUI_BOX_CHARS for ch in ln):
            return True
    # Check for content hints
    lower_tail = "\n".join(tail).lower()
    return any(hint in lower_tail for hint in TUI_CONTENT_HINTS)


def is_shell_returned(text: str) -> bool:
    """Check if CLI exited and shell prompt is visible."""
    lines = text.strip().splitlines()
    tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
    if not tail:
        return False

    has_tui = any(PROMPT_MARKER in ln for ln in tail)
    has_shell = any(ln[0] in SHELL_MARKERS for ln in tail if ln)

    if not has_shell or has_tui:
        return False

    # Check for TUI structure chars
    for ln in tail:
        if any(ch in TUI_BOX_CHARS for ch in ln):
            return False
    lower_tail = "\n".join(tail).lower()
    return not any(hint in lower_tail for hint in TUI_CONTENT_HINTS)


def _is_separator(s: str) -> bool:
    return bool(s) and all(ch in "\u2500\u2501 \t" for ch in s) and any(ch in "\u2500\u2501" for ch in s)
