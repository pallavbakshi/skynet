"""Tmux terminal host plugin."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from agp.config import settings
from agp.runtime import (
    OutputCursor,
    OutputReadResult,
    SessionHealth,
    TerminalHost,
    TerminalSession,
    _OutputAccumulator,
    _compute_output_delta,
    _strip_ansi,
)


class TmuxHost(TerminalHost):
    """Terminal host backed by tmux sessions.

    Each agent gets a dedicated tmux session named ``agp-<agent_id>``.
    Sessions are created detached and persist across terminal disconnects.
    """

    def __init__(
        self,
        *,
        tmux_bin: str = "tmux",
        session_prefix: str = "agp",
        runner: Any | None = None,
        scrollback_lines: int = 5000,
        checkpoint_dir: Path | str | None = None,
        default_cwd: str = "",
    ) -> None:
        self.tmux_bin = tmux_bin
        self.session_prefix = session_prefix
        self._runner = runner or subprocess.run
        self.scrollback_lines = scrollback_lines
        self.checkpoint_dir = (
            Path(checkpoint_dir) if checkpoint_dir else settings.output_checkpoint_dir
        )
        self.default_cwd = default_cwd or getattr(settings, "tmux_default_cwd", "") or ""
        self._accumulators: dict[str, _OutputAccumulator] = {}

    def _get_accumulator(self, session: TerminalSession) -> _OutputAccumulator:
        if session.session_id not in self._accumulators:
            path = self.checkpoint_dir / f"session-{session.session_id}.output.txt"
            self._accumulators[session.session_id] = _OutputAccumulator(path)
        return self._accumulators[session.session_id]

    @property
    def kind(self) -> str:
        return "tmux"

    def _run(self, args: list[str], *, allow_failure: bool = False) -> str:
        completed = self._runner(
            [self.tmux_bin, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 and not allow_failure:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(f"tmux command failed: {' '.join(args)} :: {stderr}")
        return completed.stdout or ""

    def _session_name(self, agent_id: str) -> str:
        return f"{self.session_prefix}-{agent_id}"

    def _session_exists_raw(self, name: str) -> bool:
        result = self._runner(
            [self.tmux_bin, "has-session", "-t", name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def get_or_create_session(
        self, *, agent_id: str, workspace_ref: str | None = None
    ) -> TerminalSession:
        name = self._session_name(agent_id)
        if self._session_exists_raw(name):
            return TerminalSession(
                session_id=name,
                agent_id=agent_id,
                workspace_ref=workspace_ref,
                metadata={"tmux_session": name},
            )
        cwd = workspace_ref or self.default_cwd
        args = ["new-session", "-d", "-s", name, "-x", "200", "-y", "50"]
        if cwd:
            args.extend(["-c", cwd])
        self._run(args)
        # Forward environment variables that agent adapters may need
        # (tmux new-session does not inherit the parent's full env).
        for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                     "ANTHROPIC_API_KEY", "AGP_SERVER_URL"):
            val = os.environ.get(key)
            if val:
                try:
                    self._run(["set-environment", "-t", name, key, val], allow_failure=True)
                except Exception:
                    pass  # best-effort; test mocks may not support set-environment
        return TerminalSession(
            session_id=name,
            agent_id=agent_id,
            workspace_ref=workspace_ref,
            metadata={"tmux_session": name},
        )

    def send_text(
        self, session: TerminalSession, text: str, *, enter: bool = True
    ) -> None:
        # Use -l (literal) for the text to prevent tmux interpreting key names,
        # then send Enter as a separate key event.
        if text:
            self._run(["send-keys", "-t", session.session_id, "-l", text])
        if enter:
            self._run(["send-keys", "-t", session.session_id, "Enter"])

    # ── Absolute line tracking via tmux format variables ────────────

    def _absolute_line(self, session: TerminalSession) -> int:
        """Return the cursor's absolute line (history_size + cursor_y).

        This is a monotonically increasing position that survives scrollback
        shifts — the key advantage tmux has over heuristic text-diffing.
        """
        hist = self._run(["display-message", "-t", session.session_id, "-p", "#{history_size}"]).strip()
        cur_y = self._run(["display-message", "-t", session.session_id, "-p", "#{cursor_y}"]).strip()
        return int(hist) + int(cur_y)

    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        abs_line = self._absolute_line(session)
        return OutputCursor(
            session_id=session.session_id,
            checkpoint="",
            metadata={"absolute_line": abs_line, "line_count": 0},
        )

    def _capture_from(self, session: TerminalSession, absolute_start: int) -> str:
        """Capture pane text from an absolute line position onward."""
        hist = int(self._run(
            ["display-message", "-t", session.session_id, "-p", "#{history_size}"],
        ).strip())
        start_offset = absolute_start - hist
        return self._run([
            "capture-pane", "-t", session.session_id, "-p", "-J",
            "-S", str(start_offset),
        ])

    def _capture_full(self, session: TerminalSession) -> str:
        """Capture full pane content including scrollback."""
        return self._run([
            "capture-pane", "-t", session.session_id, "-p",
            "-S", str(-self.scrollback_lines),
        ])

    def read_output(
        self, session: TerminalSession, cursor: OutputCursor
    ) -> OutputReadResult:
        mark = cursor.metadata.get("absolute_line")
        if mark is not None:
            delta = self._capture_from(session, mark)
        else:
            raw = self._capture_full(session)
            delta = _compute_output_delta(raw, cursor.checkpoint)

        accumulator = self._get_accumulator(session)
        accumulator.append(delta)
        abs_line = self._absolute_line(session)
        updated = OutputCursor(
            session_id=session.session_id,
            checkpoint="",
            metadata={
                "absolute_line": abs_line,
                "line_count": cursor.metadata.get("line_count", 0) + delta.count("\n"),
            },
        )
        self._save_cursor(session, updated)
        return OutputReadResult(
            session_id=session.session_id,
            cursor=updated,
            text=delta,
            full_text=accumulator.text,
            changed=bool(delta.strip()),
        )

    def _save_cursor(self, session: TerminalSession, cursor: OutputCursor) -> None:
        path = self.checkpoint_dir / f"cursor-{session.session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": session.session_id,
            "absolute_line": cursor.metadata.get("absolute_line", 0),
            "line_count": cursor.metadata.get("line_count", 0),
        }, sort_keys=True))

    def load_cursor(self, session: TerminalSession) -> OutputCursor | None:
        """Load a persisted cursor from a previous runtime process."""
        path = self.checkpoint_dir / f"cursor-{session.session_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return OutputCursor(
            session_id=session.session_id,
            checkpoint="",
            metadata={
                "absolute_line": data.get("absolute_line", data.get("line_count", 0)),
                "line_count": data.get("line_count", 0),
                "restored": True,
            },
        )

    def interrupt(self, session: TerminalSession) -> None:
        self._run(["send-keys", "-t", session.session_id, "C-c"])

    def reset_session(self, session: TerminalSession) -> TerminalSession:
        try:
            self.terminate_session(session)
        except Exception:  # noqa: BLE001
            pass
        return self.get_or_create_session(
            agent_id=session.agent_id, workspace_ref=session.workspace_ref
        )

    def terminate_session(self, session: TerminalSession) -> None:
        self._run(["kill-session", "-t", session.session_id], allow_failure=True)
        acc = self._accumulators.pop(session.session_id, None)
        if acc is not None:
            acc.reset()

    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        text = self._capture_full(session) if self._session_exists_raw(session.session_id) else ""
        acc = self._accumulators.get(session.session_id)
        return {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "text": text,
            "accumulated_text": acc.text if acc else "",
        }

    def session_exists(self, session: TerminalSession) -> bool:
        return self._session_exists_raw(session.session_id)

    def health(self, session: TerminalSession) -> SessionHealth:
        exists = self._session_exists_raw(session.session_id)
        if not exists:
            return SessionHealth(
                session_id=session.session_id,
                exists=False,
                healthy=False,
                reason="session_missing",
                metadata={"host_kind": self.kind},
            )
        return SessionHealth(
            session_id=session.session_id,
            exists=True,
            healthy=True,
            reason=None,
            metadata={"host_kind": self.kind, "tmux_session": session.session_id},
        )

    def read_visible(self, session: TerminalSession) -> str:
        """Read the currently visible screen (no scrollback)."""
        return self._run([
            "capture-pane", "-t", session.session_id, "-p",
        ])

    def wait_for_idle(
        self,
        session: TerminalSession,
        *,
        poll_seconds: float = 2.0,
        idle_after: int = 3,
        timeout_seconds: float = 0.0,
        check_lines: int = 20,
        on_poll: Any | None = None,
    ) -> bool:
        """Block until the tmux pane output stops changing."""

        def _normalise(raw: str) -> str:
            lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            lines = [ln.rstrip() for ln in lines]
            while lines and not lines[-1]:
                lines.pop()
            return "\n".join(lines)

        prev = ""
        unchanged = 0
        was_busy = False
        start = monotonic()

        while True:
            if timeout_seconds > 0 and monotonic() - start > timeout_seconds:
                return False
            if on_poll is not None:
                on_poll()
            try:
                raw = self._run([
                    "capture-pane", "-t", session.session_id, "-p", "-J",
                    "-S", str(-check_lines),
                ])
            except RuntimeError:
                return False
            snap = _normalise(raw)
            if snap == prev:
                unchanged += 1
                if was_busy and unchanged >= idle_after:
                    return True
                if not was_busy and unchanged >= idle_after * 2:
                    return True
            else:
                unchanged = 0
                was_busy = True
            prev = snap
            sleep(poll_seconds)
