"""Codex TUI character constants, noise patterns, and terminal markers.

Single source of truth for all Unicode markers and pattern tables
used to interpret the Codex TUI screen.

Derived from the Codex Rust source (codex-rs/tui/src):
- bottom_pane/chat_composer.rs — input prompt ›
- bottom_pane/custom_prompt_view.rs — user gutter ▌
- exec_cell/render.rs — spinner •/◦
- status_indicator_widget.rs — working status line
- bottom_pane/approval_overlay.rs — approval modals
- onboarding/ — onboarding flows
"""

from __future__ import annotations

# ── Prompt and input markers ─────────────────────────────────────────

PROMPT_MARKER = "\u203a"         # › — input prompt AND selection cursor
USER_GUTTER = "\u2590"          # ▌ — user message gutter prefix

# ── Spinner markers ──────────────────────────────────────────────────

SPINNER_CHAR = "\u2022"         # • — active spinner (also bullet in output)
SPINNER_CHAR_DIM = "\u25e6"     # ◦ — spinner fallback (no 16M color)

# ── Box drawing ──────────────────────────────────────────────────────

TUI_BOX_CHARS = frozenset("\u2502\u2514")  # │└

# ── Noise patterns ───────────────────────────────────────────────────

NOISE_PREFIXES = (
    "Token usage:", "To continue this session", "Tip:",
    "\u26a0", "\u2728",  # ⚠ ✨
    "See https://", "See full release", ">_",
    "model:", "directory:", "Press enter to",
    "Approaching rate", "Switch to gpt-", "Keep current model",
    "Hide future rate", "Optimized for codex",
    "Working (",  # transient "Working (3s · esc to interrupt)" status
    "Use /skills",
    "Press enter to confirm",
    "? for shortcuts",
)

# ── Shell prompt markers ─────────────────────────────────────────────

SHELL_MARKERS = frozenset({"\u276f", "$", "%", "#"})

# ── TUI detection hints ─────────────────────────────────────────────

TUI_CONTENT_HINTS = (
    "codex",
    "esc to interrupt",
    "esc to cancel",
    "esc to go back",
    "press enter to confirm",
    "shift+tab to cycle",
    "again to quit",
)

# ── Placeholder prompts ─────────────────────────────────────────────

PLACEHOLDER_PROMPTS = (
    "Find and fix a bug in", "Write tests for", "Use /skills",
    "Fix the bug in", "Explain how", "Explain this codebase",
    "Add a feature", "Improve documentation in",
)
