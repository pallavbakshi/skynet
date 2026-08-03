"""Response parsing, noise filtering, and status extraction for Claude Code TUI.

Two-phase approach:
  1. Capture — everything after the marker to end of screen (unfiltered).
  2. Parse   — structure captured content into typed blocks (text, tool_use,
               prompt, noise) so consumers can pick what they need.
"""

from __future__ import annotations

import re

from smallops._types import Block, BlockKind, ParsedResponse, Status
from smallops.tui.claude_code._markers import (
    _WORKING_VERBS,
    NOISE_PREFIXES,
    PROMPT_PREFIX,
    RESPONSE_PREFIXES,
    SEPARATOR_RE,
    SPINNER_CHARS,
    STATUS_BAR_RE,
    STATUS_CONTINUATION_RE,
    STATUS_LINE_RE,
    STATUS_TAIL_RE,
)

# ── Tool-use detection ──────────────────────────────────────────────
# Claude Code renders tool calls as: ⏺ ToolName(arguments)
# Tool results appear as:            ⎿  result text

_TOOL_USE_RE = re.compile(r"^[A-Z]\w*\(")
_TOOL_RESULT_PREFIX = "\u23bf"  # ⎿
_COLLAPSED_TOOL_SUMMARY_RE = re.compile(
    r"^Thought for \S+(?:,\s+[^()]*)?\s+\(ctrl\+o to expand\)$",
    re.IGNORECASE,
)
_COLLAPSED_TOOL_VERBS = (
    ("read ", "Read"),
    ("searched ", "Search"),
    ("search ", "Search"),
    ("ran ", "Bash"),
    ("edited ", "Edit"),
    ("updated ", "Edit"),
    ("wrote ", "Write"),
    ("created ", "Write"),
)


def _is_tool_use_header(content: str) -> bool:
    """True if content (after stripping ⏺) looks like a tool invocation."""
    return bool(_TOOL_USE_RE.match(content))


def _tool_name(content: str) -> str:
    """Extract tool name from 'ToolName(args...)'."""
    paren = content.find("(")
    return content[:paren] if paren > 0 else content.split()[0] if content else ""


def _collapsed_tool_name(content: str) -> str:
    """Extract an approximate tool name from Claude's collapsed summary line."""
    if not _COLLAPSED_TOOL_SUMMARY_RE.match(content):
        return ""
    lower = content.lower()
    for needle, name in _COLLAPSED_TOOL_VERBS:
        if needle in lower:
            return name
    return "ToolSummary"


# ── Capture + Parse ─────────────────────────────────────────────────

def capture(text: str, marker: str) -> str:
    """Phase 1: extract everything after the marker. No filtering."""
    if not marker:
        return text

    anchor = None
    idx = text.find(marker)
    if idx >= 0:
        anchor = idx + len(marker)
    else:
        path_match = re.search(r"Read the file\s+(\S+)", marker)
        if path_match:
            path = path_match.group(1).rstrip(".")
            idx = text.find(path)
            if idx >= 0:
                anchor = idx + len(path)

    if anchor is None:
        return text

    structural_idx = _first_structural_response_index(text, anchor)
    if structural_idx is not None:
        return text[structural_idx:]
    return text[anchor:]


def _first_structural_response_index(text: str, start: int) -> int | None:
    pos = start
    for line in text[start:].splitlines(keepends=True):
        s = line.strip()
        if any(s.startswith(p) for p in RESPONSE_PREFIXES) or _collapsed_tool_name(s):
            return pos
        pos += len(line)
    return None


def parse(captured: str) -> ParsedResponse:
    """Phase 2: structure captured content into typed blocks."""
    lines = captured.splitlines()
    blocks: list[Block] = []
    current_kind: BlockKind | None = None
    current_lines: list[str] = []
    current_tool = ""
    # The marker sits inside the ❯ prompt line, so captured starts mid-line.
    # Skip everything until the first structural marker (⏺, ❯, or end of prompt).
    started = False

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

        # Skip preamble — the marker is mid-line in the ❯ prompt,
        # so captured text starts with the tail of that prompt line.
        # Skip until first ⏺/● (response) or second ❯ (next prompt).
        if not started:
            if any(s.startswith(p) for p in RESPONSE_PREFIXES):
                started = True  # fall through to handle this line
            elif _collapsed_tool_name(s) or s.startswith(PROMPT_PREFIX):
                started = True  # fall through
            else:
                continue

        # Prompt line: ❯
        if s.startswith(PROMPT_PREFIX):
            _flush()
            current_kind = BlockKind.PROMPT
            current_lines = [s]
            _flush()
            continue

        # Response marker: ⏺/●
        if any(s.startswith(p) for p in RESPONSE_PREFIXES):
            _flush()
            content = s[1:].strip()
            if _is_tool_use_header(content):
                current_kind = BlockKind.TOOL_USE
                current_tool = _tool_name(content)
                current_lines = [content]
            else:
                current_kind = BlockKind.TEXT
                current_lines = [content] if content else []
            continue

        collapsed_tool = _collapsed_tool_name(s)
        if collapsed_tool:
            _flush()
            current_kind = BlockKind.TOOL_USE
            current_tool = collapsed_tool
            current_lines = [s]
            continue

        # Status lines (sTAT, status bar, token count) → STATUS block
        if is_status(s):
            if current_kind != BlockKind.STATUS:
                _flush()
                current_kind = BlockKind.STATUS
                current_lines = [line.rstrip()]
            else:
                current_lines.append(line.rstrip())
            continue

        # Noise (separators, working indicators) — skip entirely
        if is_noise(s):
            continue

        # Empty lines — preserve within text blocks
        if not s:
            if current_kind == BlockKind.TEXT:
                current_lines.append("")
            continue

        # Continuation of current block
        if current_kind is not None:
            current_lines.append(line.rstrip())
        # Content before any ⏺ marker — treat as text
        elif s:
            current_kind = BlockKind.TEXT
            current_lines = [line.rstrip()]

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


# ── Status line parsing ───────────────────────────────────────────────
# Format: sTAT | model | effort | session_id | HH:MM:SS | pct%    N tokens

_STAT_RE = re.compile(r"^\s*sTAT\s*\|")
_TOKENS_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM]?)\s+tokens?", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d+)%")
_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}")
_MODEL_SUFFIX_RE = re.compile(r"\[[^\]]+\]$")
_CARD_MODEL_RE = re.compile(r"\b(?:Opus|Sonnet|Haiku)\s+\d+(?:\.\d+)?\b", re.IGNORECASE)


def parse_status(screen: str) -> Status:
    """Extract structured status from the sTAT line and surrounding status lines.

    The sTAT line has: model | effort | session_id | time | pct%
    The token count may be on a separate ⏵⏵ continuation line.
    """
    clean_screen = re.sub(r"\x1b\[[0-9;]*m", "", screen)

    for line in clean_screen.splitlines():
        if not _STAT_RE.match(line.strip()):
            continue

        parts = [p.strip() for p in line.split("|")]

        model = parts[1] if len(parts) > 1 else ""
        effort = parts[2] if len(parts) > 2 else ""
        session_id = parts[3] if len(parts) > 3 else ""

        last_completed = ""
        raw_time_part = parts[4] if len(parts) > 4 else ""
        time_match = _TIME_RE.search(raw_time_part)
        if time_match:
            last_completed = time_match.group(0)

        tail = "|".join(parts[5:]) if len(parts) > 5 else ""
        context_pct = 0
        pct_match = _PCT_RE.search(tail)
        if pct_match:
            context_pct = int(pct_match.group(1))

        # Token count — search the entire status block, not just the sTAT line
        tokens = _parse_tokens(clean_screen)

        return Status(
            model=model,
            effort=effort,
            session_id=session_id,
            tokens=tokens,
            context_pct=context_pct,
            last_completed=last_completed,
        )

    return Status(model=_parse_card_model(clean_screen), tokens=_parse_tokens(clean_screen))


def _parse_tokens(text: str) -> int:
    tok_match = _TOKENS_RE.search(text)
    if not tok_match:
        return 0
    value = float(tok_match.group(1).replace(",", ""))
    suffix = tok_match.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _parse_card_model(text: str) -> str:
    lines = text.splitlines()
    for line in lines:
        if "API Usage Billing" not in line or "·" not in line:
            continue
        for segment in line.split("│"):
            if "API Usage Billing" not in segment or "·" not in segment:
                continue
            model = segment.split("·", 1)[0].strip()
            return _MODEL_SUFFIX_RE.sub("", model).strip()
    for idx, line in enumerate(lines):
        if "API Usage Billing" not in line:
            continue
        for candidate in reversed(lines[max(0, idx - 3):idx]):
            match = _CARD_MODEL_RE.search(candidate)
            if match:
                return match.group(0)
    return ""


def is_status(line: str) -> bool:
    """Return True if a line is a TUI status/meta line (sTAT, status bar, token count)."""
    s = line.strip()
    if not s:
        return False
    if STATUS_BAR_RE.match(s):
        return True
    if STATUS_LINE_RE.match(s):
        return True
    if STATUS_TAIL_RE.match(s):
        return True
    return bool(STATUS_CONTINUATION_RE.match(s))


def is_noise(line: str) -> bool:
    """Return True if a line is TUI chrome that should be discarded."""
    s = line.strip()
    if not s:
        return False
    if SEPARATOR_RE.match(s):
        return True
    for prefix in NOISE_PREFIXES:
        if s.startswith(prefix):
            return True
    # Feedback survey
    if "how is claude doing" in s.lower():
        return True
    if s.startswith("1:") and "dismiss" in s.lower():
        return True
    if _is_completed_timing_line(s):
        return True
    # Working indicator lines — require spinner char + known verb
    return bool(_is_working_line(s))


def _is_working_line(s: str) -> bool:
    """Match spinner lines like '∴ Thinking' or '* Working (3s)', not bare '* item'."""
    for char in SPINNER_CHARS:
        if not s.startswith(char):
            continue
        after = s[len(char):].strip()
        lower = after.lower()
        if "(" in after and ("· thinking" in lower or "esc to interrupt" in lower):
            return True
        for verb in _WORKING_VERBS:
            if after.startswith(verb):
                return True
    return False


def _is_completed_timing_line(s: str) -> bool:
    """Match post-response spinner summaries like '✻ <verb> for 7s'.

    Claude rotates this verb text frequently. The stable contract is the
    spinner prefix plus a completed duration, while active spinner lines carry
    parenthesized telemetry such as "(1s · ... · thinking)".
    """
    for char in SPINNER_CHARS:
        if not s.startswith(char):
            continue
        after = s[len(char):].strip()
        if "(" not in after and re.search(r"\bfor\s+\d+(?:\.\d+)?[smh]\b", after):
            return True
    return False
