"""Claude Code TUI character constants and regexes.

Single source of truth for all Unicode markers, separators, status bar
patterns, and noise prefixes used to interpret the Claude Code TUI screen.

Derived from the Claude Code source (claude-code-main):
- src/constants/figures.ts — response markers, icons, blockquote
- src/components/Spinner/utils.ts — spinner frames
- src/components/PromptInput/PromptInputModeIndicator.tsx — prompt prefix
- src/components/design-system/Divider.tsx — separator rendering
- src/utils/permissions/PermissionMode.ts — permission mode symbols
- src/components/StatusLine.tsx — status line
- src/components/messages/AssistantThinkingMessage.tsx — thinking ∴
- src/components/MessageResponse.tsx — tool result ⎿
"""

from __future__ import annotations

import re

# ── Prompt and response markers ──────────────────────────────────────

PROMPT_PREFIX = "\u276f"  # ❯ (figures.pointer)

RESPONSE_PREFIXES = (
    "\u23fa",  # ⏺  (macOS/Darwin)
    "\u25cf",  # ●  (Windows/Linux fallback, also reduced-motion spinner)
)

# ── Separators and box drawing ───────────────────────────────────────
# Divider.tsx renders ─ (U+2500) or ━ (U+2501 HEAVY) across terminal width.

SEPARATOR_RE = re.compile(r"^[\u2500\u2501]{4,}.*$")

NOISE_PREFIXES = (
    "\u256d", "\u2570",               # welcome box  ╭ ╰
    "\u25b6\u25b6", "\u25b6",        # status bar   ▶▶ ▶ (PLAY_ICON)
    "\u23f5\u23f5", "\u23f5",        # status bar   ⏵⏵ ⏵ (legacy)
    "\u23f8",                         # status bar   ⏸ (PAUSE_ICON / plan mode)
    # ▎ (U+258E) NOT filtered — appears in LLM blockquote output
    "\u203b",                         # away summary ※
    "\u26d1",                         # autocompact  ⛝ (pit symbol)
)

# ── Status bar patterns ──────────────────────────────────────────────

STATUS_BAR_RE = re.compile(
    r"^\s*(?:"
    r"\u25b6\u25b6"    # ▶▶ (current PLAY_ICON doubled)
    r"|\u25b6"         # ▶  (single play)
    r"|\u23f5\u23f5"   # ⏵⏵ (legacy)
    r"|\u23f8"         # ⏸  (pause / plan mode)
    r")\s+"
)

STATUS_TAIL_RE = re.compile(r"^\s*\d[\d,]*\s+tokens?\s*$", re.IGNORECASE)

STATUS_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"\u00b7\s+.*"
    r"|.*(?:"
    r"esc to interrupt"
    r"|shift\+tab to cycle"
    r"|bypass permissions on"
    r"|accept edits on"
    r"|don't ask on"
    r"|auto accept on"
    r"|plan mode on"
    r"|claude install"
    r"|native installer"
    r"|switched from npm"
    r"|update available"
    r").*"
    r")$",
    re.IGNORECASE,
)

STATUS_LINE_RE = re.compile(r"^\s*sTAT\s*\|")

# ── Working indicators ──────────────────────────────────────────────
# Spinner characters used by Claude Code during processing.
# These are only noise when followed by a known status verb (Working, Thinking, etc.)
# NOT when they appear as bare content prefixes.

SPINNER_CHARS = (
    "\u2234",  # ∴ (thinking block)
    "\u2722",  # ✢
    "\u2733",  # ✳
    "\u2736",  # ✶
    "\u273b",  # ✻
    "\u273d",  # ✽
    "*",       # platform fallback (Ghostty, Linux)
)

_WORKING_VERBS = ("Working", "Thinking", "Running", "Waiting", "Reconnecting", "Exploring")
