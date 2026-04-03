"""Corpus loading fixtures for Claude Code TUI parser tests.

The corpus stores three versions of each capture:
- .raw  — tmux capture-pane -e (with ANSI escapes, source of truth)
- .txt  — tmux capture-pane (plain text, for readability and tests)
- .scrollback.txt — tmux capture-pane -S - (full scrollback)

Tests typically load .txt files.  Use .raw files when testing ANSI
stripping robustness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CORPUS_DIR = Path(__file__).parent / "corpus"


@pytest.fixture
def corpus():
    """Load a corpus file by relative path.

    Usage::

        def test_something(corpus):
            text = corpus("ready/fresh_launch.txt")
            raw = corpus("ready/fresh_launch.raw")  # with ANSI
    """
    def _load(relpath: str) -> str:
        path = CORPUS_DIR / relpath
        if not path.exists():
            pytest.skip(f"corpus file not found: {relpath}")
        return path.read_text()
    return _load


@pytest.fixture
def corpus_with_expected():
    """Load a corpus file and its .expected.json sidecar.

    Usage::

        def test_turns(corpus_with_expected):
            text, expected = corpus_with_expected("turns/single_short.txt")
    """
    def _load(relpath: str) -> tuple[str, dict]:
        path = CORPUS_DIR / relpath
        if not path.exists():
            pytest.skip(f"corpus file not found: {relpath}")
        text = path.read_text()
        expected_path = path.with_suffix(".expected.json")
        expected = json.loads(expected_path.read_text()) if expected_path.exists() else {}
        return text, expected
    return _load


def corpus_files(category: str, suffix: str = ".txt") -> list[str]:
    """Return relative paths to all corpus files in a category directory.

    Filters out .raw, .scrollback.txt, .capture.json, and .expected.json
    — only returns the primary .txt captures by default.

    Suitable for ``@pytest.mark.parametrize``::

        @pytest.mark.parametrize("capture", corpus_files("ready"))
        def test_ready_screens(capture, corpus):
            text = corpus(capture)
    """
    category_dir = CORPUS_DIR / category
    if not category_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(CORPUS_DIR))
        for p in category_dir.glob(f"*{suffix}")
        if not p.name.startswith(".")
        and not p.name.endswith(".scrollback.txt")
        and not p.name.endswith(".capture.json")
        and not p.name.endswith(".expected.json")
    )
