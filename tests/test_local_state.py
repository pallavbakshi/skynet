"""Tests for local bare-metal state safety guards."""

from __future__ import annotations

import unittest
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agp._local_state import (
    _looks_like_control_plane_command,
    _process_cwd,
    ensure_local_control_plane_stopped,
    stop_local_control_plane,
)
from agp.config import settings
from tests._base import _reset_sqlite_database


class LocalControlPlaneGuardTest(unittest.TestCase):
    def test_recognizes_alternative_control_plane_entrypoints(self) -> None:
        self.assertTrue(_looks_like_control_plane_command("uv run agp serve"))
        self.assertTrue(_looks_like_control_plane_command("python -m agp.cli serve"))
        self.assertFalse(_looks_like_control_plane_command("python -m agp.cli status"))

    def test_rejects_reset_while_local_control_plane_is_running(self) -> None:
        with TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "control-plane.pid"
            pid_file.write_text("12345\n", encoding="utf-8")

            with patch("agp._local_state._pid_exists", return_value=True), \
                 patch("agp._local_state._process_command", return_value="python -m agp.cli serve"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_local_control_plane_stopped(pid_file)

        self.assertIn("make local-down", str(ctx.exception))

    def test_local_initdb_stops_before_agp_initdb_when_guard_fails(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            agp_log = root / "agp-invocation.txt"

            (bin_dir / "python").write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "cat >&2 <<'EOF'",
                        "RuntimeError: local control plane is still running (pid 12345); stop it with `make local-down` or `make stop-cp` before resetting local state",
                        "EOF",
                        "exit 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (bin_dir / "python").chmod(0o755)
            (bin_dir / "agp").write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        f'printf "%s\\n" "$@" > "{agp_log}"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (bin_dir / "agp").chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            }
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "-f",
                    str(Path(__file__).resolve().parents[1] / "Makefile"),
                    "RUN=",
                    f"ROOT={root}",
                    "local-initdb",
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("make local-down", result.stderr)
        self.assertIn("local control plane is still running", result.stderr)
        self.assertFalse(agp_log.exists())

    def test_reports_running_pid_when_pidfile_matches_live_control_plane(self) -> None:
        with TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "control-plane.pid"
            pid_file.write_text("12345\n", encoding="utf-8")

            with patch("agp._local_state._pid_exists", return_value=True), \
                 patch("agp._local_state._process_command", return_value="python -m agp.cli serve"):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_local_control_plane_stopped(pid_file)

        self.assertIn("pid 12345", str(ctx.exception))

    def test_ignores_live_non_control_plane_processes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "control-plane.pid"
            pid_file.write_text("12345\n", encoding="utf-8")

            with patch("agp._local_state._pid_exists", return_value=True), \
                 patch("agp._local_state._process_command", return_value="sleep 30"):
                ensure_local_control_plane_stopped(pid_file)

    def test_rejects_repo_local_control_plane_without_pidfile(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pid_file = root / "missing.pid"

            with patch("agp._local_state._candidate_control_plane_pids", return_value=[54321]):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_local_control_plane_stopped(pid_file, root=root)

        self.assertIn("54321", str(ctx.exception))

    def test_ignores_control_plane_from_other_worktree(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pid_file = root / "control-plane.pid"

            with patch("agp._local_state._candidate_control_plane_pids", return_value=[]):
                ensure_local_control_plane_stopped(pid_file, root=root)

    def test_stop_local_control_plane_signals_matching_processes_and_clears_pidfile(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pid_file = root / "control-plane.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            seen: list[tuple[int, int]] = []

            def fake_kill(pid: int, sig: int) -> None:
                seen.append((pid, sig))

            with patch("agp._local_state._candidate_control_plane_pids", return_value=[12345]), \
                 patch("agp._local_state._pid_exists", return_value=False), \
                 patch("agp._local_state.os.kill", side_effect=fake_kill):
                stopped = stop_local_control_plane(pid_file, root=root, timeout_seconds=0.0)

        self.assertEqual(stopped, [12345])
        self.assertEqual(seen, [(12345, 15)])
        self.assertFalse(pid_file.exists())

    def test_process_cwd_falls_back_to_lsof_when_proc_unavailable(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "p123\nn/tmp/example\n"})()
        real_resolve = Path.resolve

        def fake_resolve(path_obj: Path, *args, **kwargs):
            if str(path_obj) == "/proc/123/cwd":
                raise OSError
            return real_resolve(path_obj, *args, **kwargs)

        with patch("pathlib.Path.resolve", new=fake_resolve), \
             patch("subprocess.run", return_value=completed):
            self.assertEqual(_process_cwd(123), Path("/tmp/example").resolve())

    def test_reset_sqlite_database_checks_local_control_plane_before_touching_repo_db(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        original_database_url = settings.database_url
        settings.database_url = f"sqlite+pysqlite:///{repo_root / 'agp.db'}"
        try:
            with patch("tests._base.ensure_local_control_plane_stopped") as mock_guard:
                _reset_sqlite_database()
        finally:
            settings.database_url = original_database_url

        mock_guard.assert_called_once_with(repo_root / ".skyops-pids" / "control-plane.pid", root=repo_root)
