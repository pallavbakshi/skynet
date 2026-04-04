"""Regression tests that load capture sidecars and assert parser ground truth.

Each capture .txt in corpus/captures/ with a matching .expected.json sidecar
is tested for classify (is_ready, is_working, gate_kind, etc.) and parse
(turn_count, turn content, last_response_contains) correctness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agp.plugins.claude_code._classify import (
    ends_with_prompt,
    is_ready,
    is_shell_returned,
    is_working,
)
from agp.plugins.claude_code._gates import GateKind, classify_gate
from agp.plugins.claude_code._normalize import normalize_screen
from agp.plugins.claude_code._parse import extract_last_response, parse_turns
from agp.runtime._output import _strip_ansi

CORPUS_DIR = Path(__file__).parent / "corpus"
CAPTURES_DIR = CORPUS_DIR / "captures"


def _discover_sidecars() -> list[str]:
    """Find all capture .txt files that have an .expected.json sidecar."""
    results = []
    for p in CAPTURES_DIR.glob("*.expected.json"):
        base = p.name.removesuffix(".expected.json")
        if (CAPTURES_DIR / f"{base}.txt").exists():
            results.append(base)
    return sorted(results)


def _load(name: str) -> tuple[str, dict]:
    text = (CAPTURES_DIR / f"{name}.txt").read_text()
    expected = json.loads((CAPTURES_DIR / f"{name}.expected.json").read_text())
    return normalize_screen(_strip_ansi(text)), expected


SIDECARS = _discover_sidecars()


@pytest.mark.parametrize("capture", SIDECARS)
class TestCaptureRegression:
    """Regression suite driven by .expected.json sidecars."""

    def test_is_ready(self, capture: str) -> None:
        text, expected = _load(capture)
        assert is_ready(text) == expected["classify"]["is_ready"]

    def test_is_working(self, capture: str) -> None:
        text, expected = _load(capture)
        assert is_working(text) == expected["classify"]["is_working"]

    def test_ends_with_prompt(self, capture: str) -> None:
        text, expected = _load(capture)
        assert ends_with_prompt(text) == expected["classify"]["ends_with_prompt"]

    def test_is_shell_returned(self, capture: str) -> None:
        text, expected = _load(capture)
        assert is_shell_returned(text) == expected["classify"]["is_shell_returned"]

    def test_gate_kind(self, capture: str) -> None:
        text, expected = _load(capture)
        assert classify_gate(text) == GateKind(expected["classify"]["gate_kind"])

    def test_turn_count(self, capture: str) -> None:
        text, expected = _load(capture)
        if "turn_count" not in expected:
            pytest.skip("no turn_count in sidecar")
        turns = parse_turns(text)
        assert len(turns) == expected["turn_count"]

    def test_turn_content(self, capture: str) -> None:
        text, expected = _load(capture)
        if "turns" not in expected:
            pytest.skip("no turns in sidecar")
        turns = parse_turns(text)
        answered = [t for t in turns if t.response]
        for i, exp in enumerate(expected["turns"]):
            assert i < len(answered), (
                f"expected answered turn {i} but only got {len(answered)}"
            )
            turn = answered[i]
            if "prompt_contains" in exp:
                assert exp["prompt_contains"] in turn.prompt, (
                    f"turn {i} prompt missing {exp['prompt_contains']!r}"
                )
            if "response_contains" in exp:
                assert exp["response_contains"] in turn.response, (
                    f"turn {i} response missing {exp['response_contains']!r}"
                )

    def test_last_response(self, capture: str) -> None:
        text, expected = _load(capture)
        if "last_response_contains" not in expected:
            pytest.skip("no last_response_contains in sidecar")
        response = extract_last_response(text)
        assert expected["last_response_contains"] in response, (
            f"last response missing {expected['last_response_contains']!r}"
        )
