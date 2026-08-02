"""Gate prompt detection and auto-response for Claude Code TUI.

Derived from:
- src/components/BypassPermissionsModeDialog.tsx — bypass mode warning
- src/components/TrustDialog/TrustDialog.tsx — workspace trust
- src/components/permissions/ — tool-specific permission requests
- src/components/ConsoleOAuthFlow.tsx — OAuth login
- src/components/FeedbackSurvey/ — post-compaction survey
"""

from __future__ import annotations

from smallops.tui.claude_code._classify import has_completed_turn

# ── Pattern tables ───────────────────────────────────────────────────

AUTO_GATE_PATTERNS = (
    # First-run setup
    "choose the text style", "syntax highlighting",
    # Login success / continuation
    "login successful", "press enter to continue", "security notes",
    # Feedback survey (triggered after compaction, 20% probability)
    "how is claude doing",
    # Bypass permissions mode dialog
    "bypass permissions mode", "accept all responsibility",
    "running in bypass permissions mode",
    # Trust dialog (workspace trust for hooks, MCP servers, API helpers)
    "yes, i trust this folder", "i trust this folder",
    "i trust this project", "trust the contents", "quick safety check",
    # Tool-specific permission requests
    "allow tool", "allow bash", "allow read", "allow edit", "allow write",
    "allow fetch", "allow powershell", "allow skill",
    "(y/n)",
    # Upgrade / install notifications
    "claude install", "native installer", "switched from npm", "claude code update",
    "update available",
)

FATAL_GATE_PATTERNS = (
    # OAuth flow — requires browser interaction
    "select login method",
    "paste code here",          # ConsoleOAuthFlow PASTE_HERE_MSG
    "browser didn't open",
    "oauth error",
)

# Map from pattern → key to send (empty string = Enter)
_DOWN = "\x1b[B"
GATE_CHOICES: dict[str, str] = {
    "choose the text style": "", "syntax highlighting": "",
    "login successful": "", "security notes": "",
    "press enter to continue": "",
    "how is claude doing": "0",       # dismiss survey
    # Claude Code 2.1.170 uses an interactive select where "No" is focused.
    # Move to "Yes, I accept" before pressing Enter.
    "bypass permissions mode": _DOWN, "accept all responsibility": _DOWN,
    "running in bypass permissions mode": _DOWN,
    # Trust dialog defaults to "Yes"; Enter confirms it.
    "yes, i trust this folder": "", "i trust this folder": "",
    "i trust this project": "", "trust the contents": "",
    "quick safety check": "",
    # Tool permissions: "y" = allow
    "allow tool": "y", "allow bash": "y", "allow read": "y",
    "allow edit": "y", "allow write": "y", "allow fetch": "y",
    "allow powershell": "y", "allow skill": "y",
    # Updates
    "claude install": "", "native installer": "",
    "switched from npm": "", "update available": "",
}


def gate_response(screen: str) -> str | None:
    """If screen shows a gate prompt, return the key to send.  None = no gate."""
    # If completed turn visible, gate-like text is response content
    if has_completed_turn(screen):
        return None

    lower = screen.lower()

    # Fatal gates — don't auto-dismiss
    if any(pat in lower for pat in FATAL_GATE_PATTERNS):
        return None

    for phrase, choice in GATE_CHOICES.items():
        if phrase in lower:
            return choice

    # Generic (y/n) prompt
    if "(y/n)" in lower:
        return "y"

    # Unknown auto-gate pattern
    if any(pat in lower for pat in AUTO_GATE_PATTERNS):
        return ""  # Enter

    return None
