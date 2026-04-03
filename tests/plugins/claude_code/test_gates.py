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


@pytest.mark.parametrize("capture", corpus_files("ready"))
def test_ready_screens_no_gate(capture, corpus):
    text = _prepare(corpus(capture))
    assert classify_gate(text) == GateKind.NONE
