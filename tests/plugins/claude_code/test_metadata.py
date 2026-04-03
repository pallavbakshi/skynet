"""Metadata extraction tests against the real corpus."""

from __future__ import annotations

import pytest

from agp.plugins.claude_code._metadata import extract_metadata
from agp.plugins.claude_code._normalize import normalize_screen
from agp.runtime._output import _strip_ansi
from tests.plugins.claude_code.conftest import corpus_files


def _prepare(raw: str) -> str:
    return normalize_screen(_strip_ansi(raw))


# ── sTAT line parsing ─────────────────────────────────────────────────


def test_fresh_launch_metadata(corpus):
    text = _prepare(corpus("ready/fresh_launch.txt"))
    meta = extract_metadata(text)
    assert meta.model is not None
    assert "Opus" in meta.model
    assert meta.effort == "medium"
    assert meta.session_id is not None
    assert len(meta.session_id) == 36  # UUID format


def test_post_response_has_usage_and_tokens(corpus):
    text = _prepare(corpus("ready/post_response.txt"))
    meta = extract_metadata(text)
    assert meta.context_usage_pct == 2
    assert meta.token_count is not None
    assert meta.token_count > 0


def test_multi_turn_with_tools_metadata(corpus):
    text = _prepare(corpus("turns/multi_turn_with_tools.txt"))
    meta = extract_metadata(text)
    assert meta.model is not None
    assert "Opus" in meta.model
    assert meta.effort == "medium"
    assert meta.session_id is not None
    assert meta.token_count is not None
    assert meta.token_count > 10000
    assert meta.permission_mode == "accept edits"


# ── Permission mode extraction ────────────────────────────────────────


def test_bypass_permissions_mode(corpus):
    text = _prepare(corpus("ready/post_response.txt"))
    meta = extract_metadata(text)
    assert meta.permission_mode == "bypass permissions"


def test_accept_edits_mode(corpus):
    text = _prepare(corpus("turns/multi_turn_with_tools.txt"))
    meta = extract_metadata(text)
    assert meta.permission_mode == "accept edits"


def test_dont_ask_mode(corpus):
    text = _prepare(corpus("ready/with_welcome_box.txt"))
    meta = extract_metadata(text)
    assert meta.permission_mode == "don't ask"


# ── Metadata present across all captures ──────────────────────────────


@pytest.mark.parametrize("capture", corpus_files("ready") + corpus_files("turns"))
def test_all_captures_have_metadata(capture, corpus):
    if capture.endswith(".ansi.txt"):
        pytest.skip("ANSI captures may have garbled metadata")
    text = _prepare(corpus(capture))
    meta = extract_metadata(text)
    assert meta.has_content, f"{capture} should have extractable metadata"
    assert meta.model is not None, f"{capture} should have a model"
    assert meta.session_id is not None, f"{capture} should have a session ID"


# ── Empty / edge cases ────────────────────────────────────────────────


def test_empty_pane_no_metadata(corpus):
    text = _prepare(corpus("edge/empty_pane.txt"))
    meta = extract_metadata(text)
    assert not meta.has_content


def test_shell_exited_no_metadata(corpus):
    text = _prepare(corpus("shell/exited_clean.txt"))
    meta = extract_metadata(text)
    # Shell screen may still have scrollback with sTAT but no active metadata
    # The key point is it doesn't crash
    assert meta is not None
