"""Helpers for protecting local SQLite state during bare-metal workflows."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


DEFAULT_CONTROL_PLANE_PID_FILE = Path(".skyops-pids/control-plane.pid")


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str | None:
    """Return the command string for *pid*, or ``None`` if lookup fails."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, PermissionError):
        return None
    return proc.stdout.strip() or None


def _process_cwd(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        return proc_cwd.resolve()
    except OSError:
        proc = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            if not line.startswith("n"):
                continue
            try:
                return Path(line[1:]).resolve()
            except OSError:
                return None
        return None


def _looks_like_control_plane_command(command: str) -> bool:
    normalized = " ".join(command.split()).lower()
    if "serve" not in normalized:
        return False
    indicators = (
        " agp serve",
        " agp.cli serve",
        " -m agp.cli serve",
        "/agp serve",
    )
    return any(indicator in f" {normalized}" for indicator in indicators)


def _is_local_control_plane_process(pid: int, *, safe_default: bool = True) -> bool:
    """Check whether *pid* looks like a local control-plane process.

    When *safe_default* is True (used for tracked PIDs from the pid file),
    an uninspectable process is assumed to be a CP — safe-by-default.
    When False (used for discovery via the global ps scan), lack of evidence
    means the process is not treated as a CP.
    """
    if not _pid_exists(pid):
        return False
    command = _process_command(pid)
    if command is None:
        return safe_default
    return _looks_like_control_plane_command(command)


def _candidate_control_plane_pids(*, root: Path, pid_file: Path) -> list[int]:
    candidates: list[int] = []
    tracked_pid = _read_pid(pid_file)
    if tracked_pid is not None and _is_local_control_plane_process(tracked_pid):
        candidates.append(tracked_pid)

    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, PermissionError):
        return candidates
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, _command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if not _looks_like_control_plane_command(_command):
            continue
        if pid in candidates or not _is_local_control_plane_process(pid, safe_default=False):
            continue
        proc_root = _process_cwd(pid)
        if proc_root == root:
            candidates.append(pid)
    return candidates


def stop_local_control_plane(
    pid_file: str | Path = DEFAULT_CONTROL_PLANE_PID_FILE,
    *,
    root: str | Path | None = None,
    timeout_seconds: float = 5.0,
) -> list[int]:
    """Stop matching local control-plane processes for this worktree.

    Returns the list of PIDs that were signalled. Best effort only: stale pid
    files are removed and already-exited processes are ignored.
    """
    pid_path = Path(pid_file)
    repo_root = Path(root or Path.cwd()).resolve()
    pids = _candidate_control_plane_pids(root=repo_root, pid_file=pid_path)
    if not pids:
        return []

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        remaining = [pid for pid in pids if _pid_exists(pid)]
        if not remaining:
            break
        time.sleep(0.1)

    remaining = [pid for pid in pids if _pid_exists(pid)]
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue

    try:
        pid_path.unlink()
    except OSError:
        pass
    return pids


def ensure_local_control_plane_stopped(pid_file: str | Path = DEFAULT_CONTROL_PLANE_PID_FILE, *, root: str | Path | None = None) -> None:
    """Refuse destructive local-state resets while a matching local CP is still running."""
    pid_path = Path(pid_file)
    repo_root = Path(root or Path.cwd()).resolve()
    pids = _candidate_control_plane_pids(root=repo_root, pid_file=pid_path)
    if pids:
        pid_list = ", ".join(str(pid) for pid in pids)
        raise RuntimeError(
            f"local control plane is still running (pid {pid_list}); "
            "stop it with `make local-down` or `make stop-cp` before resetting local state"
        )
