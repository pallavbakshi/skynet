"""Response parsing and noise filtering for Codex TUI.

Two-phase approach (same as Claude Code):
  1. Capture — everything after the marker to end of screen (unfiltered).
  2. Parse   — structure captured content into typed blocks (text, tool_use,
               prompt, status, noise) so consumers can pick what they need.

Codex block structure (from the real TUI screen):
  • text line              — LLM prose (bullet prefix)
  • Ran command            — tool execution header (bullet prefix)
    └ output               — tool output (tree connector)
    … +N lines             — truncated output
  • Explored               — tool activity header (bullet prefix)
    └ description           — tool details
  ───────────              — separator between logical groups
  › prompt text            — input prompt
  status · status · status — status line at bottom
"""

from __future__ import annotations

import re

from smallops._types import Block, BlockKind, ParsedResponse, Status
from smallops.tui.codex._markers import (
    NOISE_PREFIXES,
    PLACEHOLDER_PROMPTS,
    PROMPT_MARKER,
    SPINNER_CHAR,
    SPINNER_CHAR_DIM,
)

# ── Tool-use detection ──────────────────────────────────────────────
# Codex renders tool calls as: • Ran command  /  • Explored  /  • Edited file

_TOOL_ACTIVITY_VERBS = (
    "Ran ", "Explored", "Exploring",
    "Added ", "Deleted ", "Edited ",
    "Called ", "Calling ",
    "Searched ", "Searching ",
    "Viewed Image", "Generated Image",
    "Proposed Plan", "Updated Plan",
    "Read ", "Wrote ",
)


def _is_tool_header(content: str) -> str:
    """If content (after stripping •) looks like a tool header, return the verb. Else ''."""
    for verb in _TOOL_ACTIVITY_VERBS:
        if content.startswith(verb) or content == verb.rstrip():
            return verb.rstrip()
    return ""


# ── Capture + Parse ─────────────────────────────────────────────────

def capture(text: str, marker: str) -> str:
    """Phase 1: extract everything after the marker. No filtering."""
    idx = text.find(marker)
    if idx >= 0:
        return text[idx + len(marker):]
    return text


def parse(captured: str) -> ParsedResponse:
    """Phase 2: structure captured content into typed blocks."""
    lines = captured.splitlines()
    blocks: list[Block] = []
    current_kind: BlockKind | None = None
    current_lines: list[str] = []
    current_tool = ""
    started = False
    skip_queued_inputs = False

    def _flush() -> None:
        nonlocal current_kind, current_lines, current_tool
        if current_kind is not None and current_lines:
            content = "\n".join(current_lines).strip("\n")
            if content:
                blocks.append(Block(kind=current_kind, content=content, tool=current_tool))
        current_kind = None
        current_lines = []
        current_tool = ""

    for line in lines:
        s = line.strip()

        if skip_queued_inputs:
            if s.startswith("\u21b3") or s.startswith("shift +"):
                continue
            skip_queued_inputs = False

        # Skip preamble — marker is mid-line in the › prompt
        if not started:
            if _is_bullet_line(s) or s.startswith(PROMPT_MARKER):
                started = True
            else:
                continue

        # Prompt line: ›
        if s.startswith(PROMPT_MARKER):
            _flush()
            if not is_placeholder(s):
                current_kind = BlockKind.PROMPT
                current_lines = [s]
                _flush()
            continue

        # Status line (·-separated at bottom)
        if is_status_line(s):
            if current_kind != BlockKind.STATUS:
                _flush()
                current_kind = BlockKind.STATUS
                current_lines = [line.rstrip()]
            else:
                current_lines.append(line.rstrip())
            continue

        # Separator lines (───)
        if _is_separator(s):
            continue

        # Noise (update banners, tips, etc.)
        if is_noise(s):
            continue

        # Working/spinner status lines
        if _is_working_line(s):
            continue

        # Bullet lines (• content) — the main Codex content marker
        if _is_bullet_line(s):
            content = _strip_bullet(s)
            if content == "Queued follow-up inputs":
                _flush()
                skip_queued_inputs = True
                continue

            # Check if it's a tool activity header
            verb = _is_tool_header(content)
            if verb:
                _flush()
                current_kind = BlockKind.TOOL_USE
                current_tool = verb
                current_lines = [content]
                continue

            # Otherwise it's LLM text
            _flush()
            current_kind = BlockKind.TEXT
            current_lines = [content]
            continue

        # Tree connector (└) and indented continuation — part of current tool block
        if (s.startswith("\u2514") or s) and current_kind == BlockKind.TOOL_USE:
            current_lines.append(line.rstrip())
            continue

        # Empty lines — preserve within text blocks
        if not s:
            if current_kind == BlockKind.TEXT:
                current_lines.append("")
            continue

        # Continuation of current block
        if current_kind is not None:
            current_lines.append(line.rstrip())

    _flush()

    # Parse status from the STATUS block if present
    status_content = "\n".join(
        b.content for b in blocks if b.kind == BlockKind.STATUS
    )
    status = parse_status(status_content) if status_content else Status()

    return ParsedResponse(raw=captured, blocks=tuple(blocks), status=status)


def parse_response(text: str, marker: str) -> str:
    """Convenience: capture + parse, return just the LLM text.

    This is the method that satisfies the Tui protocol.
    """
    raw = capture(text, marker)
    parsed = parse(raw)
    return parsed.text


# ── Line classification helpers ──────────────────────────────────────

def _is_bullet_line(s: str) -> bool:
    """True if line starts with • or ◦ (Codex content marker)."""
    return s.startswith((SPINNER_CHAR, SPINNER_CHAR_DIM))


def _strip_bullet(s: str) -> str:
    """Strip • / ◦ prefix and one space from a line."""
    for char in (SPINNER_CHAR, SPINNER_CHAR_DIM):
        if s.startswith(char):
            after = s[len(char):]
            after = after.removeprefix(" ")
            return after
    return s


def _is_separator(s: str) -> bool:
    """True for horizontal rule lines (───)."""
    return bool(s) and all(ch in "\u2500\u2501 \t" for ch in s) and any(ch in "\u2500\u2501" for ch in s)


def _is_working_line(s: str) -> bool:
    """Match spinner lines like '• Working (3s • esc to interrupt)', not bare content."""
    if "esc to interrupt" in s:
        return True
    for char in (SPINNER_CHAR, SPINNER_CHAR_DIM):
        if s.startswith(char) and "(" in s and any(
            s[len(char):].strip().startswith(verb)
            for verb in ("Working", "Thinking", "Running", "Waiting", "Reconnecting", "Exploring")
        ):
            return True
    return False


def is_noise(line: str) -> bool:
    """Return True if a line is TUI chrome that should be discarded."""
    s = line.strip()
    if not s:
        return False
    for prefix in NOISE_PREFIXES:
        if s.startswith(prefix):
            return True
    return False


def is_status_line(s: str) -> bool:
    """True for ·-separated status lines at the bottom of the screen."""
    if "\u00b7" not in s:
        return False
    parts = [p.strip() for p in s.split("\u00b7")]
    if len(parts) < 2:
        return False
    return any(_looks_like_status_part(p) for p in parts)


def is_placeholder(line: str) -> bool:
    """Return True if a prompt-marker line is a composer placeholder, not a real task."""
    s = line.strip()
    if not s.startswith(PROMPT_MARKER):
        return False
    content = s.removeprefix(PROMPT_MARKER).strip()
    return any(content.startswith(ph) for ph in PLACEHOLDER_PROMPTS)


# ── Status line parsing ───────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PCT_USED_RE = re.compile(r"^(\d+)%\s+used$")
_PCT_LEFT_RE = re.compile(r"^(\d+)%\s+left$")
_TOKENS_USED_RE = re.compile(r"^([\d,.]+[km]?)\s+used$", re.IGNORECASE)
_WINDOW_RE = re.compile(r"^([\d,.]+[km]?)\s+window$", re.IGNORECASE)
_EFFORT_WORDS = frozenset({"low", "medium", "high", "max"})


def parse_status(screen: str) -> Status:
    """Extract structured status from the Codex status line."""
    for line in screen.splitlines():
        s = line.strip()
        if "\u00b7" not in s:
            continue
        parts = [p.strip() for p in s.split("\u00b7")]
        if len(parts) < 2:
            continue
        if not any(_looks_like_status_part(p) for p in parts):
            continue
        return _parse_parts(parts)
    return Status()


def _looks_like_status_part(part: str) -> bool:
    """Quick check if a part looks like a known Codex status item."""
    if _UUID_RE.match(part):
        return True
    if _PCT_USED_RE.match(part) or _PCT_LEFT_RE.match(part):
        return True
    if _TOKENS_USED_RE.match(part):
        return True
    if _WINDOW_RE.match(part):
        return True
    words = part.split()
    if len(words) >= 2 and words[-1].lower() in _EFFORT_WORDS:
        return True
    return bool(len(words) >= 2 and words[-1].lower() == "fast" and len(words) >= 3 and words[-2].lower() in _EFFORT_WORDS)


def _parse_parts(parts: list[str]) -> Status:
    """Parse known segments from a list of · separated parts."""
    model = ""
    effort = ""
    session_id = ""
    tokens = 0
    context_pct = 0

    for part in parts:
        if _UUID_RE.match(part):
            session_id = part
            continue
        m = _PCT_USED_RE.match(part)
        if m:
            context_pct = int(m.group(1))
            continue
        m = _PCT_LEFT_RE.match(part)
        if m:
            context_pct = 100 - int(m.group(1))
            continue
        m = _TOKENS_USED_RE.match(part)
        if m:
            tokens = _parse_compact_tokens(m.group(1))
            continue
        if _WINDOW_RE.match(part):
            continue
        words = part.split()
        if len(words) >= 2 and words[-1].lower() in _EFFORT_WORDS:
            model = " ".join(words[:-1])
            effort = words[-1].lower()
            continue
        if len(words) >= 3 and words[-1].lower() == "fast" and words[-2].lower() in _EFFORT_WORDS:
            model = " ".join(words[:-2])
            effort = words[-2].lower()
            continue

    return Status(
        model=model,
        effort=effort,
        session_id=session_id,
        tokens=tokens,
        context_pct=context_pct,
    )


def _parse_compact_tokens(s: str) -> int:
    """Parse Codex compact token format: '12.3k' → 12300, '1.5m' → 1500000."""
    s = s.strip().lower().replace(",", "")
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    try:
        return int(s)
    except ValueError:
        return 0
