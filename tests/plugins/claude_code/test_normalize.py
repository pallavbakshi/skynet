"""Normalization and noise filtering tests against the corpus."""

from __future__ import annotations

import pytest

from agp.plugins.claude_code._normalize import (
    is_noise_line,
    is_status_continuation,
    normalize_screen,
    screen_tail,
)


def test_normalize_strips_trailing_blanks():
    raw = "line1\nline2\n\n\n"
    assert normalize_screen(raw) == "line1\nline2"


def test_normalize_handles_crlf():
    raw = "line1\r\nline2\r\n"
    assert normalize_screen(raw) == "line1\nline2"


def test_separator_is_noise():
    assert is_noise_line("\u2500" * 20)


def test_status_bar_is_noise():
    assert is_noise_line("  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)")


def test_welcome_box_is_noise():
    assert is_noise_line("\u256d" + "\u2500" * 40 + "\u256e")
    assert is_noise_line("\u2502  Claude Code v2.1.91")


def test_blank_line_is_not_noise():
    assert not is_noise_line("")
    assert not is_noise_line("   ")


def test_prompt_line_is_not_noise():
    assert not is_noise_line("\u276f What is 2+2?")


def test_response_line_is_not_noise():
    assert not is_noise_line("\u23fa The answer is 4.")


def test_status_continuation_patterns():
    assert is_status_continuation("  esc to interrupt")
    assert is_status_continuation("  shift+tab to cycle")
    assert is_status_continuation("  bypass permissions on (shift+tab)")
    assert is_status_continuation("                                       16418 tokens")
    assert is_status_continuation("Claude Code has switched from npm to native installer.")
    assert is_status_continuation("  · esc to interrupt")


def test_normal_text_not_status_continuation():
    assert not is_status_continuation("The answer is 42.")
    assert not is_status_continuation("\u276f What is 2+2?")


def test_screen_tail_filters_status_bar(corpus):
    text = normalize_screen(corpus("ready/fresh_launch.txt"))
    tail = screen_tail(text)
    assert "\u23f5\u23f5" not in tail  # no status bar in tail


def test_screen_tail_filters_separators(corpus):
    text = normalize_screen(corpus("ready/post_response.txt"))
    tail = screen_tail(text)
    # Separators should not appear
    for line in tail.splitlines():
        s = line.strip()
        if s:
            assert not all(c == "\u2500" for c in s), "separator should be filtered"


def test_screen_tail_upgrade_notification_filtered(corpus):
    text = normalize_screen(corpus("edge/upgrade_notification.txt"))
    tail = screen_tail(text)
    assert "claude install" not in tail.lower()
    assert "native installer" not in tail.lower()


# ── Heartbeat last_line filtering ────────────────────────────────────
# These test the same filtering logic the adapter uses (adapter.py:412)
# to select last_line for progress heartbeats.


def _pick_last_line(text: str) -> str:
    """Replicate the adapter's last_line selection logic."""
    from agp.plugins.claude_code._markers import PROMPT_PREFIX

    for ln in reversed(text.splitlines()):
        stripped = ln.strip()
        if not stripped:
            continue
        if is_noise_line(stripped) or is_status_continuation(stripped):
            continue
        if stripped.rstrip() == PROMPT_PREFIX:
            continue
        return stripped[:80]
    return ""


def test_heartbeat_skips_status_bar():
    text = "⏺ The answer is 4.\n⏵⏵ bypass permissions on (shift+tab to cycle)"
    assert _pick_last_line(text) == "⏺ The answer is 4."


def test_heartbeat_skips_token_count():
    text = "⏺ Some response\n                     16,418 tokens"
    assert _pick_last_line(text) == "⏺ Some response"


def test_heartbeat_skips_separator():
    text = "⏺ Some response\n" + "─" * 40
    assert _pick_last_line(text) == "⏺ Some response"


def test_heartbeat_skips_status_continuation():
    text = "⏺ Some response\n  esc to interrupt\n  shift+tab to cycle"
    assert _pick_last_line(text) == "⏺ Some response"


def test_heartbeat_skips_upgrade_notification():
    text = "⏺ Some response\nClaude Code has switched from npm to native installer."
    assert _pick_last_line(text) == "⏺ Some response"


def test_heartbeat_returns_thinking_line():
    text = "⏺ Previous response\n✳ Thinking…\n⏵⏵ bypass permissions on"
    assert _pick_last_line(text) == "✳ Thinking…"


def test_heartbeat_returns_content_through_noise():
    """Content line followed by multiple noise layers — still reachable."""
    text = (
        "❯ do something\n"
        "⏺ Working on it...\n"
        "─" * 40 + "\n"
        "⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        "  esc to interrupt\n"
        "                     16,418 tokens\n"
    )
    assert _pick_last_line(text) == "⏺ Working on it..."


def test_heartbeat_skips_bare_prompt_glyph():
    text = "⏺ Some response\n❯"
    assert _pick_last_line(text) == "⏺ Some response"


def test_heartbeat_skips_prompt_through_noise():
    """Bare ❯ plus noise layers — should reach the content line."""
    text = (
        "⏺ Working on it...\n"
        "❯\n"
        "⏵⏵ bypass permissions on\n"
        "  16,418 tokens\n"
    )
    assert _pick_last_line(text) == "⏺ Working on it..."


def test_heartbeat_empty_when_only_prompt_and_noise():
    text = "❯\n⏵⏵ bypass permissions on\n  16,418 tokens"
    assert _pick_last_line(text) == ""


def test_heartbeat_empty_when_all_noise():
    text = "⏵⏵ bypass permissions on\n  shift+tab to cycle\n  16,418 tokens"
    assert _pick_last_line(text) == ""
