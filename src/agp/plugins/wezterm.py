"""WezTerm terminal host plugin."""
from __future__ import annotations
import hashlib
import json
import subprocess
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from agp.config import settings
from agp.runtime import (
    OutputCursor, OutputReadResult, SessionHealth, TerminalHost, TerminalSession,
    _OutputAccumulator, _compute_output_delta, _strip_ansi,
)

# Shell prompt characters that indicate the CLI exited and the shell returned.
_SHELL_PROMPT_CHARS = {"\u276f", "\u2733", "$", "%", "#"}

# Known TUI process names for foreground detection.
_TUI_PROCESSES = {"codex", "ncodex", "claude", "gemini"}


class WezTermHost(TerminalHost):
    def __init__(
        self,
        *,
        wezterm_bin: str = "wezterm",
        workspace: str = "agp",
        shell_argv: list[str] | None = None,
        runner: Any | None = None,
        scrollback_lines: int = 5000,
        checkpoint_dir: Path | str | None = None,
        default_cwd: str = "",
    ) -> None:
        self.wezterm_bin = wezterm_bin
        self.workspace = workspace
        self.shell_argv = shell_argv
        self._runner = runner or subprocess.run
        self.scrollback_lines = scrollback_lines
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else settings.output_checkpoint_dir
        self.default_cwd = default_cwd or settings.wezterm_default_cwd or ""
        self._accumulators: dict[str, _OutputAccumulator] = {}

    def _get_accumulator(self, session: TerminalSession) -> _OutputAccumulator:
        if session.session_id not in self._accumulators:
            path = self.checkpoint_dir / f"session-{session.session_id}.output.txt"
            self._accumulators[session.session_id] = _OutputAccumulator(path)
        return self._accumulators[session.session_id]

    @property
    def kind(self) -> str:
        return "wezterm"

    def _run(self, args: list[str], *, stdin_text: str | None = None) -> str:
        completed = self._runner(
            [self.wezterm_bin, "cli", *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(f"wezterm command failed: {' '.join(args)} :: {stderr}")
        return completed.stdout or ""

    def _marker(self, agent_id: str) -> str:
        return f"AGP:{agent_id}"

    def _list_panes(self) -> list[dict[str, Any]]:
        raw = self._run(["list", "--format", "json"])
        if not raw:
            return []
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else []

    def _find_existing(self, *, agent_id: str) -> TerminalSession | None:
        marker = self._marker(agent_id)
        for pane in self._list_panes():
            if pane.get("workspace") != self.workspace:
                continue
            if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                return TerminalSession(
                    session_id=str(pane["pane_id"]),
                    agent_id=agent_id,
                    workspace_ref=pane.get("cwd"),
                    metadata={
                        "pane_id": pane["pane_id"],
                        "tab_id": pane.get("tab_id"),
                        "window_id": pane.get("window_id"),
                        "workspace": pane.get("workspace"),
                    },
                )
        return None

    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        existing = self._find_existing(agent_id=agent_id)
        if existing is not None:
            return existing
        cwd = workspace_ref or self.default_cwd
        args = ["spawn", "--new-window", "--workspace", self.workspace]
        if cwd:
            args.extend(["--cwd", cwd])
        if self.shell_argv:
            args.extend(["--", *self.shell_argv])
        pane_id = self._run(args).strip()
        session = TerminalSession(
            session_id=pane_id,
            agent_id=agent_id,
            workspace_ref=workspace_ref,
            metadata={"pane_id": int(pane_id), "workspace": self.workspace},
        )
        self._run(["set-window-title", "--pane-id", pane_id, self._marker(agent_id)])
        self._run(["set-tab-title", "--pane-id", pane_id, self._marker(agent_id)])
        return session

    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        self._run(["send-text", "--pane-id", session.session_id, "--no-paste", text])
        if enter:
            self._run(["send-text", "--pane-id", session.session_id, "--no-paste", "\r"])

    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        baseline = self._run(["get-text", "--pane-id", session.session_id, "--start-line", str(-self.scrollback_lines)])
        return OutputCursor(session_id=session.session_id, checkpoint=baseline, metadata={"line_count": 0})

    def save_cursor(self, session: TerminalSession, cursor: OutputCursor) -> None:
        """Persist cursor state to disk for restart resilience."""
        path = self.checkpoint_dir / f"cursor-{session.session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": session.session_id,
            "line_count": cursor.metadata.get("line_count", 0),
            "trailing_hash": cursor.metadata.get("trailing_hash", ""),
            "checkpoint_len": len(cursor.checkpoint),
        }, sort_keys=True))

    def load_cursor(self, session: TerminalSession) -> OutputCursor | None:
        """Load a persisted cursor.  Returns None if no checkpoint exists.

        On restore the checkpoint is set to the *previously accumulated*
        text (loaded from the accumulator file) so that the next
        ``read_output`` call treats any output produced while the runtime
        was down as new delta.  The accumulator deduplicates what it
        already persisted, and the anchor-based diff handles scrollback
        shifts that occurred during the gap.
        """
        path = self.checkpoint_dir / f"cursor-{session.session_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        # Load the accumulator to recover previously-seen content.  Using
        # that as the checkpoint means _compute_output_delta will treat
        # anything *not* in the accumulator as new.
        acc = self._get_accumulator(session)
        # Use the trailing portion of the accumulated text as the
        # checkpoint — _compute_output_delta's anchor search will match
        # the overlap between what we saw before and the current
        # scrollback, yielding only the genuinely new output.
        prior_tail = acc.text[-self.scrollback_lines * 80:] if acc.text else ""
        return OutputCursor(
            session_id=session.session_id,
            checkpoint=prior_tail,
            metadata={
                "line_count": data.get("line_count", 0),
                "trailing_hash": data.get("trailing_hash", ""),
                "restored": True,
            },
        )

    def read_output(self, session: TerminalSession, cursor: OutputCursor) -> OutputReadResult:
        raw = self._run(["get-text", "--pane-id", session.session_id, "--start-line", str(-self.scrollback_lines)])
        delta = _compute_output_delta(raw, cursor.checkpoint)
        accumulator = self._get_accumulator(session)
        accumulator.append(delta)
        prior_lines = cursor.metadata.get("line_count", 0)
        updated = OutputCursor(
            session_id=session.session_id,
            checkpoint=raw,
            metadata={
                **cursor.metadata,
                "line_count": prior_lines + delta.count("\n"),
                "trailing_hash": hashlib.sha256(raw[-2048:].encode()).hexdigest()[:16] if raw else "",
            },
        )
        self.save_cursor(session, updated)
        return OutputReadResult(
            session_id=session.session_id,
            cursor=updated,
            text=delta,
            full_text=accumulator.text,
            changed=bool(delta),
        )

    def is_foreground_tui(self, session: TerminalSession) -> bool:
        """Check whether a TUI process is still in the foreground.

        Reads the visible screen and checks for TUI-specific markers vs.
        shell prompt markers.  Returns True if a TUI appears to be running,
        False if the shell prompt has returned.
        """
        screen = _strip_ansi(self.read_visible(session))
        if not screen.strip():
            return False
        lines = screen.strip().splitlines()
        # Check the last few non-empty lines for shell vs. TUI indicators.
        tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
        has_tui = any("\u203a" in ln for ln in tail)  # › = Codex prompt
        has_shell = any(ln[0] in _SHELL_PROMPT_CHARS for ln in tail if ln)
        if has_tui:
            return True
        if has_shell:
            return False
        # Ambiguous — assume TUI is still alive.
        return True

    def interrupt(self, session: TerminalSession) -> None:
        self._run(["send-text", "--pane-id", session.session_id, "--no-paste", "\u0003"])

    def reset_session(self, session: TerminalSession) -> TerminalSession:
        try:
            self.terminate_session(session)
        except Exception:  # noqa: BLE001
            pass
        return self.get_or_create_session(agent_id=session.agent_id, workspace_ref=session.workspace_ref)

    def terminate_session(self, session: TerminalSession) -> None:
        self._run(["kill-pane", "--pane-id", session.session_id])
        acc = self._accumulators.pop(session.session_id, None)
        if acc is not None:
            acc.reset()

    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        pane = next((item for item in self._list_panes() if str(item.get("pane_id")) == session.session_id), None)
        text = self._run(["get-text", "--pane-id", session.session_id, "--start-line", str(-self.scrollback_lines)])
        acc = self._accumulators.get(session.session_id)
        return {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "pane": pane,
            "text": text,
            "accumulated_text": acc.text if acc else "",
        }

    def session_exists(self, session: TerminalSession) -> bool:
        return any(str(item.get("pane_id")) == session.session_id for item in self._list_panes())

    def health(self, session: TerminalSession) -> SessionHealth:
        pane = next((item for item in self._list_panes() if str(item.get("pane_id")) == session.session_id), None)
        if pane is None:
            return SessionHealth(
                session_id=session.session_id,
                exists=False,
                healthy=False,
                reason="pane_missing",
                metadata={"host_kind": self.kind},
            )
        return SessionHealth(
            session_id=session.session_id,
            exists=True,
            healthy=True,
            reason=None,
            metadata={
                "host_kind": self.kind,
                "workspace": pane.get("workspace"),
                "pane_id": pane.get("pane_id"),
                "tab_id": pane.get("tab_id"),
                "window_id": pane.get("window_id"),
            },
        )

    def read_visible(self, session: TerminalSession) -> str:
        """Read the visible screen content (captures alternate buffer)."""
        return self._run(["get-text", "--pane-id", session.session_id])

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
        """Block until the pane output stops changing.

        Uses snapshot comparison: reads the last *check_lines* lines,
        normalises whitespace, and waits until *idle_after* consecutive
        polls produce the same result.  A *was_busy* guard prevents
        false-positive idle on an already-quiet pane.
        """

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
                raw = self._run(
                    ["get-text", "--pane-id", session.session_id, "--start-line", str(-check_lines)],
                )
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
