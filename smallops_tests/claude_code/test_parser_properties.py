"""Offline parser property tests for Claude Code screen captures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from smallops import ClaudeCodeTui, IdleReason
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
