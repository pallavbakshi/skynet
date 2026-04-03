"""Gate prompt detection, classification, and auto-response."""

from __future__ import annotations

from enum import Enum


class GateKind(Enum):
    """Classification of a gate prompt screen."""

    NONE = "NONE"
    AUTO = "AUTO"    # Can be auto-dismissed with Enter or a numbered choice
    FATAL = "FATAL"  # Requires user action (e.g., OAuth login)


# ── Pattern tables ────────────────────────────────────────────────────
# Update these when Claude Code adds new interactive prompts.

AUTO_GATE_PATTERNS = (
    # First-run setup
    "choose the text style",
    "syntax highlighting",
    # Login success / continuation
    "login successful",
    "press enter to continue",
    "security notes",
    # Feedback survey
    "how is claude doing",
    # Bypass permissions
    "bypass permissions mode",
    "accept all responsibility",
    # Trust prompts
    "yes, i trust this folder",
    "i trust this folder",
    "i trust this project",
    "trust the contents",
    "quick safety check",
    # Permission prompts
    "allow tool",
    "allow bash",
    "allow read",
    "allow edit",
    "allow write",
    "(y/n)",
    # Upgrade notifications
    "claude install",
    "native installer",
    "switched from npm",
    "update available",
)

FATAL_GATE_PATTERNS = (
    "select login method",
    "paste code here",
    "browser didn't open",
    "oauth error",
)

# Map from pattern → key to send (empty string = Enter to accept default)
GATE_CHOICES: dict[str, str] = {
    "choose the text style": "",
    "syntax highlighting": "",
    "login successful": "",
    "security notes": "",
    "press enter to continue": "",
    "how is claude doing": "0",
    "bypass permissions mode": "2",
    "accept all responsibility": "2",
    "yes, i trust this folder": "1",
    "i trust this folder": "1",
    "i trust this project": "1",
    "trust the contents": "1",
    "quick safety check": "1",
    "claude install": "",
    "native installer": "",
    "switched from npm": "",
    "update available": "",
}


def classify_gate(text: str) -> GateKind:
    """Classify whether the screen shows a gate prompt.

    Returns GateKind.FATAL for prompts requiring user action,
    GateKind.AUTO for auto-dismissable prompts, or GateKind.NONE.
    """
    lower = text.lower()
    if any(pat in lower for pat in FATAL_GATE_PATTERNS):
        return GateKind.FATAL
    if any(pat in lower for pat in AUTO_GATE_PATTERNS):
        return GateKind.AUTO
    return GateKind.NONE


def gate_response(text: str) -> str:
    """Return the key to send for an auto-dismissable gate prompt.

    Returns empty string (Enter) for unrecognized auto gates.
    """
    lower = text.lower()
    for phrase, choice in GATE_CHOICES.items():
        if phrase in lower:
            return choice
    return ""
