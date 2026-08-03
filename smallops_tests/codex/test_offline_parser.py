"""Offline parser property tests for Codex screen shapes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from smallops import BlockKind, CodexTui, IdleReason, normalize_screen, strip_ansi
from smallops._types import Status

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
    missing = {"ready", "turns", "working"} - categories
    assert not missing, f"corpus missing required categories: {sorted(missing)}"
    return cases


def read_screen(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return normalize_screen(strip_ansi(text))


@pytest.mark.offline
@pytest.mark.parametrize("case", discover_captures(), ids=lambda case: case.id)
def test_codex_parser_accepts_corpus_inputs(case: CaptureCase) -> None:
    tui = CodexTui()
    screen = read_screen(case.path)

    reason = tui.classify_idle(screen)
    parsed = tui.parse_blocks(screen, "")
    status = tui.parse_status(screen)

    assert reason in IdleReason
    assert parsed.raw == screen
    assert isinstance(status, Status)
    assert isinstance(status.model, str)
    assert isinstance(status.effort, str)
    assert isinstance(status.tokens, int)
    assert status.tokens >= 0
    assert isinstance(status.context_pct, int)
    assert status.context_pct >= 0


@pytest.mark.offline
@pytest.mark.parametrize("case", discover_captures(), ids=lambda case: case.id)
def test_codex_category_level_parser_properties(case: CaptureCase) -> None:
    tui = CodexTui()
    screen = read_screen(case.path)
    reason = tui.classify_idle(screen)

    if case.category in {"ready", "turns"}:
        assert reason == IdleReason.READY
    elif case.category in {"working", "gates", "captures", "edge"}:
        assert reason in IdleReason
    else:
        raise AssertionError(f"unexpected corpus category: {case.category}")


@pytest.mark.offline
@pytest.mark.parametrize(
    "screen",
    [
        """
╭────────────────────────────────────────╮
│ >_ Codex                               │
╰────────────────────────────────────────╯

›
gpt-5.3-codex low · 12.3k used · 19% used
""",
        """
› Read the file /tmp/smallops/task-example.md

• PONG

›
gpt-5.3-codex low · 12.3k used · 19% used
""",
        """
› Read README.md

• Ran command
  └ cat README.md
  └ SKYNET-CODEX-CANARY

• SKYNET-CODEX-CANARY

›
gpt-5.3-codex low · 12.3k used · 19% used
""",
    ],
)
def test_codex_parser_accepts_screen_shapes(screen: str) -> None:
    tui = CodexTui()

    reason = tui.classify_idle(screen)
    parsed = tui.parse_blocks(screen, "")
    status = tui.parse_status(screen)

    assert reason in IdleReason
    assert parsed.raw == screen
    assert isinstance(status, Status)
    assert isinstance(status.model, str)
    assert isinstance(status.effort, str)
    assert isinstance(status.tokens, int)
    assert status.tokens >= 0
    assert isinstance(status.context_pct, int)
    assert status.context_pct >= 0


@pytest.mark.offline
def test_codex_completed_turn_parses_text_and_tool_use() -> None:
    tui = CodexTui()
    marker = "Read the file /tmp/smallops/task-example.md"
    screen = f"""
› {marker}

• Ran command
  └ cat README.md
  └ SKYNET-CODEX-CANARY

• SKYNET-CODEX-CANARY

›
gpt-5.3-codex low · 12.3k used · 19% used
"""

    parsed = tui.parse_blocks(screen, marker)

    assert "SKYNET-CODEX-CANARY" in parsed.text
    assert parsed.tool_uses
    assert parsed.tool_uses[0].kind == BlockKind.TOOL_USE
    assert parsed.tool_uses[0].tool == "Ran"
    assert parsed.status.model == "gpt-5.3-codex"
    assert parsed.status.effort == "low"
    assert parsed.status.tokens == 12300
    assert parsed.status.context_pct == 19


@pytest.mark.offline
def test_codex_placeholder_prompt_after_status_is_ready() -> None:
    tui = CodexTui()
    screen = """
› Read the file /tmp/smallops/task-example.md

• Explored
  └ Read task-example.md

• CODEX-DOCKER-PONG

────────────────────────────────────────────────────────────────────────────────

› Find and fix a bug in @filename

  minimax/minimax-m2.7 low · 100% left · /tmp/work
"""

    assert tui.classify_idle(screen) == IdleReason.READY


@pytest.mark.offline
def test_codex_plain_bullet_reply_after_returned_prompt_is_ready() -> None:
    tui = CodexTui()
    screen = """
› SMALLOPS-CODEX-TASK-abc123 Reply with exactly: CODEX-PONG


• CODEX-PONG


› Find and fix a bug in @filename

  openai/gpt-5.6-luna-pro low · Context 10% used · 258K window · 17K used
"""

    assert tui.classify_idle(screen) == IdleReason.READY


@pytest.mark.offline
@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("Welcome to Codex\nSign in with ChatGPT\nSign in with device code\nProvide your own API key", "1"),
        ("Do you trust the contents of this directory?\n1. Yes, continue", "1"),
        ("Would you like to run the following command?\n1. Yes, proceed", "y"),
        ("Press enter to continue", ""),
    ],
)
def test_codex_gate_patterns_are_auto_dismissible(
    monkeypatch: pytest.MonkeyPatch,
    screen: str,
    expected: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tui = CodexTui()

    assert tui.classify_idle(screen) == IdleReason.GATE
    assert tui.gate_response(screen) == expected


@pytest.mark.offline
def test_codex_stale_trust_prompt_with_queued_input_is_not_gate() -> None:
    tui = CodexTui()
    screen = """
Welcome to Codex, OpenAI's command-line coding agent

Do you trust the contents of this directory?
› 1. Yes, continue
  2. No, quit

• Queued follow-up inputs
  ↳ 1
  ↳ Read the file /tmp/smallops/task-example.md.

› Summarize recent commits

  minimax/minimax-m2.7 low · /tmp/work
"""

    assert tui.gate_response(screen) is None
    assert tui.classify_idle(screen) == IdleReason.READY


@pytest.mark.offline
def test_codex_parser_skips_queued_followup_inputs() -> None:
    tui = CodexTui()
    marker = "Read the file /tmp/smallops/task-example.md"
    screen = f"""
› {marker}

• Queued follow-up inputs
  ↳ {marker}
    shift + ← edit last queued message

• CODEX-DOCKER-PONG

› Summarize recent commits

  minimax/minimax-m2.7 low · /tmp/work
"""

    parsed = tui.parse_blocks(screen, marker)

    assert parsed.text == "CODEX-DOCKER-PONG"
    assert marker not in parsed.text


@pytest.mark.offline
def test_codex_fatal_gate_is_not_dismissed() -> None:
    tui = CodexTui()
    screen = "You've hit your usage limit. Upgrade to Pro."

    assert tui.classify_idle(screen) == IdleReason.ERROR
    assert tui.is_fatal_gate(screen)
    assert tui.gate_response(screen) is None


@pytest.mark.offline
def test_codex_launch_command_trusts_exact_cwd() -> None:
    command = CodexTui(
        flags="-p openrouter --dangerously-bypass-approvals-and-sandbox"
    ).launch_command(cwd="/tmp/smallops work")

    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "-C '/tmp/smallops work'" in command
    assert "-c 'projects.\"/tmp/smallops work\".trust_level=\"trusted\"'" in command


@pytest.mark.offline
def test_codex_launch_prompt_command_quotes_task_text() -> None:
    command = CodexTui(flags="-p openrouter").launch_prompt_command(
        "first line\nsecond line",
        cwd="/tmp/smallops work",
    )

    assert command.startswith("codex -p openrouter ")
    assert "-C '/tmp/smallops work'" in command
    assert "'first line\nsecond line'" in command


@pytest.mark.offline
def test_codex_launch_prompt_command_can_wrap_script_pty() -> None:
    command = CodexTui(flags="-p openrouter", script_pty=True).launch_prompt_command(
        "Reply OK",
        cwd="/tmp/smallops work",
    )

    assert command.startswith("script -qfec ")
    assert "codex -p openrouter" in command
    assert "/tmp/smallops-codex.typescript" in command


@pytest.mark.offline
def test_codex_format_send_uses_direct_task_marker() -> None:
    marker, send_text, path = CodexTui().format_send("Reply OK")

    assert marker.startswith("SMALLOPS-CODEX-TASK-")
    assert marker in send_text
    assert "Read the file" not in send_text
    assert send_text.endswith(" Reply OK")
    assert path is None
