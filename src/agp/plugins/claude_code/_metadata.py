"""TUI metadata extraction — model, effort, session, tokens, permissions.

The sTAT line is produced by a custom status line command configured in
~/.claude/settings.json::statusLine.  Claude Code sends a JSON payload
to the command's stdin on every state change::

    {
      "model": {"display_name": "Opus 4.6 (1M context)"},
      "session_id": "uuid",
      "context_window": {"used_percentage": 3.5}
    }

The command (statusline-command.sh) formats this as pipe-delimited text::

    sTAT | {model.display_name} | {effortLevel} | {session_id} | {pct}%

The effort level is read from settings.json (not the JSON payload).
The percentage is ANSI-colored (green <50%, yellow 50-79%, red 80%+)
and stripped by _strip_ansi before reaching this parser.

Token count and permission mode come from the ⏵⏵ status bar line,
which is a Claude Code built-in (not the custom status line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agp.plugins.claude_code._markers import STATUS_BAR_RE, STATUS_LINE_RE


@dataclass
class TuiMetadata:
    """Structured metadata extracted from Claude Code status lines."""

    # From sTAT line (custom status line command)
    model: str | None = None            # e.g. "Opus 4.6 (1M context)"
    effort: str | None = None           # e.g. "medium"
    session_id: str | None = None       # UUID
    context_usage_pct: int | None = None  # 0-100

    # From ⏵⏵ status bar (Claude Code built-in)
    token_count: int | None = None      # e.g. 16339
    permission_mode: str | None = None  # e.g. "bypass permissions", "accept edits", "don't ask"

    @property
    def has_content(self) -> bool:
        return any(v is not None for v in (
            self.model, self.effort, self.session_id,
            self.context_usage_pct, self.token_count, self.permission_mode,
        ))


_TOKEN_RE = re.compile(r"(\d[\d,]*)\s+tokens?", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d+)%")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PERM_MODE_RE = re.compile(
    r"(bypass permissions|accept edits|don't ask|auto accept|auto mode|plan mode)\s+on",
    re.IGNORECASE,
)


def extract_metadata(text: str) -> TuiMetadata:
    """Extract metadata from the Claude Code TUI status lines.

    Parses the sTAT custom status line and the ⏵⏵ built-in status bar.
    Expects ANSI-stripped text (call _strip_ansi first).
    """
    meta = TuiMetadata()

    for line in text.splitlines():
        s = line.strip()

        # sTAT | model | effort | session_id [| pct%]
        if STATUS_LINE_RE.match(s):
            _parse_stat_line(s, meta)
            continue

        # ⏵⏵ {mode} on (shift+tab to cycle)  [N tokens]
        if STATUS_BAR_RE.match(s):
            _parse_status_bar(s, meta)
            continue

    # Token count can also appear right-aligned on non-status lines
    if meta.token_count is None:
        for line in text.splitlines():
            m = _TOKEN_RE.search(line)
            if m:
                meta.token_count = int(m.group(1).replace(",", ""))
                break

    return meta


def _parse_stat_line(line: str, meta: TuiMetadata) -> None:
    """Parse the custom sTAT status line.

    Format: ``sTAT | Opus 4.6 (1M context) | medium | {uuid} [| N%]``
    The separators are dim ``|`` characters rendered by the status line
    command.  After ANSI stripping they are plain ``|``.
    """
    parts = [p.strip() for p in line.split("|")]
    # parts[0] = "sTAT", parts[1] = model, parts[2] = effort, parts[3] = session_id
    if len(parts) < 4:
        return

    meta.model = parts[1] or None
    meta.effort = parts[2] or None

    uuid_match = _UUID_RE.search(parts[3])
    if uuid_match:
        meta.session_id = uuid_match.group(0)

    # parts[4] = context usage percentage (optional, absent at 0%)
    if len(parts) >= 5:
        pct_match = _PCT_RE.search(parts[4])
        if pct_match:
            meta.context_usage_pct = int(pct_match.group(1))

    # Token count is right-aligned on the same line (after whitespace)
    token_match = _TOKEN_RE.search(line)
    if token_match:
        meta.token_count = int(token_match.group(1).replace(",", ""))


def _parse_status_bar(line: str, meta: TuiMetadata) -> None:
    """Parse the ⏵⏵ built-in status bar.

    Format: ``⏵⏵ bypass permissions on (shift+tab to cycle)  16340 tokens``
    """
    perm_match = _PERM_MODE_RE.search(line)
    if perm_match:
        meta.permission_mode = perm_match.group(1).lower()

    token_match = _TOKEN_RE.search(line)
    if token_match and meta.token_count is None:
        meta.token_count = int(token_match.group(1).replace(",", ""))
