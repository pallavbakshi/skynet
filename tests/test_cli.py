"""Focused CLI helper tests."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agp.cli import (
    _cli_client,
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

    def test_extract_trailing_json_payload_recovers_fenced_json(self) -> None:
        payload = _extract_trailing_json_payload(
            "review notes here\n"
            "```json\n"
            '{"verdict":"approved","summary":"looks good"}\n'
            "```"
        )
        self.assertEqual(payload, {"verdict": "approved", "summary": "looks good"})

    def test_extract_trailing_json_payload_recovers_fenced_json_with_internal_newlines(self) -> None:
        payload = _extract_trailing_json_payload(
            "review notes here\n"
            "```json\n"
            "{\n"
            '  "verdict": "approved",\n'
            '  "summary": "wrapped review"\n'
            "}\n"
            "```"
        )
        self.assertEqual(payload, {"verdict": "approved", "summary": "wrapped review"})

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


class CliClientTransportErrorTest(unittest.TestCase):
    """Verify _cli_client catches transport errors and exits cleanly."""

    @patch("agp.cli._make_client")
    def test_transport_error_prints_friendly_message(self, mock_make: MagicMock) -> None:
        import httpx

        # Simulate a ConnectError raised inside the context manager
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock())
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        # Make the client's method raise TransportError
        def _raise(*_a, **_kw):
            raise httpx.ConnectError("Connection refused")

        ctx.__enter__.return_value.health = _raise

        from click.exceptions import Exit

        with self.assertRaises(Exit) as cm:
            with _cli_client("http://localhost:9999") as client:
                client.health()

        self.assertEqual(cm.exception.exit_code, 1)

    @patch("agp.cli._make_client")
    def test_http_status_error_passes_through(self, mock_make: MagicMock) -> None:
        import httpx

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock())
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        def _raise(*_a, **_kw):
            resp = httpx.Response(404, request=httpx.Request("GET", "http://x"))
            raise httpx.HTTPStatusError("not found", request=resp.request, response=resp)

        ctx.__enter__.return_value.get_job = _raise

        # HTTPStatusError should NOT be caught by _cli_client — it passes through
        with self.assertRaises(httpx.HTTPStatusError):
            with _cli_client("http://localhost:9999") as client:
                client.get_job("job_123")
