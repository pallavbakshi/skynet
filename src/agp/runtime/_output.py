"""Pure utilities for terminal output processing."""

from __future__ import annotations

import re
from pathlib import Path


class _OutputAccumulator:
    """Append-only output log for durable session transcript capture.

    Persists all deltas to a file so the full transcript is available even
    when the terminal scrollback buffer shifts.  The file survives runtime
    restarts — on reload the prior content is recovered automatically.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[str] = []
        if path.exists():
            self._buffer = [path.read_text()]

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._buffer.append(delta)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as fh:
            fh.write(delta)

    @property
    def text(self) -> str:
        return "".join(self._buffer)

    def reset(self) -> None:
        self._buffer = []
        if self._path.exists():
            self._path.unlink()


def _compute_output_delta(current_text: str, prior_text: str) -> str:
    """Compute new output since the last read, surviving scrollback shifts.

    Strategy:
    1. Fast path — prior is a prefix of current (buffer did not shift).
    2. Slow path — find trailing-line anchors from prior in current.
    3. Fallback — return all of current (data gap, best effort).
    """
    if not prior_text:
        return current_text
    if not current_text:
        return ""
    if current_text.startswith(prior_text):
        return current_text[len(prior_text):]

    prior_lines = prior_text.splitlines()
    current_lines = current_text.splitlines()
    if not prior_lines:
        return current_text

    for anchor_size in (20, 10, 5, 3, 2):
        if anchor_size > len(prior_lines):
            continue
        anchor = prior_lines[-anchor_size:]
        for i in range(len(current_lines) - anchor_size + 1):
            if current_lines[i : i + anchor_size] == anchor:
                new_start = i + anchor_size
                if new_start >= len(current_lines):
                    return ""
                return "\n".join(current_lines[new_start:]) + "\n"

    return current_text


_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;]*[A-Za-z]"  # CSI sequences
    r"|\][^\x07]*\x07"  # OSC sequences (terminated by BEL)
    r"|\][^\x1b]*\x1b\\"  # OSC sequences (terminated by ST)
    r"|[^[\]][^\x1b]?"  # two-char escapes
    r")",
    re.DOTALL,
)

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_RE.sub("", text)
