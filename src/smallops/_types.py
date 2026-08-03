"""Core data types for smallops."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentState(Enum):
    WORKING = "working"
    IDLE = "idle"


class IdleReason(Enum):
    READY = "ready"  # prompt visible, agent waiting for input
    ERROR = "error"  # pane died, process exited, error visible
    GATE = "gate"    # permission/OAuth/trust prompt or unknown block


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Opaque handle to a terminal pane managed by a Mux."""

    id: str                                  # tmux session name or wezterm pane_id
    name: str                                # human label
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BlockKind(Enum):
    TEXT = "text"            # LLM response prose
    TOOL_USE = "tool_use"   # tool invocation + result
    PROMPT = "prompt"        # ❯ or › input line
    STATUS = "status"        # status bars, sTAT line, token counts
    NOISE = "noise"          # separators, working indicators


@dataclass(frozen=True, slots=True)
class Block:
    """A single parsed block from agent output."""

    kind: BlockKind
    content: str          # the block's text content
    tool: str = ""        # tool name if kind is TOOL_USE (e.g. "Read", "Bash")


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    """Structured parse of everything after the marker."""

    raw: str                                         # capture: everything after marker, unfiltered
    blocks: tuple[Block, ...] = ()                   # parse: structured blocks
    status: Status = field(default_factory=lambda: Status())  # parsed status metadata

    @property
    def text(self) -> str:
        """Just the LLM text blocks, joined."""
        parts = [b.content for b in self.blocks if b.kind == BlockKind.TEXT]
        return "\n\n".join(parts).strip("\n")

    @property
    def tool_uses(self) -> tuple[Block, ...]:
        """Just the tool-use blocks."""
        return tuple(b for b in self.blocks if b.kind == BlockKind.TOOL_USE)


@dataclass(frozen=True, slots=True)
class Response:
    """Result of a send() call."""

    text: str        # parsed LLM text (convenience — same as parsed.text)
    raw: str         # raw screen capture used for parsing
    elapsed: float   # seconds from send to completion
    marker: str      # the via-file reference string (doubles as output anchor)
    parsed: ParsedResponse | None = None  # structured blocks (None for legacy/codex)


@dataclass(frozen=True, slots=True)
class Status:
    """Parsed TUI status line fields."""

    model: str = ""           # e.g. "Opus 4.6 (1M context)"
    effort: str = ""          # e.g. "medium", "high"
    session_id: str = ""      # e.g. "b1272008-3723-4981-8c00-118057b24d08"
    tokens: int = 0           # e.g. 194137
    context_pct: int = 0      # e.g. 19
    last_completed: str = ""  # e.g. "23:14:57"


@dataclass(frozen=True, slots=True)
class Meta:
    """Snapshot of session state — returned by Session.meta()."""

    state: AgentState
    idle_reason: IdleReason | None  # only set when state is IDLE
    alive: bool
    uptime: float         # seconds since up()
    last_activity: float  # seconds since last screen change
    tui: str              # tui kind, e.g. "claude_code" or "codex"
    mux: str              # mux kind, e.g. "tmux" or "wezterm"
    pane_id: str          # underlying pane identifier
    status: Status = field(default_factory=Status)  # parsed from TUI status line


@dataclass(slots=True)
class Config:
    """Tunable parameters — sensible defaults for all."""

    poll_interval: float = 2.0        # seconds between screen polls
    idle_threshold: int = 3           # consecutive unchanged polls = "static"
    timeout: float = 300.0            # default send/wait timeout
    hard_ceiling: float = 3600.0      # absolute max wait (1 hour)
    via_file_dir: str = "/tmp/smallops"
    bootstrap_timeout: float = 60.0   # max seconds to wait for agent ready
    max_gate_dismissals: int = 10     # auto-dismiss limit per operation


# ── Exceptions ───────────────────────────────────────────────────────

class SmallopsError(Exception):
    """Base for all smallops errors."""


class BootstrapTimeout(SmallopsError):
    """Agent did not become ready within bootstrap_timeout."""


class SendTimeout(SmallopsError):
    """Agent did not complete within timeout."""


class PaneDied(SmallopsError):
    """Terminal pane disappeared during operation."""


class FatalGate(SmallopsError):
    """Gate prompt that cannot be auto-dismissed (e.g. OAuth login required)."""
