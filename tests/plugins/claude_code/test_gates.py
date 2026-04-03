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


def test_trust_folder_response(corpus):
    text = normalize_screen(corpus("gates/trust_folder.txt"))
    assert gate_response(text) == "1"


def test_bypass_permissions_response(corpus):
    text = normalize_screen(corpus("gates/bypass_permissions.txt"))
    assert gate_response(text) == "2"


def test_feedback_survey_response(corpus):
    text = normalize_screen(corpus("gates/feedback_survey.txt"))
    assert gate_response(text) == "0"


def test_login_is_fatal(corpus):
    text = normalize_screen(corpus("gates/login_required.txt"))
    assert classify_gate(text) == GateKind.FATAL


@pytest.mark.parametrize("capture", corpus_files("ready"))
def test_ready_screens_no_gate(capture, corpus):
    text = _prepare(corpus(capture))
    assert classify_gate(text) == GateKind.NONE
