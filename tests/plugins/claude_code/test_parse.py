"""Turn parsing tests against the corpus."""

from __future__ import annotations

import pytest

from agp.plugins.claude_code._parse import extract_last_response, parse_turns
from agp.plugins.claude_code._normalize import normalize_screen
from agp.runtime._output import _strip_ansi


def _prepare(raw: str) -> str:
    return normalize_screen(_strip_ansi(raw))
from tests.plugins.claude_code.conftest import corpus_files


# ── Turn count ────────────────────────────────────────────────────────


def test_fresh_launch_zero_turns(corpus):
    text = _prepare(corpus("ready/fresh_launch.txt"))
    turns = parse_turns(text)
    answered = [t for t in turns if t.response]
    assert len(answered) == 0


def test_single_short_one_turn(corpus):
    text = _prepare(corpus("turns/single_short.txt"))
    turns = parse_turns(text)
    answered = [t for t in turns if t.response]
    assert len(answered) == 1


def test_with_tool_results_two_turns(corpus):
    text = _prepare(corpus("turns/with_tool_results.txt"))
    turns = parse_turns(text)
    answered = [t for t in turns if t.response]
    assert len(answered) == 2


# ── Content extraction ────────────────────────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("turns"))
def test_turns_have_content(capture, corpus_with_expected):
    text, expected = corpus_with_expected(capture)
    text = _prepare(text)
    turns = parse_turns(text)

    if "turns" not in expected:
        pytest.skip("no expected turns in sidecar")

    for i, exp in enumerate(expected["turns"]):
        assert i < len(turns), f"expected turn {i} but only got {len(turns)}"
        turn = turns[i]
        if "prompt_contains" in exp:
            assert exp["prompt_contains"] in turn.prompt, (
                f"turn {i} prompt should contain {exp['prompt_contains']!r}, got {turn.prompt!r}"
            )
        if "response_contains" in exp:
            assert exp["response_contains"] in turn.response, (
                f"turn {i} response should contain {exp['response_contains']!r}"
            )
        if "response_starts_with" in exp:
            assert turn.response.startswith(exp["response_starts_with"])


# ── Last response extraction ─────────────────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("turns"))
def test_extract_last_response(capture, corpus_with_expected):
    text, expected = corpus_with_expected(capture)
    text = _prepare(text)
    response = extract_last_response(text)

    if "last_response_contains" in expected:
        assert expected["last_response_contains"] in response


def test_fresh_launch_no_response(corpus):
    text = _prepare(corpus("ready/fresh_launch.txt"))
    assert extract_last_response(text) == ""


# ── Tool results preserved ───────────────────────────────────────────


def test_tool_result_lines_in_response(corpus):
    text = _prepare(corpus("turns/with_tool_results.txt"))
    turns = parse_turns(text)
    # The second turn should have tool result content
    tool_turn = turns[1]
    assert "Read" in tool_turn.response or "\u23bf" in tool_turn.response
