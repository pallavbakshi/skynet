#!/usr/bin/env python3
"""Print smallops parser classifications for the offline Claude Code corpus."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallops import ClaudeCodeTui
from smallops._util import normalize_screen, strip_ansi

CORPUS = ROOT / "smallops_tests" / "claude_code" / "corpus"


def main() -> None:
    tui = ClaudeCodeTui()
    for path in sorted(CORPUS.rglob("*")):
        if path.suffix not in {".txt", ".raw"}:
            continue
        screen = normalize_screen(strip_ansi(path.read_text(encoding="utf-8", errors="replace")))
        status = tui.parse_status(screen)
        gate = tui.gate_response(screen)
        rel = path.relative_to(CORPUS)
        print(
            f"{rel}\tclassify={tui.classify_idle(screen).value}"
            f"\tgate_resp={gate!r}\tmodel={status.model!r}\ttokens={status.tokens}"
        )


if __name__ == "__main__":
    main()
