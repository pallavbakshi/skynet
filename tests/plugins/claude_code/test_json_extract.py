"""JSON extraction tests."""

from __future__ import annotations

from agp.plugins.claude_code._json_extract import extract_trailing_json


def test_trailing_json_object():
    text = 'Some text before\n{"key": "value"}'
    result = extract_trailing_json(text)
    assert result is not None
    assert '"key"' in result


def test_trailing_json_array():
    text = 'Preamble\n[1, 2, 3]'
    result = extract_trailing_json(text)
    assert result is not None
    assert "[1, 2, 3]" in result


def test_no_json():
    assert extract_trailing_json("just plain text") is None


def test_empty_string():
    assert extract_trailing_json("") is None


def test_json_in_code_block():
    text = 'Here is the result:\n```json\n{"verdict": "approved"}\n```'
    result = extract_trailing_json(text)
    assert result is not None
    assert "approved" in result


def test_nested_json():
    text = '{"outer": {"inner": "value"}, "list": [1, 2]}'
    result = extract_trailing_json(text)
    assert result is not None
    assert "inner" in result
