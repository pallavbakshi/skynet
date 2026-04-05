"""Gate detection and response tests against the corpus."""

from __future__ import annotations

import pytest

from agp.plugins.claude_code._gates import GateKind, classify_gate, gate_response
from agp.plugins.claude_code._normalize import normalize_screen
from agp.runtime._output import _strip_ansi
from tests.plugins.claude_code.conftest import corpus_files


def _prepare(raw: str) -> str:
    return normalize_screen(_strip_ansi(raw))


@pytest.mark.parametrize("capture", corpus_files("gates"))
def test_gate_detected(capture, corpus_with_expected):
    text, expected = corpus_with_expected(capture)
    text = _prepare(text)
    kind = classify_gate(text)
    expected_kind = expected.get("classify", {}).get("gate_kind", "AUTO")
    assert kind == GateKind(expected_kind), f"{capture}: expected {expected_kind}, got {kind}"


@pytest.mark.parametrize("capture", corpus_files("gates"))
def test_gate_response_matches(capture, corpus_with_expected):
    text, expected = corpus_with_expected(capture)
    text = _prepare(text)
    if "gate_response" in expected:
        assert gate_response(text) == expected["gate_response"]


def test_permission_prompt_is_auto_gate(corpus):
    text = _prepare(corpus("gates/permission_prompt.txt"))
    assert classify_gate(text) == GateKind.AUTO


def test_claude_code_update_banner_is_auto_gate():
    text = _prepare("Claude Code update available")
    assert classify_gate(text) == GateKind.AUTO
    assert gate_response(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "update available",
        "A security update is available for package X",
    ],
)
def test_generic_update_available_text_is_not_a_gate(text):
    text = _prepare(text)
    assert classify_gate(text) == GateKind.NONE


def test_gate_patterns_in_response_body_ignored_when_prompt_visible():
    """Gate-like strings in agent output must not trigger classification
    when the idle ❯ prompt is visible — the TUI is past any real gate."""
    screen = _prepare(
        '⏺ The AUTO_GATE_PATTERNS are:\n'
        '  - "allow bash"\n'
        '  - "allow read"\n'
        '  - "(y/n)"\n'
        '  - "bypass permissions mode"\n'
        '────────────────────────────────\n'
        '❯ \n'
        '────────────────────────────────\n'
        '  sTAT | Opus | 1234 tokens\n'
        '  ⏵⏵ bypass permissions on\n'
    )
    assert classify_gate(screen) == GateKind.NONE


@pytest.mark.parametrize("capture", corpus_files("ready"))
def test_ready_screens_no_gate(capture, corpus):
    text = _prepare(corpus(capture))
    assert classify_gate(text) == GateKind.NONE
