"""Shared utilities: ANSI stripping, via-file delivery, output delta."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

# ── Shell detection ──────────────────────────────────────────────────

_SHELL_NAMES = frozenset({"bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh", "nu", "pwsh"})


def is_shell_foreground(tty: str) -> bool:
    """Check if the foreground process on tty is a shell."""
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-t", tty.removeprefix("/dev/")],
            capture_output=True, text=True, check=False,
        )
    except Exception:
        return False

    if result.returncode != 0:
        return False

    for line in reversed(result.stdout.strip().splitlines()):
        name = line.strip().lstrip("-").split("/")[-1].lower()
        if name in _SHELL_NAMES:
            return True
        if name:
            return False
    return False

# ── ANSI escape removal ─────────────────────────────────────────────

_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;]*[A-Za-z]"       # CSI sequences
    r"|\][^\x07]*\x07"         # OSC sequences (BEL terminated)
    r"|\][^\x1b]*\x1b\\"      # OSC sequences (ST terminated)
    r"|[^[\]][^\x1b]?"         # two-char escapes
    r")",
    re.DOTALL,
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_RE.sub("", text)


# ── Via-file prompt delivery ─────────────────────────────────────────

_REFERENCE_TEMPLATE = (
    "Read the file {path}. Execute only the task text between "
    "BEGIN TASK and END TASK exactly; do not summarize or restate it. "
    "Respond with only the task's requested output, and do not mention "
    "this instruction, the file path, or the BEGIN TASK / END TASK markers."
)


def write_via_file(
    prompt: str,
    *,
    file: str | None = None,
    sections: str | None = None,
    directory: str = "/tmp/smallops",
) -> tuple[str, str]:
    """Write prompt to a task file.

    Args:
        prompt: The task text. Ignored if ``file`` is provided.
        file: Path to an existing file whose content replaces ``prompt``.
              The file is read and its content is embedded in our own task
              file (with our own marker filename). The original file is
              not modified.
        sections: Optional extra markdown sections (metadata, attachments,
                  context) appended after the task block.
        directory: Directory for the task file.

    Returns (reference_string, file_path).
    The reference string is short enough to paste safely into any TUI
    and doubles as a marker to locate the response in output.
    """
    dir_path = Path(directory)
    dir_path.mkdir(mode=0o700, exist_ok=True)

    # Validate ownership
    if dir_path.stat().st_uid != os.getuid():
        raise RuntimeError(f"{directory} is owned by another user")

    # Read content from file if provided
    if file is not None:
        prompt = Path(file).read_text(encoding="utf-8")

    file_id = uuid4().hex[:12]
    path = dir_path / f"task-{file_id}.md"

    content = f"# Task\n\nBEGIN TASK\n{prompt}\nEND TASK\n"
    if sections:
        content += f"\n{sections}\n"

    # Atomic write: temp file then rename
    fd, tmp_path = tempfile.mkstemp(prefix="task-", suffix=".md.tmp", dir=str(dir_path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    ref = _REFERENCE_TEMPLATE.format(path=path)
    return ref, str(path)


def cleanup_via_file(path: str) -> None:
    """Remove a task file."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ── Nudge file delivery ────────────────────────────────────────────

_NUDGE_REFERENCE_TEMPLATE = (
    "Read the file {path} and follow the instructions inside."
)


def write_nudge_file(text: str, *, directory: str = "/tmp/smallops") -> tuple[str, str]:
    """Write nudge text to a plain file (no BEGIN TASK / END TASK framing).

    Returns (reference_string, file_path).
    """
    dir_path = Path(directory)
    dir_path.mkdir(mode=0o700, exist_ok=True)

    if dir_path.stat().st_uid != os.getuid():
        raise RuntimeError(f"{directory} is owned by another user")

    file_id = uuid4().hex[:8]
    path = dir_path / f"nudge-{file_id}.md"

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(prefix="nudge-", suffix=".md.tmp", dir=str(dir_path))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    ref = _NUDGE_REFERENCE_TEMPLATE.format(path=path)
    return ref, str(path)



# ── Screen normalization ─────────────────────────────────────────────

def normalize_screen(raw: str) -> str:
    """Normalize line endings and strip trailing blanks."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
