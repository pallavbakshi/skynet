"""Claude Code TUI parser and adapter package.

Public API:
- ClaudeCodeAdapter — the agent adapter (from legacy module for now)
- parse_turns, Turn, extract_last_response — content extraction
- is_ready, is_working, ends_with_prompt, is_shell_returned, is_completed_turn — state classification
- classify_gate, gate_response, GateKind — gate prompt handling
- normalize_screen, screen_tail — screen normalization
- extract_trailing_json — output contract JSON extraction
"""

from __future__ import annotations

# Parser API
from agp.plugins.claude_code._classify import (
    ends_with_prompt,
    is_completed_turn,
    is_ready,
    is_shell_returned,
    is_working,
)
from agp.plugins.claude_code._gates import GateKind, classify_gate, gate_response
from agp.plugins.claude_code._json_extract import extract_trailing_json
from agp.plugins.claude_code._normalize import normalize_screen, screen_tail
from agp.plugins.claude_code._parse import Turn, extract_last_response, parse_turns

# Legacy adapter — re-exported so existing imports continue to work.
# The adapter class still lives in the old monolithic module until
# it is migrated to adapter.py.
from agp.plugins._claude_code_legacy import ClaudeCodeAdapter  # noqa: F401
from agp.plugins._claude_code_legacy import (  # noqa: F401
    _clean_claude_code_output,
    _extract_trailing_json_text,
    _parse_claude_code_turns,
    _repair_json_string,
    _strip_ansi,
)

__all__ = [
    # Adapter
    "ClaudeCodeAdapter",
    # Parser
    "Turn",
    "parse_turns",
    "extract_last_response",
    # Classification
    "is_ready",
    "is_working",
    "ends_with_prompt",
    "is_shell_returned",
    "is_completed_turn",
    # Gates
    "GateKind",
    "classify_gate",
    "gate_response",
    # Normalization
    "normalize_screen",
    "screen_tail",
    # JSON
    "extract_trailing_json",
    # Legacy re-exports
    "_clean_claude_code_output",
    "_extract_trailing_json_text",
    "_parse_claude_code_turns",
    "_repair_json_string",
    "_strip_ansi",
]
