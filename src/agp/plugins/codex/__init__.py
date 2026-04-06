"""Codex agent adapter package.

Backed by the smallops library for TUI lifecycle and parsing.
AGP-specific orchestration (output contracts, artifacts, recovery)
lives in adapter.py.
"""

from __future__ import annotations

from agp.plugins.codex.adapter import CodexAdapter  # noqa: F401

__all__ = ["CodexAdapter"]
