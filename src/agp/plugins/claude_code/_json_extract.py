"""Trailing JSON extraction and repair for output contract validation."""

from __future__ import annotations

import json
import re


def repair_json_string(text: str) -> str:
    """Attempt to repair malformed JSON by fixing common quote issues."""
    # Fix unescaped double quotes inside string values
    # Pattern: "key": "value with "quotes" inside"
    # This is a best-effort heuristic.
    result = text
    # Fix escaped single quotes that should be double quotes
    result = result.replace("\\'", "'")
    return result


def extract_trailing_json(text: str) -> str | None:
    """Extract and validate trailing JSON object or array from text.

    Searches backwards from the end of the text for a complete JSON
    structure. Returns the parsed-and-re-serialized JSON string, or
    None if no valid JSON is found.
    """
    stripped = text.rstrip()
    if not stripped:
        return None

    # Try the full text first (common case: entire output is JSON)
    for raw in _candidate_json_strings(stripped):
        parsed = _try_parse_json(raw)
        if parsed is not None:
            return json.dumps(parsed)

    return None


def _candidate_json_strings(text: str) -> list[str]:
    """Generate candidate JSON strings from the end of text."""
    candidates: list[str] = []

    # Find the last } or ] and match backward to { or [
    for end_char, start_char in [("}", "{"), ("]", "[")]:
        idx = text.rfind(end_char)
        if idx < 0:
            continue
        # Search backward for the matching opener.
        # When scanning backward, a " at position i is escaped if
        # preceded (in the forward direction) by an odd number of \.
        depth = 0
        in_string = False
        i = idx
        while i >= 0:
            ch = text[i]
            if ch == '"':
                # Count preceding backslashes to detect escaping
                n_bs = 0
                j = i - 1
                while j >= 0 and text[j] == '\\':
                    n_bs += 1
                    j -= 1
                if n_bs % 2 == 0:
                    in_string = not in_string
                # Skip past the backslashes we already inspected
                i = j
                continue
            if in_string:
                i -= 1
                continue
            if ch == end_char:
                depth += 1
            elif ch == start_char:
                depth -= 1
                if depth == 0:
                    candidates.append(text[i:idx + 1])
                    break
            i -= 1

    # Also try extracting from markdown code blocks
    code_block_re = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
    for match in code_block_re.finditer(text):
        candidates.append(match.group(1).strip())

    return candidates


def _try_parse_json(raw: str) -> object | None:
    """Try to parse JSON, with repair fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try with repair
    try:
        return json.loads(repair_json_string(raw))
    except json.JSONDecodeError:
        return None
