"""Single source of truth for Claude Code TUI character constants and regexes.

Update this file when Claude Code ships UI changes that affect
glyphs, status bar format, or prompt markers.
"""

from __future__ import annotations

import re

# ── Prompt and response markers ───────────────────────────────────────

PROMPT_PREFIX = "\u276f"  # ❯

RESPONSE_PREFIXES = (
    "\u23fa",  # ⏺  (older versions)
    "\u25cf",  # ●  (newer versions)
)

TOOL_RESULT_PREFIX = "\u23bf"  # ⎿

# ── Separators and box drawing ────────────────────────────────────────

SEPARATOR_RE = re.compile(r"^\u2500{4,}$")  # ──── (4+ horizontal lines)

BOX_CHARS = frozenset(
    "\u2500\u2502\u256d\u256e\u256f\u2570"
    "\u2514\u250c\u2510\u2518\u2524\u251c"
    "\u252c\u2534\u253c\u2501\u2503"
)

WELCOME_START = "\u256d"  # ╭
WELCOME_END = "\u2570"    # ╰
WELCOME_SIDE = "\u2502"   # │

# ── Status bar ────────────────────────────────────────────────────────

STATUS_BAR_RE = re.compile(r"^\s*\u23f5\u23f5\s+")  # ⏵⏵

STATUS_TAIL_RE = re.compile(
    r"^\s*\d[\d,]*\s+tokens?\s*$", re.IGNORECASE
)

STATUS_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"\u00b7\s+.*"  # · (middle dot) continuation
    r"|.*(?:"
    r"esc to interrupt"
    r"|shift\+tab to cycle"
    r"|bypass permissions on"
    r"|don't ask on"
    r"|claude install"
    r"|native installer"
    r"|switched from npm"
    r"|update available"
    r").*"
    r")$",
    re.IGNORECASE,
)

# ── Thinking / working indicators ─────────────────────────────────────

THINKING_PREFIXES = (
    "\u2234",  # ∴
    "\u2733",  # ✳
    "\u2736",  # ✶
    "\u273b",  # ✻
    "\u273d",  # ✽
    "\u00b7",  # · (middle dot — used for "Contemplating…", "Beaming…", etc.)
)

THINKING_VERBS = (
    "thinking", "working", "analyzing", "planning",
    "swooping", "cogitating", "bloviating", "germinating",
    "ruminating", "pondering", "reflecting", "processing",
    "reasoning", "considering", "contemplating", "beaming",
    "simmering", "brewing", "churning",
)

# ── Compaction ────────────────────────────────────────────────────────

COMPACTION_RE = re.compile(r"^\u273b\s+(Conversation compacted|Churned)")

# ── Feedback survey ───────────────────────────────────────────────────

FEEDBACK_RE = re.compile(r"how is claude doing", re.IGNORECASE)

# ── Noise prefixes (TUI chrome, not content) ──────────────────────────

NOISE_PREFIXES = (
    WELCOME_START,   # ╭ welcome box top
    WELCOME_END,     # ╰ welcome box bottom
    WELCOME_SIDE,    # │ welcome box side
    "\u23f5\u23f5",  # ⏵⏵ status bar
)

# Additional status line patterns (new in Claude Code v2.1+)
STATUS_LINE_RE = re.compile(
    r"^\s*sTAT\s*\|",  # sTAT | Opus 4.6 ... style status line
)

# ── Shell prompt markers (post-exit detection) ────────────────────────

SHELL_PROMPT_CHARS = ("$", "%", "#", "❯")
