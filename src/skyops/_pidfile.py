"""PID file management for bare-metal service processes."""
import os
import signal
import time
from pathlib import Path


def pid_dir(config_dir: Path | None = None) -> Path:
    """Return (and create) the PID directory."""
    base = config_dir or Path.cwd()
    d = base / ".skyops-pids"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pidfile(pid_directory: Path, label: str, pid: int) -> Path:
    p = pid_directory / f"{label}.pid"
    p.write_text(str(pid))
    return p


def read_pidfile(pid_directory: Path, label: str) -> int | None:
    p = pid_directory / f"{label}.pid"
    if not p.exists():
        return None
    pid = int(p.read_text().strip())
    try:
        os.kill(pid, 0)  # check if alive
        return pid
    except OSError:
        p.unlink(missing_ok=True)
        return None


def remove_pidfile(pid_directory: Path, label: str) -> None:
    (pid_directory / f"{label}.pid").unlink(missing_ok=True)


def list_pidfiles(pid_directory: Path) -> dict[str, int]:
    result = {}
    if not pid_directory.exists():
        return result
    for p in pid_directory.glob("*.pid"):
        label = p.stem
        pid = read_pidfile(pid_directory, label)
        if pid is not None:
            result[label] = pid
    return result


def signal_and_wait(pid_directory: Path, label: str, timeout: float = 5.0) -> bool:
    pid = read_pidfile(pid_directory, label)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        remove_pidfile(pid_directory, label)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.2)
        except OSError:
            remove_pidfile(pid_directory, label)
            return True
    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    remove_pidfile(pid_directory, label)
    return True
