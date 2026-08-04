"""Guard: promoted corpus captures must never contain secrets.

Corpus files are real TUI screen captures promoted from Docker qualification
runs. The herdr mux records the shell launch line (``$ env … script -qfec …``),
which has leaked real API keys (e.g. ``sk-or-…``) into captures before. This
test scans every committed corpus capture for common secret patterns and the
herdr launch preamble, and fails if any are found — so a leak cannot recur.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CORPUS_ROOTS = [
    Path(__file__).parent / "codex" / "corpus",
    Path(__file__).parent / "claude_code" / "corpus",
]

# Patterns are deliberately value-shaped (a key-like token or non-empty secret
# assignment) so a capture merely mentioning a var name doesn't false-positive.
_SECRET_PATTERNS = [
    re.compile(r"sk-or-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r'OPENROUTER_API_KEY\s*=\s*["\']?[A-Za-z0-9_-]{8,}'),
    re.compile(r'OPENAI_API_KEY\s*=\s*["\']?sk'),
    re.compile(r'ANTHROPIC_(?:API_KEY|AUTH_TOKEN)\s*=\s*["\']?[A-Za-z0-9_-]{8,}'),
    re.compile(r"script\s+-qfec"),  # herdr launch preamble — must be stripped before promotion
]


def _corpus_files() -> list[Path]:
    files: list[Path] = []
    for root in _CORPUS_ROOTS:
        if root.exists():
            files.extend(p for p in sorted(root.rglob("*")) if p.suffix in {".txt", ".raw"})
    return files


@pytest.mark.offline
def test_corpus_captures_contain_no_secrets() -> None:
    assert _corpus_files(), "no corpus capture files found — guard misconfigured"

    offenders: list[str] = []
    for path in _corpus_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in _SECRET_PATTERNS:
            if pat.search(text):
                rel = path.relative_to(Path(__file__).parent.parent)
                offenders.append(f"{rel}: /{pat.pattern}/")

    assert not offenders, (
        "secrets or launch preamble found in corpus captures — redact/strip before committing:\n  "
        + "\n  ".join(offenders)
    )
