#!/usr/bin/env python3
"""Fail if AGP imports smallops private modules directly."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGP_ROOT = ROOT / "src" / "agp"


def _is_private_smallops_module(module: str) -> bool:
    parts = module.split(".")
    if not parts or parts[0] != "smallops":
        return False
    return any(part.startswith("_") for part in parts[1:])


def _violations(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_private_smallops_module(alias.name):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _is_private_smallops_module(module):
                found.append((node.lineno, module))
    return found


def main() -> int:
    failures: list[str] = []
    for path in sorted(AGP_ROOT.rglob("*.py")):
        for lineno, module in _violations(path):
            rel = path.relative_to(ROOT)
            failures.append(f"{rel}:{lineno}: private smallops import: {module}")

    if failures:
        print("AGP must import smallops only through its public API:", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
