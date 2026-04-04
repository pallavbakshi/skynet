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
    _poll_until_done,
    _review_attachment_note,
    _review_fix_attachment_note,
    _strip_tui_action_traces,
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

    def test_strip_tui_action_traces_keeps_only_final_summary_block(self) -> None:
        raw = (
            "The schema change is in place. I’m running pytest across the repo now.\n"
            "pytest is still running; initial collection and early tests are passing.\n"
            "Waited for background terminal · pytest\n"
            "The original pytest run appears to have terminated after reporting failures.\n"
            "FAILED tests/mvp_flow/test_observability.py::test_case\n"
            "3 failed, 26 passed in 4.07s\n"
            "─ Worked for 1m 31s ─────────────────────────────────────\n"
            "Updated src/agp/schemas.py so RuntimeResponse includes capabilities.\n"
            "pytest did not pass cleanly in this workspace.\n"
            "RuntimeError: local control plane is still running (pid 36702).\n"
            "1 background terminal running · /ps to view · /stop to close\n"
        )

        cleaned = _strip_tui_action_traces(raw)

        self.assertEqual(
            cleaned,
            "Updated src/agp/schemas.py so RuntimeResponse includes capabilities.\n"
            "pytest did not pass cleanly in this workspace.",
        )
        self.assertNotIn("36702", cleaned)

    def test_strip_tui_action_traces_redacts_pid_in_final_summary(self) -> None:
        cleaned = _strip_tui_action_traces("Completed validation against live service (PID 4242).\n")
        self.assertEqual(cleaned, "Completed validation against live service (pid redacted).")

    def test_strip_tui_action_traces_drops_pytest_session_banner(self) -> None:
        raw = (
            "platform darwin -- Python 3.12.11, pytest-8.4.1, pluggy-1.6.0\n"
            "rootdir: /Users/pb/projects/skynet\n"
            "plugins: anyio-4.10.0\n"
            "collected 23 items\n"
            "\n"
            "Updated src/agp/cli.py to strip review attachment noise.\n"
            "Ran targeted CLI tests covering review artifact sanitization.\n"
            "23 passed in 0.17s\n"
        )

        cleaned = _strip_tui_action_traces(raw)

        self.assertEqual(
            cleaned,
            "Updated src/agp/cli.py to strip review attachment noise.\n"
            "Ran targeted CLI tests covering review artifact sanitization.",
        )
        self.assertNotIn("platform darwin", cleaned)
        self.assertNotIn("23 passed", cleaned)


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
    def test_reply_accepts_unquoted_multi_word_task(self, mock_make: MagicMock) -> None:
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

        # Simulate unquoted: reply job_src Three findings need fixes --detach
        result = runner.invoke(app, ["reply", "job_src", "Three", "findings", "need", "fixes", "--detach"])

        self.assertEqual(result.exit_code, 0, result.output)
        fake_client.send.assert_called_once()
        sent_task = fake_client.send.call_args.args[2]
        self.assertIn("Three findings need fixes", sent_task)

    @patch("agp.cli._make_client")
    def test_send_accepts_unquoted_multi_word_task(self, mock_make: MagicMock) -> None:
        fake_client = MagicMock()
        fake_client.send.return_value = {"job_id": "job_new"}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        # Simulate unquoted: send agent_x Analyze the code --detach
        result = runner.invoke(app, ["send", "agent_x", "Analyze", "the", "code", "--detach"])

        self.assertEqual(result.exit_code, 0, result.output)
        fake_client.send.assert_called_once()
        sent_task = fake_client.send.call_args.args[2]
        self.assertIn("Analyze the code", sent_task)

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
        self.assertIn("Describe only verification you actually completed", fix_text)
        self.assertIn("Do NOT mention background terminals, PIDs", fix_text)


class ReviewResumeTest(unittest.TestCase):
    """Tests for resumable review loop state persistence and --resume."""

    @patch("agp.cli._poll_until_done")
    @patch("agp.cli._make_client")
    def test_review_state_uploaded_on_session_start(
        self,
        mock_make: MagicMock,
        mock_poll: MagicMock,
    ) -> None:
        """Starting a review session should upload a review-state artifact."""
        fake_client = MagicMock()
        fake_client.get_job.return_value = {
            "job_id": "job_src",
            "conversation_id": "conv_123",
            "target_agent_id": "dev_agent",
            "result_artifact_id": "artifact_source",
        }

        def fetch_artifact(artifact_id: str, *, content: bool = False):
            if artifact_id == "artifact_source":
                return {"content": "source result"}
            if artifact_id == "artifact_review":
                return {"content": '{"verdict":"approved","summary":"lgtm"}'}
            raise AssertionError(f"unexpected artifact id: {artifact_id}")

        fake_client.fetch_artifact.side_effect = fetch_artifact
        fake_client.send.return_value = {"job_id": "job_review"}
        fake_client.upload_artifact.return_value = {"storage_ref": "ref_1", "role": "review-state"}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        # Reviewer approves on round 1
        mock_poll.return_value = (
            {"status": "completed", "result_artifact_id": "artifact_review"},
            False,
        )

        result = runner.invoke(app, ["review", "job_src", "reviewer_agent"])
        self.assertEqual(result.exit_code, 0, result.output)

        # upload_artifact should have been called (initial state + transitions)
        self.assertGreaterEqual(fake_client.upload_artifact.call_count, 1)
        first_call = fake_client.upload_artifact.call_args_list[0]
        self.assertEqual(first_call.kwargs["role"], "review-state")
        self.assertEqual(first_call.kwargs["name"], "review-state.json")
        self.assertEqual(first_call.kwargs["namespace"], "conv_123")
        state = json.loads(first_call.kwargs["content"])
        self.assertEqual(state["source_job_id"], "job_src")
        self.assertIn("review_session_id", state)
        self.assertEqual(state["phase"], "send_to_reviewer")

    @patch("agp.cli._poll_until_done")
    @patch("agp.cli._make_client")
    def test_resume_fetches_state_and_reenters_loop(
        self,
        mock_make: MagicMock,
        mock_poll: MagicMock,
    ) -> None:
        """--resume should load saved state and re-enter the review loop."""
        saved_state = {
            "review_session_id": "rev_abc123",
            "source_job_id": "job_src",
            "reviewer_id": "reviewer_agent",
            "dev_id": "dev_agent",
            "max_rounds": 3,
            "current_round": 1,
            "phase": "poll_dev",
            "conversation_id": "conv_123",
            "active_job_id": "job_dev_fix",
            "last_verdict": "changes_requested",
            "updated_at": "2026-04-01T10:00:00+00:00",
        }

        fake_client = MagicMock()
        # get_job: first call for source job, second for active job
        fake_client.get_job.side_effect = [
            {
                "job_id": "job_src",
                "conversation_id": "conv_123",
                "target_agent_id": "dev_agent",
                "result_artifact_id": "artifact_source",
            },
            {
                "job_id": "job_dev_fix",
                "status": "completed",
                "result_artifact_id": "artifact_fix",
            },
        ]
        fake_client.list_job_artifacts.return_value = {
            "items": [
                {"artifact_id": "art_state_1", "role": "review-state"},
            ],
        }
        fake_client.upload_artifact.return_value = {"storage_ref": "ref_x", "role": "review-state"}

        def fetch_artifact(artifact_id: str, *, content: bool = False):
            if artifact_id == "art_state_1":
                return {"content": json.dumps(saved_state)}
            if artifact_id == "artifact_fix":
                return {"content": "fix output"}
            if artifact_id == "artifact_review_r2":
                return {"content": '{"verdict":"approved","summary":"all good"}'}
            raise AssertionError(f"unexpected artifact id: {artifact_id}")

        fake_client.fetch_artifact.side_effect = fetch_artifact
        fake_client.send.return_value = {"job_id": "job_review_r2"}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        # Reviewer approves on round 2
        mock_poll.return_value = (
            {"status": "completed", "result_artifact_id": "artifact_review_r2"},
            False,
        )

        result = runner.invoke(app, [
            "review", "job_src", "reviewer_agent",
            "--resume", "job_src",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Resuming session", result.output)
        self.assertIn("Approved", result.output)

        # Should have loaded state via list_job_artifacts
        fake_client.list_job_artifacts.assert_called_once_with("job_src", role="review-state")

    @patch("agp.cli._poll_until_done")
    @patch("agp.cli._make_client")
    def test_resume_handles_completed_pending_reviewer_job(
        self,
        mock_make: MagicMock,
        mock_poll: MagicMock,
    ) -> None:
        """Resume from poll_reviewer where reviewer already completed and approved."""
        saved_state = {
            "review_session_id": "rev_xyz789",
            "source_job_id": "job_src",
            "reviewer_id": "reviewer_agent",
            "dev_id": "dev_agent",
            "max_rounds": 3,
            "current_round": 2,
            "phase": "poll_reviewer",
            "conversation_id": "conv_456",
            "active_job_id": "job_reviewer_r2",
            "last_verdict": None,
            "updated_at": "2026-04-01T10:00:00+00:00",
        }

        fake_client = MagicMock()
        fake_client.get_job.side_effect = [
            {
                "job_id": "job_src",
                "conversation_id": "conv_456",
                "target_agent_id": "dev_agent",
            },
            {
                "job_id": "job_reviewer_r2",
                "status": "completed",
                "result_artifact_id": "artifact_rev_result",
            },
        ]
        fake_client.list_job_artifacts.return_value = {
            "items": [{"artifact_id": "art_state", "role": "review-state"}],
        }
        fake_client.upload_artifact.return_value = {"storage_ref": "ref_z", "role": "review-state"}

        def fetch_artifact(artifact_id: str, *, content: bool = False):
            if artifact_id == "art_state":
                return {"content": json.dumps(saved_state)}
            if artifact_id == "artifact_rev_result":
                return {"content": '{"verdict":"approved","summary":"looks great"}'}
            raise AssertionError(f"unexpected artifact id: {artifact_id}")

        fake_client.fetch_artifact.side_effect = fetch_artifact

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, [
            "review", "job_src", "reviewer_agent",
            "--resume", "job_src",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Approved after 2 round(s)", result.output)

        # Should NOT have re-polled (job was already completed)
        mock_poll.assert_not_called()
        # Should NOT have sent any new messages (reviewer already approved)
        fake_client.send.assert_not_called()


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
    def test_excludes_untracked_files_by_default(self, mock_which: MagicMock) -> None:
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
        self.assertIsNone(stat)
        self.assertIsNone(diff)

    @patch("shutil.which", return_value="git")
    def test_includes_untracked_files_when_requested(self, mock_which: MagicMock) -> None:
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
            stat, diff = _capture_git_diff(include_untracked=True)
        self.assertIn("new_file.py", stat)
        self.assertIn("Untracked", diff)


class PollUntilDoneTest(unittest.TestCase):
    """Verify _poll_until_done renders activity hints from progress events."""

    def _make_client(self, *, job_sequence, events_data=None, events_error=False):
        client = MagicMock()
        client.get_job.side_effect = list(job_sequence)
        if events_error:
            client.get_job_events.side_effect = Exception("connection failed")
        elif events_data is not None:
            client.get_job_events.return_value = events_data
        else:
            client.get_job_events.return_value = {"items": []}
        return client

    @patch("time.sleep", new=MagicMock())
    @patch("time.monotonic")
    def test_renders_last_line_hint(self, mock_monotonic: MagicMock) -> None:
        from datetime import datetime, timezone

        now_ts = datetime.now(timezone.utc).isoformat()
        client = self._make_client(
            job_sequence=[
                {"status": "running"},
                {"status": "running"},
                {"status": "completed", "result": "done"},
            ],
            events_data={"items": [
                {"body": {"message": "runtime.progress_heartbeat", "details": {"last_line": "Running pytest tests/", "output_chars": 4210}}, "created_at": now_ts},
            ]},
        )
        # start=0, while(0), now=0 (skip), while(11), now=11 (fire), while(12), completed
        mock_monotonic.side_effect = [0, 0, 0, 11, 11, 12]

        with patch("agp.cli.typer.echo") as mock_echo:
            job, timed_out = _poll_until_done(client, "job_1", timeout=60, heartbeat_interval=10)

        self.assertFalse(timed_out)
        echo_calls = [c.args[0] for c in mock_echo.call_args_list]
        matching = [c for c in echo_calls if "Running pytest" in c]
        self.assertTrue(matching, f"expected last_line hint in output, got: {echo_calls}")

    @patch("time.sleep", new=MagicMock())
    @patch("time.monotonic")
    def test_renders_output_chars_when_no_last_line(self, mock_monotonic: MagicMock) -> None:
        from datetime import datetime, timezone

        now_ts = datetime.now(timezone.utc).isoformat()
        client = self._make_client(
            job_sequence=[
                {"status": "running"},
                {"status": "running"},
                {"status": "completed"},
            ],
            events_data={"items": [
                {"body": {"message": "runtime.progress_heartbeat", "details": {"last_line": "", "output_chars": 4210}}, "created_at": now_ts},
            ]},
        )
        mock_monotonic.side_effect = [0, 0, 0, 11, 11, 12]

        with patch("agp.cli.typer.echo") as mock_echo:
            _poll_until_done(client, "job_1", timeout=60, heartbeat_interval=10)

        echo_calls = [c.args[0] for c in mock_echo.call_args_list]
        matching = [c for c in echo_calls if "4,210 chars output" in c]
        self.assertTrue(matching, f"expected output_chars hint, got: {echo_calls}")

    @patch("time.sleep", new=MagicMock())
    @patch("time.monotonic")
    def test_handles_event_fetch_failure_gracefully(self, mock_monotonic: MagicMock) -> None:
        client = self._make_client(
            job_sequence=[
                {"status": "running"},
                {"status": "running"},
                {"status": "completed"},
            ],
            events_error=True,
        )
        mock_monotonic.side_effect = [0, 0, 0, 11, 11, 12]

        with patch("agp.cli.typer.echo") as mock_echo:
            job, timed_out = _poll_until_done(client, "job_1", timeout=60, heartbeat_interval=10)

        self.assertFalse(timed_out)
        echo_calls = [c.args[0] for c in mock_echo.call_args_list]
        matching = [c for c in echo_calls if "Agent working" in c]
        self.assertTrue(matching, "should still show heartbeat even when events fail")

    @patch("time.sleep", new=MagicMock())
    @patch("time.monotonic")
    def test_stall_detection_when_event_is_old(self, mock_monotonic: MagicMock) -> None:
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        client = self._make_client(
            job_sequence=[
                {"status": "running"},
                {"status": "running"},
                {"status": "completed"},
            ],
            events_data={"items": [
                {"body": {"message": "runtime.progress_heartbeat", "details": {"last_line": "Thinking...", "output_chars": 100}}, "created_at": old_ts},
            ]},
        )
        mock_monotonic.side_effect = [0, 0, 0, 11, 11, 12]

        with patch("agp.cli.typer.echo") as mock_echo:
            _poll_until_done(client, "job_1", timeout=60, heartbeat_interval=10)

        echo_calls = [c.args[0] for c in mock_echo.call_args_list]
        stalled = [c for c in echo_calls if "stalled" in c]
        self.assertTrue(stalled, f"expected (stalled) hint, got: {echo_calls}")


class ClaudeCodeWorkingDetectionTest(unittest.TestCase):
    """Verify _looks_like_working matches old and new Claude Code thinking indicators."""

    def test_old_thinking_prefix(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        self.assertTrue(adapter._looks_like_working("∴ thinking about this..."))
        self.assertTrue(adapter._looks_like_working("∴ Working on changes"))

    def test_new_thinking_indicators(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        # New-style indicators seen in Claude Code v2.1.89+
        self.assertTrue(adapter._looks_like_working("✳ Swooping… (51s · ↓ 3.7k tokens)"))
        self.assertTrue(adapter._looks_like_working("✻ Cogitating… (2m 4s · thinking with high effort)"))
        self.assertTrue(adapter._looks_like_working("✽ Bloviating… (1m 30s)"))
        self.assertTrue(adapter._looks_like_working("✻ Ruminating… (5s)"))

    def test_middle_dot_not_false_positive(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        # · (middle dot) appears in regular response content — must NOT match
        self.assertFalse(adapter._looks_like_working("· next steps..."))
        self.assertFalse(adapter._looks_like_working("· thinking... through tradeoffs"))

    def test_response_content_not_working(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        # Response content that mentions working indicators must NOT match
        self.assertFalse(adapter._looks_like_working(
            '- Then checks "esc to interrupt" on non-noise lines'
        ))
        # Code quotes containing ellipsis + working keywords must NOT match
        self.assertFalse(adapter._looks_like_working(
            'if "esc to interrupt" in sl and ("…" in s or "..." in s'
        ))

    def test_status_bar_not_working(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        # Status bar is skipped entirely (bottom-scan filters noise)
        self.assertFalse(adapter._looks_like_working(
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt   30987 tokens"
        ))

    def test_completed_not_working(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        # Post-thinking lines should NOT match
        self.assertFalse(adapter._looks_like_working("✻ Cogitated for 4m 4s"))
        self.assertFalse(adapter._looks_like_working("❯ "))
        self.assertFalse(adapter._looks_like_working("⏵⏵ bypass permissions on"))
        self.assertFalse(adapter._looks_like_working("────────────"))

    def test_completed_turn_with_stale_thinking_line_not_working(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        screen = (
            "❯ Reply with exactly: claude-dev-ok\n"
            "∴ Thinking…\n"
            "  The user is asking me to reply with exactly \"claude-dev-ok\".\n"
            "● claude-dev-ok\n"
            "────────────────────────────────────────\n"
            "❯ \n"
            "────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)   22466 tokens\n"
        )
        self.assertTrue(adapter._looks_like_completed_turn(
            screen, baseline_answered_turns=0, baseline_last_response=None,
        ))
        self.assertFalse(adapter._looks_like_working(screen))

    def test_empty_and_noise(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter()
        self.assertFalse(adapter._looks_like_working(""))
        self.assertFalse(adapter._looks_like_working("⏺ Read some file"))


class ScreenTailStabilityTest(unittest.TestCase):
    """Verify _screen_tail excludes status bar and separator noise."""

    def test_status_bar_excluded_from_tail(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        screen = (
            "⏺ Here is my analysis of the code.\n"
            "  The key issue is in the polling loop.\n"
            "────────────────────────────────────────\n"
            "❯ \n"
            "────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · 42146 tokens\n"
        )
        tail = ClaudeCodeAdapter._screen_tail(screen)
        # Status bar and separators should be excluded
        self.assertNotIn("⏵⏵", tail)
        self.assertNotIn("────", tail)
        # Content lines should be preserved
        self.assertIn("analysis", tail)
        self.assertIn("❯", tail)

    def test_token_count_change_not_in_tail(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter
        # Simulates two captures where only the token count changed
        screen1 = "❯ \n  ⏵⏵ bypass permissions on · 42146 tokens\n"
        screen2 = "❯ \n  ⏵⏵ bypass permissions on · 42200 tokens\n"
        self.assertEqual(
            ClaudeCodeAdapter._screen_tail(screen1),
            ClaudeCodeAdapter._screen_tail(screen2),
        )

    def test_wrapped_status_bar_continuation_excluded_from_tail_and_prompt_check(self) -> None:
        from agp.plugins.claude_code import ClaudeCodeAdapter

        screen1 = (
            "❯ hello\n"
            "● 你好！\n"
            "────────────────────────────────────────\n"
            "❯ \n"
            "  ⏵⏵ bypass permissions on\n"
            "  · esc to interrupt   42146 tokens\n"
        )
        screen2 = (
            "❯ hello\n"
            "● 你好！\n"
            "────────────────────────────────────────\n"
            "❯ \n"
            "  ⏵⏵ bypass permissions on\n"
            "  · esc to interrupt   42200 tokens\n"
        )

        self.assertEqual(
            ClaudeCodeAdapter._screen_tail(screen1),
            ClaudeCodeAdapter._screen_tail(screen2),
        )
        self.assertTrue(ClaudeCodeAdapter._visible_ends_with_prompt(screen1))

    def test_codex_noise_excluded_from_tail(self) -> None:
        from agp.plugins.codex import CodexAdapter
        screen = (
            "• Here is my response\n"
            "Working (30s • esc to interrupt)\n"
            "Token usage: 5000\n"
            "› \n"
        )
        tail = CodexAdapter._screen_tail(screen)
        self.assertNotIn("Working (", tail)
        self.assertNotIn("Token usage:", tail)
        self.assertIn("response", tail)
        self.assertIn("›", tail)


class HeartbeatAgeSecondsTest(unittest.TestCase):
    """Tests for the shared _heartbeat_age_seconds helper."""

    def test_returns_none_for_none(self) -> None:
        from agp.cli import _heartbeat_age_seconds
        self.assertIsNone(_heartbeat_age_seconds(None))

    def test_returns_none_for_empty_string(self) -> None:
        from agp.cli import _heartbeat_age_seconds
        self.assertIsNone(_heartbeat_age_seconds(""))

    def test_returns_positive_float_for_past_timestamp(self) -> None:
        from agp.cli import _heartbeat_age_seconds
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        age = _heartbeat_age_seconds(past)
        self.assertIsNotNone(age)
        self.assertGreater(age, 25)
        self.assertLess(age, 60)

    def test_handles_z_suffix(self) -> None:
        from agp.cli import _heartbeat_age_seconds
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = _heartbeat_age_seconds(past)
        self.assertIsNotNone(age)
        self.assertGreater(age, 5)

    def test_returns_none_for_garbage(self) -> None:
        from agp.cli import _heartbeat_age_seconds
        self.assertIsNone(_heartbeat_age_seconds("not-a-date"))


class DiagnoseAgentTest(unittest.TestCase):
    """Tests for the _diagnose_agent CLI code path."""

    @patch("agp.cli._make_client")
    def test_diagnose_agent_shows_agent_info(self, mock_make: MagicMock) -> None:
        from datetime import datetime, timezone, timedelta
        hb_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

        fake_client = MagicMock()
        fake_client.get_agent.return_value = {
            "agent_id": "agt_test",
            "status": "idle",
            "capabilities": ["code"],
            "workspace_ref": "/tmp/test",
            "created_at": "2026-04-01T00:00:00+00:00",
            "last_heartbeat_at": hb_time,
        }
        fake_client.ops_list_runtimes.return_value = {
            "items": [
                {
                    "runtime_id": "rtm-agt_test",
                    "agent_id": "agt_test",
                    "status": "idle",
                    "hostname": "localhost",
                }
            ]
        }
        fake_client.list_jobs.return_value = {"items": []}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, ["diagnose", "agent", "agt_test"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("agt_test", result.output)
        self.assertIn("idle", result.output)
        self.assertIn("rtm-agt_test", result.output)

    @patch("agp.cli._make_client")
    def test_diagnose_agent_404_exits_with_error(self, mock_make: MagicMock) -> None:
        import httpx
        fake_client = MagicMock()
        resp = httpx.Response(404, request=httpx.Request("GET", "http://x"))
        fake_client.get_agent.side_effect = httpx.HTTPStatusError(
            "not found", request=resp.request, response=resp
        )

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, ["diagnose", "agent", "agt_missing"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not found", result.output)

    @patch("agp.cli._make_client")
    def test_diagnose_agent_json_output(self, mock_make: MagicMock) -> None:
        fake_client = MagicMock()
        fake_client.get_agent.return_value = {
            "agent_id": "agt_test",
            "status": "idle",
            "capabilities": [],
            "created_at": "2026-04-01T00:00:00+00:00",
            "last_heartbeat_at": None,
        }
        fake_client.ops_list_runtimes.return_value = {"items": []}
        fake_client.list_jobs.return_value = {"items": []}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, ["diagnose", "agent", "agt_test", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["agent"]["agent_id"], "agt_test")
        self.assertIsNone(data["runtime"])

    @patch("agp.cli._make_client")
    def test_diagnose_agent_finds_runtime_by_agent_id(self, mock_make: MagicMock) -> None:
        """Runtime with a non-standard ID is found by agent_id, not name prefix."""
        fake_client = MagicMock()
        fake_client.get_agent.return_value = {
            "agent_id": "reviewer",
            "status": "busy",
            "capabilities": ["review"],
            "created_at": "2026-04-01T00:00:00+00:00",
            "last_heartbeat_at": None,
        }
        fake_client.ops_list_runtimes.return_value = {
            "items": [
                {
                    "runtime_id": "custom-runtime-xyz",
                    "agent_id": "reviewer",
                    "status": "busy",
                    "hostname": "worker-3",
                }
            ]
        }
        fake_client.list_jobs.return_value = {"items": []}

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_make.return_value = ctx

        result = runner.invoke(app, ["diagnose", "agent", "reviewer", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["runtime"]["runtime_id"], "custom-runtime-xyz")
