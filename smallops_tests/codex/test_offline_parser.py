"""Offline parser property tests for Codex screen shapes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from smallops import (
    BlockKind,
    ClaudeCodeTui,
    CodexTui,
    IdleReason,
    normalize_screen,
    strip_ansi,
)
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
def test_codex_uses_via_file_delivery_like_claude_code() -> None:
    """Codex ships no ``format_send`` override, so ``Session.send`` falls back to
    ``write_via_file`` — the same temp-file delivery Claude Code uses. This keeps
    large/multiline prompts out of the terminal paste path (parity with Claude Code)."""
    # Neither Tui overrides prompt delivery → both route through write_via_file.
    assert not hasattr(CodexTui(), "format_send")
    assert not hasattr(ClaudeCodeTui(), "format_send")


@pytest.mark.offline
def test_codex_parser_extracts_response_after_via_file_reference_marker() -> None:
    """Codex now delivers prompts via the temp-file reference (parity with Claude
    Code). That reference is far longer than the old SMALLOPS-CODEX-TASK marker, so
    prove the parser still locates it verbatim in codex's echoed › line: a preceding
    turn's output must be EXCLUDED, which only happens if ``capture()`` finds the marker."""
    tui = CodexTui()
    marker = (
        "Read the file /tmp/smallops/task-example.md. Execute only the task text "
        "between BEGIN TASK and END TASK exactly; do not summarize or restate."
    )
    screen = f"""
• PREVIOUS-TURN-OUTPUT

› {marker}

• CODEX-PONG

›
gpt-5.3-codex low · 12.3k used · 19% used
"""
    parsed = tui.parse_blocks(screen, marker)

    assert parsed.text == "CODEX-PONG"
    assert "PREVIOUS-TURN-OUTPUT" not in parsed.text
    assert tui.classify_idle(screen) == IdleReason.READY


@pytest.mark.offline
def test_codex_parser_handles_wrapped_via_file_marker_via_path_fallback() -> None:
    """The via-file reference is long and can wrap in a real terminal, so the exact
    marker won't match. The parser must fall back to anchoring on the shorter file
    path (parity with claude_code's capture). Regression test for the wrapped-marker
    case surfaced in review."""
    tui = CodexTui()
    marker = (
        "Read the file /tmp/smallops/task-example.md. Execute only the task text "
        "between BEGIN TASK and END TASK exactly; do not summarize or restate."
    )
    # Codex wrapped the echoed input across two lines → the full marker is NOT a
    # contiguous substring, but the path is.
    screen = """
• PREVIOUS-TURN-OUTPUT

› Read the file /tmp/smallops/task-example.md.
Execute only the task text between BEGIN TASK and END TASK exactly; do not summarize or restate.

• CODEX-PONG

› Find and fix a bug in @filename

  gpt-5.3-codex low · 100% left · /tmp/work
"""
    parsed = tui.parse_blocks(screen, marker)

    assert parsed.text == "CODEX-PONG"
    assert "PREVIOUS-TURN-OUTPUT" not in parsed.text


@pytest.mark.offline
def test_codex_session_send_uses_via_file_delivery(tmp_path) -> None:
    """Session.send with CodexTui writes the prompt to a temp file and sends only the
    'Read the file …' reference — never the full prompt — inheriting the generic
    via-file path through Session.send (parity with Claude Code)."""
    from smallops import Config, Session, SessionInfo

    class FakeMux:
        kind = "fake"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def session_exists(self, session) -> bool:
            return True

        def send_text(self, session, text: str, *, enter: bool = True) -> None:
            self.sent.append(text)

        def peek(self, session, n: int | None = None) -> str:
            last = self.sent[-1] if self.sent else ""
            return (
                f"› {last}\n\n• CODEX-RESPONSE\n\n"
                "› Find and fix a bug in @filename\n\n"
                "  gpt-5.3-codex low · 100% left · /tmp/work\n"
            )

        def interrupt(self, session) -> None:
            pass

        def destroy_session(self, session) -> None:
            pass

    mux = FakeMux()
    config = Config(poll_interval=0, idle_threshold=2, timeout=10, via_file_dir=str(tmp_path))
    s = Session(mux=mux, tui=CodexTui(), config=config)
    s._session = SessionInfo(id="fake", name="codex-dev", cwd=str(tmp_path))

    response = s.send("do the thing")

    # (1) exactly one task file written under via_file_dir
    files = sorted(tmp_path.glob("task-*.md"))
    assert len(files) == 1
    # (2) send_text received the reference, not the full prompt
    assert mux.sent and mux.sent[-1].startswith("Read the file")
    assert "do the thing" not in mux.sent[-1]
    assert str(files[0]) in mux.sent[-1]
    # (3) the prompt is wrapped in the file (BEGIN/END TASK), not typed inline
    body = files[0].read_text()
    assert "BEGIN TASK" in body and "do the thing" in body
    # (4) the response was parsed
    assert response.text == "CODEX-RESPONSE"
    # (5) cleanup on down() removes the task file
    s.down()
    assert not files[0].exists()


@pytest.mark.offline
def test_codex_session_send_file_arg_is_rewrapped_once(tmp_path) -> None:
    """Passing file= reads that file and rewraps its content into our own task file
    (a single wrap), then sends the reference — the original content is never typed."""
    from smallops import Config, Session, SessionInfo

    class FakeMux:
        kind = "fake"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def session_exists(self, session) -> bool:
            return True

        def send_text(self, session, text: str, *, enter: bool = True) -> None:
            self.sent.append(text)

        def peek(self, session, n: int | None = None) -> str:
            last = self.sent[-1] if self.sent else ""
            return (
                f"› {last}\n\n• OK\n\n"
                "› Find and fix a bug in @filename\n\n"
                "  gpt-5.3-codex low · 100% left · /tmp/work\n"
            )

        def interrupt(self, session) -> None:
            pass

        def destroy_session(self, session) -> None:
            pass

    src = tmp_path / "src.md"
    src.write_text("ORIGINAL-PROMPT-BODY")

    mux = FakeMux()
    config = Config(poll_interval=0, idle_threshold=2, timeout=10, via_file_dir=str(tmp_path))
    s = Session(mux=mux, tui=CodexTui(), config=config)
    s._session = SessionInfo(id="fake", name="codex-dev", cwd=str(tmp_path))

    s.send(file=str(src))

    task_files = sorted(tmp_path.glob("task-*.md"))
    assert len(task_files) == 1
    assert task_files[0] != src                                    # a NEW task file, not the source
    assert "ORIGINAL-PROMPT-BODY" in task_files[0].read_text()     # content rewrapped into it
    assert src.read_text() == "ORIGINAL-PROMPT-BODY"              # source untouched
    assert mux.sent and mux.sent[-1].startswith("Read the file")
    assert "ORIGINAL-PROMPT-BODY" not in mux.sent[-1]             # not typed inline
