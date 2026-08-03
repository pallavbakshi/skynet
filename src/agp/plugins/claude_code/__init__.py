"""Claude Code agent adapter package.

Backed by the smallops library for TUI lifecycle and parsing.
AGP-specific orchestration (output contracts, artifacts, recovery)
lives in adapter.py.
"""

from __future__ import annotations

from agp.plugins.claude_code.adapter import ClaudeCodeAdapter

__all__ = [
    "ClaudeCodeAdapter",
]
