"""Tests for the via-file prompt delivery module."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from agp.plugins.claude_code._via_file import (
    build_task_file_content,
    cleanup_stale_task_files,
    cleanup_task_file,
    reference_string,
    write_task_file,
)


class BuildTaskFileContentTest(unittest.TestCase):
    """Tests for build_task_file_content()."""

    def _minimal_claimed(self, **overrides: object) -> dict:
        base = {
            "agent_id": "agt_test",
            "job": {"job_id": "job_123"},
            "run": {"run_id": "run_456"},
            "message": {"text": "do something"},
        }
        base.update(overrides)
        return base

    def test_minimal_prompt(self) -> None:
        content = build_task_file_content(
            prompt="hello world",
            claimed=self._minimal_claimed(),
        )
        self.assertIn("# AGP Task", content)
        self.assertIn("## Task", content)
        self.assertIn("hello world", content)
        self.assertIn("run_456", content)
        self.assertIn("job_123", content)
        self.assertIn("agt_test", content)

    def test_includes_output_contract(self) -> None:
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
        content = build_task_file_content(prompt="review this", claimed=claimed)
        self.assertIn("## Output Contract", content)
        self.assertIn('"verdict"', content)
        self.assertIn("valid JSON matching this schema", content)

    def test_includes_conversation_id(self) -> None:
        claimed = self._minimal_claimed(
            message={"text": "task", "conversation_id": "conv_abc"},
        )
        content = build_task_file_content(prompt="task", claimed=claimed)
        self.assertIn("conv_abc", content)

    def test_includes_attachments_with_staged_paths(self) -> None:
        attachments = [
            {"name": "readme.md", "role": "context", "staged_path": "/workspace/.agp-tmp/readme.md"},
            {"name": "data.json", "role": "input"},
        ]
        content = build_task_file_content(
            prompt="process these",
            claimed=self._minimal_claimed(),
            attachments=attachments,
        )
        self.assertIn("## Attachments", content)
        self.assertIn("readme.md", content)
        self.assertIn("/workspace/.agp-tmp/readme.md", content)
        self.assertIn("data.json", content)

    def test_includes_context_messages(self) -> None:
        claimed = self._minimal_claimed(
            context_messages=[
                {"role": "user", "text": "first message"},
                {"role": "assistant", "text": "first response"},
            ],
        )
        content = build_task_file_content(prompt="follow up", claimed=claimed)
        self.assertIn("## Conversation Context", content)
        self.assertIn("first message", content)
        self.assertIn("first response", content)

    def test_no_optional_sections_when_absent(self) -> None:
        content = build_task_file_content(
            prompt="simple task",
            claimed=self._minimal_claimed(),
        )
        self.assertNotIn("## Attachments", content)
        self.assertNotIn("## Conversation Context", content)
        self.assertNotIn("## Output Contract", content)

    def test_parent_job_included(self) -> None:
        claimed = self._minimal_claimed(
            job={"job_id": "job_child", "parent_job_id": "job_parent"},
        )
        content = build_task_file_content(prompt="sub-task", claimed=claimed)
        self.assertIn("job_parent", content)


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
            cleaned = cleanup_stale_task_files(max_age_seconds=1800)
            # The fresh file should NOT be cleaned
            self.assertTrue(Path(path).exists())
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
