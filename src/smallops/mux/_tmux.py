"""Tmux Mux implementation."""

from __future__ import annotations

import subprocess
from time import monotonic, sleep

from smallops._types import SessionInfo
from smallops._util import is_shell_foreground


class TmuxMux:
    """Terminal multiplexer backed by tmux.

    Each session gets a dedicated detached tmux session.
    Uses absolute line tracking (history_size + cursor_y) for reliable
    output capture that survives scrollback shifts.
    """

    kind = "tmux"

    def __init__(
        self,
        *,
        bin: str = "tmux",
        prefix: str = "smallops",
        scrollback: int = 5000,
    ) -> None:
        self._bin = bin
        self._prefix = prefix
        self._scrollback = scrollback

    # ── Internal helpers ─────────────────────────────────────────────

    def _run(self, args: list[str], *, allow_failure: bool = False) -> str:
        result = subprocess.run(
            [self._bin, *args],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 and not allow_failure:
            raise RuntimeError(f"tmux {' '.join(args)}: {(result.stderr or '').strip()}")
        return result.stdout or ""

    def _session_name(self, name: str) -> str:
        return f"{self._prefix}-{name}"

    def _session_exists_raw(self, sid: str) -> bool:
        r = subprocess.run(
            [self._bin, "has-session", "-t", sid],
            capture_output=True, text=True, check=False,
        )
        return r.returncode == 0

    # ── Mux protocol ─────────────────────────────────────────────────

    def create_session(self, *, name: str, cwd: str | None = None) -> SessionInfo:
        sid = self._session_name(name)
        if self._session_exists_raw(sid):
            actual_cwd = cwd
            if not actual_cwd:
                try:
                    actual_cwd = self._run(
                        ["display-message", "-t", sid, "-p", "#{pane_current_path}"]
                    ).strip() or None
                except RuntimeError:
                    pass
            return SessionInfo(id=sid, name=name, cwd=actual_cwd)

        args = ["new-session", "-d", "-s", sid, "-x", "200", "-y", "50"]
        if cwd:
            args.extend(["-c", cwd])
        self._run(args)
        self._run(["set-option", "-t", sid, "history-limit", str(self._scrollback)])
        self._run(["set-window-option", "-t", sid, "remain-on-exit", "on"])
        sleep(0.3)  # let tmux fully register the pane
        return SessionInfo(id=sid, name=name, cwd=cwd)

    def destroy_session(self, session: SessionInfo) -> None:
        self._run(["kill-session", "-t", session.id], allow_failure=True)

    def session_exists(self, session: SessionInfo) -> bool:
        if not self._session_exists_raw(session.id):
            return False
        dead = self._run(
            ["display-message", "-t", session.id, "-p", "#{pane_dead}"],
            allow_failure=True,
        ).strip()
        return dead != "1"

    def send_text(self, session: SessionInfo, text: str, *, enter: bool = True) -> None:
        if text == "\x1b[B":
            self._run(["send-keys", "-t", session.id, "Down"])
        elif text:
            self._run(["send-keys", "-t", session.id, "-l", text])
        if enter:
            if text:
                # Codex has paste-burst detection with a 120ms Enter suppression
                # window. After sending text as a burst, we must wait >120ms
                # before Enter, otherwise Enter is treated as a newline.
                sleep(0.50 if "\n" in text else 0.15)
            self._run(["send-keys", "-t", session.id, "Enter"])

    def peek(self, session: SessionInfo, n: int | None = None) -> str:
        if n is not None and n > 0:
            return self._run(["capture-pane", "-t", session.id, "-p", "-S", str(-n)])
        return self._run(["capture-pane", "-t", session.id, "-p"])

    def shell_idle(self, session: SessionInfo) -> bool:
        tty = self._run(
            ["display-message", "-t", session.id, "-p", "#{pane_tty}"],
            allow_failure=True,
        ).strip()
        if not tty:
            return False
        return is_shell_foreground(tty)

    def respawn(self, session: SessionInfo, command: str, *, env: dict[str, str] | None = None) -> SessionInfo:
        """Replace the pane's process with command. No TTY echo."""
        args = ["respawn-pane", "-k", "-t", session.id]
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        if session.cwd:
            args.extend(["-c", session.cwd])
        args.append(command)
        self._run(args)
        sleep(0.3)  # let tmux settle after respawn
        return session  # tmux keeps the same pane ID

    def interrupt(self, session: SessionInfo) -> None:
        self._run(["send-keys", "-t", session.id, "C-c"])
        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            if self.shell_idle(session):
                return
            sleep(0.05)
