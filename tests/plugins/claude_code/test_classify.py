"""State classification tests against the corpus."""

from __future__ import annotations

import pytest

from agp.plugins.claude_code._classify import (
    ends_with_prompt,
    is_completed_turn,
    is_ready,
    is_shell_returned,
    is_working,
)
from agp.plugins.claude_code._gates import GateKind, classify_gate
from agp.plugins.claude_code._normalize import normalize_screen
from agp.runtime._output import _strip_ansi
from tests.plugins.claude_code.conftest import corpus_files


def _prepare(raw: str) -> str:
    """Strip ANSI and normalize — mirrors what the adapter does each poll."""
    return normalize_screen(_strip_ansi(raw))


# ── Ready state ───────────────────────────────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("ready"))
def test_ready_screens_are_ready(capture, corpus):
    text = _prepare(corpus(capture))
    assert is_ready(text), f"{capture} should be classified as ready"


@pytest.mark.parametrize("capture", corpus_files("working"))
def test_working_screens_are_not_ready_idle(capture, corpus):
    """Working screens may still have a prompt visible but are not idle."""
    text = _prepare(corpus(capture))
    assert is_working(text), f"{capture} should be classified as working"


@pytest.mark.parametrize("capture", corpus_files("shell"))
def test_shell_screens_detected(capture, corpus):
    text = _prepare(corpus(capture))
    assert is_shell_returned(text), f"{capture} should detect shell returned"


@pytest.mark.parametrize("capture", corpus_files("shell"))
def test_shell_screens_not_ready(capture, corpus):
    text = _prepare(corpus(capture))
    assert not is_ready(text) or is_shell_returned(text)


# ── Ends with prompt ─────────────────────────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("ready"))
def test_ready_screens_end_with_prompt(capture, corpus):
    text = _prepare(corpus(capture))
    assert ends_with_prompt(text), f"{capture} should end with prompt"


# ── Working state ─────────────────────────────────────────────────────


def test_thinking_screen_is_working(corpus):
    text = _prepare(corpus("working/thinking.txt"))
    assert is_working(text)
    assert not is_shell_returned(text)


# ── Completed turn ────────────────────────────────────────────────────


def test_post_response_is_completed_turn(corpus):
    text = _prepare(corpus("ready/post_response.txt"))
    assert is_completed_turn(text, baseline_answered_turns=0, baseline_last_response=None)


def test_fresh_launch_is_not_completed_turn(corpus):
    text = _prepare(corpus("ready/fresh_launch.txt"))
    assert not is_completed_turn(text, baseline_answered_turns=0, baseline_last_response=None)


# ── Gate detection ────────────────────────────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("gates"))
def test_gate_screens_detected(capture, corpus_with_expected):
    text, expected = corpus_with_expected(capture)
    text = _prepare(text)
    expected_kind = expected.get("classify", {}).get("gate_kind", "AUTO")
    assert classify_gate(text) == GateKind(expected_kind)


def test_ready_screens_no_gate(corpus):
    text = _prepare(corpus("ready/fresh_launch.txt"))
    assert classify_gate(text) == GateKind.NONE


def test_permission_prompt_is_auto_gate(corpus):
    text = _prepare(corpus("gates/permission_prompt.txt"))
    assert classify_gate(text) == GateKind.AUTO


# ── Edge cases ────────────────────────────────────────────────────────


def test_empty_pane(corpus):
    text = _prepare(corpus("edge/empty_pane.txt"))
    assert not is_ready(text)
    assert not is_working(text)
    assert not is_shell_returned(text)
    assert classify_gate(text) == GateKind.NONE
