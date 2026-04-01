"""Focused CLI helper tests."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from agp.cli import (
    _capture_git_diff,
    _cli_client,
    _cli_idempotency_key,
    _extract_trailing_json_payload,
    _review_attachment_note,
    _review_fix_attachment_note,
    app,
)

runner = CliRunner()


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

    def test_extract_trailing_json_payload_repairs_unescaped_quotes(self) -> None:
        payload = _extract_trailing_json_payload(
            "review notes here\n"
            '{"verdict":"changes_requested","summary":"broken","findings":'
            '[{"severity":"medium","description":"returns str(exc) in "line" path","line":34}]}'
        )
        self.assertEqual(
            payload,
            {
                "verdict": "changes_requested",
                "summary": "broken",
                "findings": [
                    {
                        "severity": "medium",
                        "description": 'returns str(exc) in "line" path',
                        "line": 34,
                    }
                ],
            },
        )

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


class ReplyCommandTest(unittest.TestCase):
    @patch("agp.cli._make_client")
    def test_reply_accepts_task_from_stdin_when_argument_omitted(self, mock_make: MagicMock) -> None:
        fake_client = MagicMock()
        fake_client.get_job.return_value = {
            "job_id": "job_src",
            "message_id": "msg_123",
            "conversation_id": "conv_123",
            "target_agent_id": "agt_reply",
        }
        fake_client.send.return_value = {"job_id": "job_new"}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, ["reply", "job_src", "--detach"], input="follow-up from stdin\n")

        self.assertEqual(result.exit_code, 0, result.output)
        fake_client.send.assert_called_once()
        self.assertEqual(fake_client.send.call_args.args[:3], ("agent", "agt_reply", "follow-up from stdin"))

    @patch("agp.cli._make_client")
    @patch("agp.cli.sys.stdin.read", side_effect=AssertionError("stdin should not be read"))
    def test_reply_validates_output_contract_before_reading_stdin(
        self,
        _mock_stdin_read: MagicMock,
        mock_make: MagicMock,
    ) -> None:
        result = runner.invoke(app, ["reply", "job_src", "--detach", "--output-contract", "{"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("invalid JSON for --output-contract", result.output)
        mock_make.assert_not_called()

    @patch("agp.cli._make_client")
    def test_reply_rejects_empty_stdin_when_argument_omitted(self, mock_make: MagicMock) -> None:
        result = runner.invoke(app, ["reply", "job_src", "--detach"], input="\n")

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("task is required", result.output)
        mock_make.assert_not_called()


class ReviewCommandTest(unittest.TestCase):
    @patch("agp.cli._poll_until_done")
    @patch("agp.cli._make_client")
    def test_review_forwards_normalized_json_findings_to_dev(
        self,
        mock_make: MagicMock,
        mock_poll_until_done: MagicMock,
    ) -> None:
        fake_client = MagicMock()
        fake_client.get_job.return_value = {
            "job_id": "job_src",
            "conversation_id": "conv_123",
            "target_agent_id": "dev_agent",
            "result_artifact_id": "artifact_source",
        }

        malformed_review = (
            "notes first\n"
            '{"verdict":"changes_requested","summary":"broken","findings":'
            '[{"severity":"medium","description":"returns str(exc) in "line" path","line":34}]}'
        )

        def fetch_artifact(artifact_id: str, *, content: bool = False):
            self.assertTrue(content)
            if artifact_id == "artifact_source":
                return {"content": "source result"}
            if artifact_id == "artifact_review":
                return {"content": malformed_review}
            raise AssertionError(f"unexpected artifact id: {artifact_id}")

        fake_client.fetch_artifact.side_effect = fetch_artifact
        fake_client.send.side_effect = [{"job_id": "job_review"}, {"job_id": "job_fix"}]

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        mock_poll_until_done.side_effect = [
            ({"status": "completed", "result_artifact_id": "artifact_review"}, False),
            ({"status": "completed"}, True),
        ]

        result = runner.invoke(app, ["review", "job_src", "reviewer_agent", "--max-rounds", "2"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(fake_client.send.call_count, 2)
        fix_text = fake_client.send.call_args_list[1].args[2]
        self.assertIn(
            '{"findings":[{"description":"returns str(exc) in \\"line\\" path","line":34,"severity":"medium"}],'
            '"summary":"broken","verdict":"changes_requested"}',
            fix_text,
        )
        self.assertNotIn('returns str(exc) in "line" path', fix_text)


class CaptureGitDiffTest(unittest.TestCase):
    """Verify _capture_git_diff handles missing git and real repos gracefully."""

    @patch("shutil.which", return_value=None)
    def test_returns_none_when_git_binary_missing(self, _mock_which: MagicMock) -> None:
        stat, diff = _capture_git_diff()
        self.assertIsNone(stat)
        self.assertIsNone(diff)

    @patch("subprocess.run", side_effect=Exception("no git"))
    def test_returns_none_when_rev_parse_fails(self, _mock_run: MagicMock) -> None:
        stat, diff = _capture_git_diff()
        self.assertIsNone(stat)
        self.assertIsNone(diff)

    @patch("shutil.which", return_value="git")
    def test_returns_stat_and_diff_in_git_repo(self, mock_which: MagicMock) -> None:
        from unittest.mock import MagicMock as _Mag

        def fake_run(cmd: list[str], **kwargs):
            result = _Mag()
            result.returncode = 0
            if "rev-parse" in cmd:
                result.stdout = "true\n"
                result.check_returncode = lambda: None
            elif "--stat" in cmd:
                result.stdout = " file.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
            elif "diff" in cmd and "HEAD" in cmd:
                result.stdout = "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
            elif "ls-files" in cmd:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            stat, diff = _capture_git_diff()
        self.assertIn("file.py", stat)
        self.assertIn("diff --git", diff)
        self.assertNotIn("Untracked", diff)

    @patch("shutil.which", return_value="git")
    def test_returns_none_when_diff_is_empty(self, mock_which: MagicMock) -> None:
        from unittest.mock import MagicMock as _Mag

        def fake_run(cmd: list[str], **kwargs):
            result = _Mag()
            result.returncode = 0
            if "rev-parse" in cmd:
                result.stdout = "true\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            stat, diff = _capture_git_diff()
        self.assertIsNone(stat)
        self.assertIsNone(diff)

    @patch("shutil.which", return_value="git")
    def test_includes_untracked_files(self, mock_which: MagicMock) -> None:
        from unittest.mock import MagicMock as _Mag

        def fake_run(cmd: list[str], **kwargs):
            result = _Mag()
            result.returncode = 0
            if "rev-parse" in cmd:
                result.stdout = "true\n"
            elif "ls-files" in cmd:
                result.stdout = "new_file.py\n"
            elif "--stat" in cmd:
                result.stdout = ""
            elif "diff" in cmd and "HEAD" in cmd:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            stat, diff = _capture_git_diff()
        self.assertIn("new_file.py", stat)
        self.assertIn("Untracked", diff)
