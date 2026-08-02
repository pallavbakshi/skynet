"""Gate prompt detection and auto-response for Codex TUI.

Derived from:
- codex-rs/tui/src/onboarding/ — onboarding flows
- codex-rs/tui/src/bottom_pane/approval_overlay.rs — approval modals
- codex-rs/tui/src/bottom_pane/list_selection_view.rs — selection lists
"""

from __future__ import annotations

import os

from smallops.tui.codex._classify import has_completed_turn

# ── Gate patterns ────────────────────────────────────────────────────
# Patterns found in screen text that indicate a gate prompt.
# Checked case-insensitively.

GATE_PATTERNS = (
    # Onboarding / auth
    "welcome to codex",
    "sign in with chatgpt",
    "sign in with device code",
    "provide your own api key",
    # Trust directory
    "do you trust the contents of this directory",
    "trust the contents",
    "do you trust",
    "yes, continue",
    # Approval modals
    "would you like to run the following command",
    "would you like to make the following edits",
    "would you like to grant these permissions",
    "yes, proceed",
    "yes, and don't ask again",
    "no, and tell codex what to do differently",
    # Confirmation prompts
    "press enter to continue",
    "press enter to confirm or esc",
    # Rate limits / model selection
    "approaching rate limits",
    "select model and effort",
    "introducing gpt-",
    "try new model",
    "use existing model",
    "switch to gpt-",
)

# Map from pattern → key to send.
# Empty string = Enter (accept default/first option).
# For numbered selections, send the number.
GATE_CHOICES: dict[str, str] = {
    # Trust prompt: "1. Yes, continue"
    "do you trust the contents of this directory": "1",
    "trust the contents": "1",
    "yes, continue": "",
    # Approval modals: "1. Yes, proceed (y)"
    "would you like to run the following command": "y",
    "would you like to make the following edits": "y",
    "would you like to grant these permissions": "y",
    "yes, proceed": "",
    # Rate limits
    "approaching rate limits": "3",  # "Keep current model (never show again)"
    "introducing gpt-": "2",         # Use existing model
    "try new model": "2",
    "switch to gpt-": "3",
    # General confirmation
    "press enter to continue": "",
    "press enter to confirm or esc": "",
}

FATAL_GATE_PATTERNS = (
    "you've hit your usage limit",
    "you have hit your usage limit",
    "hit your usage limit",
    "upgrade to pro",
    "purchase more credits",
)


def is_onboarding(lower: str) -> bool:
    """Check if the screen shows the Codex onboarding menu."""
    return (
        "welcome to codex" in lower
        and ("sign in with chatgpt" in lower or "sign in with device code" in lower)
        and "provide your own api key" in lower
    )


def preferred_auth_choice() -> str:
    """Choose the safest onboarding path for the current env."""
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"):
        return "3"  # provide own API key
    if os.environ.get("CODEX_PREFER_DEVICE_CODE", "").lower() in {"1", "true", "yes"}:
        return "2"
    return "1"


def gate_response(screen: str) -> str | None:
    """If screen shows a gate prompt, return the key to send.  None = no gate."""
    # If completed turn visible, gate-like text is response content
    if has_completed_turn(screen):
        return None

    lower = screen.lower()
    active = active_gate_text(screen).lower()

    # Fatal — don't auto-dismiss
    if any(pat in lower for pat in FATAL_GATE_PATTERNS):
        return None

    # Onboarding menu
    if is_onboarding(active):
        return preferred_auth_choice()

    for phrase, choice in GATE_CHOICES.items():
        if phrase in active:
            return choice

    if any(pat in active for pat in GATE_PATTERNS):
        return ""  # Enter

    return None


def has_active_gate(screen: str) -> bool:
    """Return True when gate text appears in the active bottom region."""
    active = active_gate_text(screen).lower()
    return is_onboarding(active) or any(pat in active for pat in GATE_PATTERNS)


def active_gate_text(screen: str) -> str:
    """Gate scanning window.

    Codex inline mode preserves old prompt text in tall panes. Once input has
    been queued, old trust/onboarding prompts can stay visible above the active
    composer and must not be dismissed again.
    """
    lower = screen.lower()
    if "queued follow-up inputs" in lower:
        return ""
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return "\n".join(lines[-35:])
