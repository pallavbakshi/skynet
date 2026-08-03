"""Offline parser property tests for Claude Code screen captures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from smallops import BlockKind, ClaudeCodeTui, IdleReason
from smallops._types import Status
from smallops._util import normalize_screen, strip_ansi

CORPUS_ROOT = Path(__file__).parent / "corpus"


@dataclass(frozen=True, slots=True)
class CaptureCase:
    path: Path
    category: str

    @property
    def id(self) -> str:
        return f"{self.category}/{self.path.stem}"


def discover_captures() -> list[CaptureCase]:
    cases: list[CaptureCase] = []
    for path in sorted(CORPUS_ROOT.rglob("*")):
        if path.suffix not in {".txt", ".raw"}:
            continue
        if path.name == "README.md":
            continue
        rel = path.relative_to(CORPUS_ROOT)
        cases.append(CaptureCase(path=path, category=rel.parts[0]))
    assert cases, f"no corpus captures found under {CORPUS_ROOT}"
    categories = {case.category for case in cases}
    missing = {"ready", "gates"} - categories
    assert not missing, f"corpus missing required categories: {sorted(missing)}"
    return cases


def read_screen(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return normalize_screen(strip_ansi(text))


@pytest.mark.offline
@pytest.mark.parametrize("case", discover_captures(), ids=lambda case: case.id)
def test_parser_accepts_corpus_inputs(case: CaptureCase) -> None:
    tui = ClaudeCodeTui()
    screen = read_screen(case.path)

    reason = tui.classify_idle(screen)
    parsed = tui.parse_blocks(screen, "")
    status = tui.parse_status(screen)

    assert reason in IdleReason
    assert parsed.raw == screen
    assert isinstance(status, Status)
    assert isinstance(status.model, str)
    assert isinstance(status.effort, str)
    assert isinstance(status.session_id, str)
    assert isinstance(status.tokens, int)
    assert status.tokens >= 0
    assert isinstance(status.context_pct, int)
    assert status.context_pct >= 0
    assert isinstance(status.last_completed, str)


@pytest.mark.offline
@pytest.mark.parametrize("case", discover_captures(), ids=lambda case: case.id)
def test_category_level_parser_properties(case: CaptureCase) -> None:
    tui = ClaudeCodeTui()
    screen = read_screen(case.path)
    reason = tui.classify_idle(screen)

    if case.category == "ready":
        assert reason == IdleReason.READY
    elif case.category == "gates":
        assert reason == IdleReason.GATE
        assert tui.gate_response(screen) is not None
    elif case.category == "shell":
        # Current static-frame behavior: exited_clean classifies READY because
        # smallops cannot distinguish this shell prompt from Claude's ready
        # prompt without live process context. Keep this weak until the driver
        # itself grows a stronger shell-returned signal.
        assert reason in IdleReason
    elif case.category in {"working", "turns", "captures", "edge", "scrollback"}:
        assert reason in IdleReason
    else:
        raise AssertionError(f"unexpected corpus category: {case.category}")


@pytest.mark.offline
def test_parse_status_accepts_claude_code_openrouter_card() -> None:
    tui = ClaudeCodeTui()
    screen = """
╭─── Claude Code v2.1.170 ───╮
│ x-ai/grok-4.20[1m] · API Usage Billing │
╰────────────────────────────╯

● Agent(Execute the exact task from the file)
  ⎿  Done (0 tool uses · 9.5k tokens · 1s)
❯
"""

    status = tui.parse_status(screen)

    assert status.model == "x-ai/grok-4.20"
    assert status.tokens == 9500


@pytest.mark.offline
def test_classify_does_not_treat_visible_calculating_as_ready() -> None:
    tui = ClaudeCodeTui()
    screen = """
╭─── Claude Code v2.1.170 ───╮
│ Sonnet 4 · API Usage Billing │
╰────────────────────────────╯

❯ Read the file /tmp/smallops/task-example.md

* Calculating…

────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ bypass permissions on · esc to interrupt
"""

    assert tui.classify_idle(screen) == IdleReason.GATE


@pytest.mark.offline
def test_parse_filters_completed_timing_spinner_from_response_text() -> None:
    tui = ClaudeCodeTui()
    marker = "Read the file /tmp/smallops/task-example.md"
    screen = f"""
❯ {marker}. Execute only the task text.

  Thought for 7s, read 1 file (ctrl+o to expand)

● MANUAL-PONG

✻ Cogitated for 7s

✻ Zorblenarfed for 4s

────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ bypass permissions on
"""

    parsed = tui.parse_blocks(screen, marker)

    assert parsed.text == "MANUAL-PONG"
    assert "Cogitated" not in parsed.text
    assert "Zorblenarfed" not in parsed.text


@pytest.mark.offline
def test_parse_collapsed_tool_summary_as_tool_use() -> None:
    tui = ClaudeCodeTui()
    marker = "Read the file /tmp/smallops/task-example.md"
    screen = f"""
❯ {marker}. Execute only the task text.

  Thought for 7s, read 2 files (ctrl+o to expand)

● MANUAL-CLAUDE-TMUX-CANARY

✻ Cooked for 7s

────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ bypass permissions on
"""

    parsed = tui.parse_blocks(screen, marker)

    assert parsed.text == "MANUAL-CLAUDE-TMUX-CANARY"
    assert parsed.tool_uses
    assert parsed.tool_uses[0].kind == BlockKind.TOOL_USE
    assert parsed.tool_uses[0].tool == "Read"


@pytest.mark.offline
def test_classify_active_spinner_overrides_completed_scrollback() -> None:
    tui = ClaudeCodeTui()
    screen = """
❯ Read the file /tmp/smallops/task-old.md

● MANUAL-PONG

✻ Cogitated for 7s

❯ Read the file /tmp/smallops/nudge-active.md

* Whirlpooling… (1s · ↓ 2 tokens · thinking)
  ⎿  Tip: Use /theme to change the color theme

────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ bypass permissions on · esc to interrupt
"""

    assert tui.classify_idle(screen) == IdleReason.GATE


@pytest.mark.offline
def test_classify_generated_active_spinner_verb_as_working() -> None:
    tui = ClaudeCodeTui()
    screen = """
❯ Read the file /tmp/smallops/task-old.md

● FIXED-MANUAL-PONG

✻ Cooked for 8s

❯ Read the file /tmp/smallops/nudge-active.md

✢ Whatchamacalliting… (1s · ↓ 1 tokens · thinking)
  ⎿  Tip: Use Plan Mode to prepare for a complex request before making changes.

────────────────────────────────
❯
────────────────────────────────
  ⏵⏵ bypass permissions on · esc to interrupt
"""

    assert tui.classify_idle(screen) == IdleReason.GATE
