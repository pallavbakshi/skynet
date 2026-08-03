"""Tests for the via-file prompt delivery module."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from agp.plugins._via_file import (
    build_task_content,
    cleanup_stale_task_files,
    cleanup_task_file,
    reference_string,
    write_task_file,
)


class BuildTaskContentTest(unittest.TestCase):
    """Tests for build_task_content() — returns (prompt, sections) tuple."""

    def _minimal_claimed(self, **overrides: object) -> dict:
        base = {
            "agent_id": "agt_test",
            "job": {"job_id": "job_123"},
            "run": {"run_id": "run_456"},
            "message": {"text": "do something"},
        }
        base.update(overrides)
        return base

    def test_returns_prompt_and_sections_tuple(self) -> None:
        prompt, sections = build_task_content(
            prompt="hello world",
            claimed=self._minimal_claimed(),
        )
        self.assertEqual(prompt, "hello world")
        self.assertIsInstance(sections, str)

    def test_prompt_is_returned_unchanged(self) -> None:
        prompt, _ = build_task_content(
            prompt="hello world",
            claimed=self._minimal_claimed(),
        )
        self.assertEqual(prompt, "hello world")
        # BEGIN TASK / END TASK markers are added by smallops, not by us
        self.assertNotIn("BEGIN TASK", prompt)

    def test_sections_contain_metadata(self) -> None:
        _, sections = build_task_content(
            prompt="hello world",
            claimed=self._minimal_claimed(),
        )
        self.assertIn("## Metadata", sections)
        self.assertIn("run_456", sections)
        self.assertIn("job_123", sections)
        self.assertIn("agt_test", sections)

    def test_sections_contain_output_contract(self) -> None:
        claimed = self._minimal_claimed(
            job={
                "job_id": "job_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["verdict"],
                        "properties": {"verdict": {"type": "string"}},
                    },
                },
            },
        )
        _, sections = build_task_content(prompt="review this", claimed=claimed)
        self.assertIn("## Output Contract", sections)
        self.assertIn('"verdict"', sections)
        self.assertIn("valid JSON matching this schema", sections)

    def test_sections_contain_conversation_id(self) -> None:
        claimed = self._minimal_claimed(
            message={"text": "task", "conversation_id": "conv_abc"},
        )
        _, sections = build_task_content(prompt="task", claimed=claimed)
        self.assertIn("conv_abc", sections)

    def test_sections_contain_attachments(self) -> None:
        attachments = [
            {"name": "readme.md", "role": "context", "staged_path": "/workspace/.agp-tmp/readme.md"},
            {"name": "data.json", "role": "input"},
        ]
        _, sections = build_task_content(
            prompt="process these",
            claimed=self._minimal_claimed(),
            attachments=attachments,
        )
        self.assertIn("## Attachments", sections)
        self.assertIn("readme.md", sections)
        self.assertIn("/workspace/.agp-tmp/readme.md", sections)
        self.assertIn("data.json", sections)

    def test_sections_contain_context_messages(self) -> None:
        claimed = self._minimal_claimed(
            context_messages=[
                {"role": "user", "text": "first message"},
                {"role": "assistant", "text": "first response"},
            ],
        )
        _, sections = build_task_content(prompt="follow up", claimed=claimed)
        self.assertIn("## Conversation Context", sections)
        self.assertIn("first message", sections)
        self.assertIn("first response", sections)

    def test_no_optional_sections_when_absent(self) -> None:
        prompt, sections = build_task_content(
            prompt="simple task",
            claimed={"job": {}, "run": {}, "message": {"text": "t"}},
        )
        self.assertEqual(prompt, "simple task")
        self.assertEqual(sections, "")

    def test_parent_job_included(self) -> None:
        claimed = self._minimal_claimed(
            job={"job_id": "job_child", "parent_job_id": "job_parent"},
        )
        _, sections = build_task_content(prompt="sub-task", claimed=claimed)
        self.assertIn("job_parent", sections)


class WriteAndCleanupTest(unittest.TestCase):
    """Tests for write_task_file, cleanup_task_file, reference_string."""

    def test_write_and_read_back(self) -> None:
        path = write_task_file(run_id="test_write_001", content="hello via-file")
        try:
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).read_text(), "hello via-file")
            self.assertIn("agp-task-test_write_001.md", path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_reference_string_format(self) -> None:
        ref = reference_string("/tmp/agp-tasks-501/agp-task-run_abc.md")
        self.assertEqual(
            ref,
            "Read the file /tmp/agp-tasks-501/agp-task-run_abc.md and follow the instructions inside.",
        )

    def test_cleanup_task_file(self) -> None:
        path = write_task_file(run_id="test_cleanup_001", content="to be cleaned")
        self.assertTrue(Path(path).exists())
        result = cleanup_task_file("test_cleanup_001")
        self.assertTrue(result)
        self.assertFalse(Path(path).exists())

    def test_cleanup_missing_file_is_ok(self) -> None:
        result = cleanup_task_file("nonexistent_run_id")
        self.assertTrue(result)

    def test_cleanup_stale_files(self) -> None:
        # Write a file and set its mtime to 1 hour ago
        path = write_task_file(run_id="test_stale_001", content="stale content")
        try:
            import time
            old_time = time.time() - 3600
            os.utime(path, (old_time, old_time))
            cleaned = cleanup_stale_task_files(max_age_seconds=1800)
            self.assertGreaterEqual(cleaned, 1)
            self.assertFalse(Path(path).exists())
        finally:
            Path(path).unlink(missing_ok=True)

    def test_fresh_files_not_cleaned(self) -> None:
        path = write_task_file(run_id="test_fresh_001", content="fresh content")
        try:
            cleanup_stale_task_files(max_age_seconds=1800)
            # The fresh file should NOT be cleaned
            self.assertTrue(Path(path).exists())
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
