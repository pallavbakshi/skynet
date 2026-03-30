"""Focused CLI helper tests."""

from __future__ import annotations

import json
import unittest

from agp.cli import (
    _cli_idempotency_key,
    _extract_trailing_json_payload,
    _review_attachment_note,
    _review_fix_attachment_note,
)


class CliHelpersTest(unittest.TestCase):
    def test_cli_idempotency_keys_are_unique(self) -> None:
        first = _cli_idempotency_key("cli")
        second = _cli_idempotency_key("cli")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("cli-"))

    def test_extract_trailing_json_payload_ignores_leading_prose(self) -> None:
        payload = _extract_trailing_json_payload(
            "review notes here\n"
            + json.dumps({"verdict": "approved", "summary": "ok"})
        )
        self.assertEqual(payload, {"verdict": "approved", "summary": "ok"})

    def test_extract_trailing_json_payload_recovers_wrapped_json(self) -> None:
        payload = _extract_trailing_json_payload(
            'notes first\n{"verdict":"changes_requested","summary":"wrapped re\n'
            'view"}'
        )
        self.assertEqual(payload, {"verdict": "changes_requested", "summary": "wrapped review"})

    def test_extract_trailing_json_payload_returns_none_when_missing(self) -> None:
        self.assertIsNone(_extract_trailing_json_payload("plain text only"))

    def test_review_attachment_note_mentions_short_valid_outputs(self) -> None:
        note = _review_attachment_note(
            attachment_name="result.txt",
            short_output_guidance="Short outputs can still be valid.",
        )
        self.assertIn("result.txt", note)
        self.assertIn("Short outputs can still be valid.", note)

    def test_review_fix_attachment_note_mentions_short_valid_outputs(self) -> None:
        note = _review_fix_attachment_note(
            attachment_name="fix.txt",
            short_output_guidance="Short outputs can still be valid.",
        )
        self.assertIn("fix.txt", note)
        self.assertIn("Short outputs can still be valid.", note)
