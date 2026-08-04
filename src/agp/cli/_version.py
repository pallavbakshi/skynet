"""Version reporting: package version + git commit SHA."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


def _git_short_sha(min_length: int = 7) -> str | None:
    """Short SHA of HEAD, or ``None`` if git is missing / not a git repo.

    Uses ``--short=N`` so git guarantees at least N chars and auto-extends
    the prefix if it is ambiguous in this repo (collision-safe).
    """
    here = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "-C", str(here), "rev-parse", f"--short={min_length}", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


@lru_cache(maxsize=1)
def package_version() -> str:
    """Installed package version (e.g. ``0.1.0``) from distribution metadata."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("agp")
    except PackageNotFoundError:
        return "0.0.0"


def format_version() -> str:
    """``<version>+<short-sha>`` (PEP 440 local segment); SHA falls back to ``unknown``."""
    sha = _git_short_sha() or "unknown"
    return f"{package_version()}+{sha}"
