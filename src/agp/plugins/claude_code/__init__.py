"""Claude Code TUI parser and adapter package.

Public API:
- ClaudeCodeAdapter — the agent adapter
- parse_turns, Turn, extract_last_response — content extraction
- is_ready, is_working, ends_with_prompt, is_shell_returned, is_completed_turn — state classification
- classify_gate, gate_response, GateKind — gate prompt handling
- normalize_screen, screen_tail — screen normalization
- extract_trailing_json — output contract JSON extraction
- TuiMetadata, extract_metadata — status bar metadata extraction
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
from agp.plugins.claude_code._metadata import TuiMetadata, extract_metadata
from agp.plugins.claude_code._normalize import normalize_screen, screen_tail
from agp.plugins.claude_code._parse import Turn, extract_last_response, parse_turns

# Adapter and convenience helpers
from agp.plugins.claude_code.adapter import ClaudeCodeAdapter  # noqa: F401
from agp.plugins.claude_code.adapter import (  # noqa: F401
    _clean_claude_code_output,
    _extract_trailing_json_text,
    _parse_claude_code_turns,
    _repair_json_string,
)
from agp.runtime import _strip_ansi  # noqa: F401

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
    # Metadata
    "TuiMetadata",
    "extract_metadata",
    # Compat re-exports
    "_clean_claude_code_output",
    "_extract_trailing_json_text",
    "_parse_claude_code_turns",
    "_repair_json_string",
    "_strip_ansi",
]
