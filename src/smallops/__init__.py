"""smallops — Minimal TUI agent orchestration.

    from smallops import Session, TmuxMux, ClaudeCodeTui

    with Session(mux=TmuxMux(), tui=ClaudeCodeTui()) as s:
        s.up(cwd="/path/to/project")
        r = s.send("fix the bug in main.py")
        print(r.text)
"""

from __future__ import annotations

from time import monotonic, sleep
from uuid import uuid4

from smallops._poll import (
    _handle_gate,
    poll_until_done,
    wait_for_idle,
    wait_for_ready,
)
from smallops._protocols import Mux, Tui
from smallops._types import (
    AgentState,
    Block,
    BlockKind,
    BootstrapTimeout,
    Config,
    FatalGate,
    IdleReason,
    Meta,
    PaneDied,
    ParsedResponse,
    Response,
    SendTimeout,
    SessionInfo,
    SmallopsError,
    Status,
)
from smallops._util import (
    cleanup_via_file,
    normalize_screen,
    strip_ansi,
    write_nudge_file,
    write_via_file,
)
from smallops.mux import HerdrMux, TmuxMux, WezTermMux
from smallops.tui import ClaudeCodeTui, CodexTui

__all__ = [
    "AgentState",
    "Block",
    "BlockKind",
    "BootstrapTimeout",
    "ClaudeCodeTui",
    "CodexTui",
    "Config",
    "FatalGate",
    "HerdrMux",
    "IdleReason",
    "Meta",
    "Mux",
    "PaneDied",
    "ParsedResponse",
    "Response",
    "SendTimeout",
    "Session",
    "SessionInfo",
    "SmallopsError",
    "Status",
    "TmuxMux",
    "Tui",
    "WezTermMux",
    "normalize_screen",
    "strip_ansi",
]


class Session:
    """Composed Mux + Tui handle.  The primary user-facing API."""

    def __init__(
        self,
        *,
        mux: Mux,
        tui: Tui,
        config: Config | None = None,
        name: str | None = None,
    ) -> None:
        self.mux = mux
        self.tui = tui
        self.config = config or Config()
        self._name = name or f"s-{uuid4().hex[:8]}"
        self._session: SessionInfo | None = None
        self._markers: list[str] = []
        self._task_files: list[str] = []
        self._started_at: float | None = None
        self._last_activity: float = 0.0
        self._env: dict[str, str] | None = None

    # ── Screen reading ─────────���─────────────────────────────────────

    def _read_screen(self, n: int | None = None, *, handle_gates: bool = True) -> str:
        """Read and normalize screen text.

        Public observation helpers handle ambient gates. Polling loops disable
        that and handle gates themselves so one screen cannot be dismissed twice.
        """
        session = self._require_session()
        screen = normalize_screen(strip_ansi(self.mux.peek(session, n)))
        if handle_gates:
            _handle_gate(self.mux, self.tui, session, screen)
        return screen

    # ── Lifecycle ─────────���──────────────────────────────────────────

    def up(self, *, cwd: str | None = None, env: dict[str, str] | None = None) -> SessionInfo:
        """Create terminal pane, launch agent, wait for ready prompt.

        Args:
            cwd: Working directory for the agent.
            env: Environment variables to inject (e.g. API keys).
        """
        self._session = self.mux.create_session(name=self._name, cwd=cwd)
        self._env = env
        self._started_at = monotonic()
        self._last_activity = self._started_at

        if getattr(self.tui, "defer_launch_until_send", False):
            return self._session

        # Launch agent CLI via respawn — replaces the pane's shell process
        # directly without going through the TTY input path, so the command
        # never appears in terminal scrollback.
        cmd = self.tui.launch_command(cwd=cwd)
        self._session = self.mux.respawn(self._session, cmd, env=env)

        # Wait for ready
        wait_for_ready(self, self.config)
        return self._session

    def down(self) -> None:
        """Interrupt agent and destroy terminal pane."""
        if self._session is not None:
            try:
                self.mux.interrupt(self._session)
            except Exception:
                pass
            try:
                self.mux.destroy_session(self._session)
            except Exception:
                pass
            self._session = None

        # Clean up task files
        for path in self._task_files:
            cleanup_via_file(path)
        self._task_files.clear()

    def reset(self, *, cwd: str | None = None, env: dict[str, str] | None = None) -> SessionInfo:
        """Tear down and bring back up in one call."""
        old_cwd = self._session.cwd if self._session else None
        self.down()
        return self.up(cwd=cwd or old_cwd, env=env)

    # ── Prompt operations ────────��──────────────────────────────��────

    def send(
        self,
        prompt: str = "",
        *,
        file: str | None = None,
        sections: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Send prompt via file, wait for completion, return parsed response.

        Args:
            prompt: The task text. Ignored if ``file`` is provided.
            file: Path to an existing file whose content is used as the prompt.
                  The file is read and embedded in our own task file (with our
                  own marker filename). Avoids double-wrapping when the caller
                  already has a file.
            sections: Optional extra markdown sections (metadata, attachments,
                      context) appended after the task block.
            timeout: Max seconds to wait for completion.
        """
        if not prompt and not file:
            raise SmallopsError("send() requires either prompt or file")
        session = self._require_session()

        formatter = getattr(self.tui, "format_send", None)
        if formatter is not None:
            ref, send_text, path = formatter(
                prompt, file=file, sections=sections, directory=self.config.via_file_dir,
            )
        else:
            # Write via-file and send reference string
            ref, path = write_via_file(
                prompt, file=file, sections=sections, directory=self.config.via_file_dir,
            )
            send_text = ref
        if path is not None:
            self._task_files.append(path)
        self._markers.append(ref)

        launch_prompt = getattr(self.tui, "launch_prompt_command", None)
        if getattr(self.tui, "send_via_launch", False) and launch_prompt is not None:
            self._session = self.mux.respawn(
                session,
                launch_prompt(send_text, cwd=session.cwd),
                env=self._env,
            )
        else:
            self.mux.send_text(session, send_text, enter=True)
        self._last_activity = monotonic()

        # Poll until done
        response = poll_until_done(self, ref, self.config, timeout=timeout)
        self._last_activity = monotonic()
        return response

    def nudge(self, text: str) -> str:
        """Fire-and-forget text injection into a running agent.

        Writes text to a plain file (no BEGIN TASK / END TASK framing),
        sends a reference string to the terminal, and returns immediately.
        Does not poll or wait for completion.

        Returns the file path written.
        """
        session = self._require_session()
        ref, path = write_nudge_file(text, directory=self.config.via_file_dir)
        self._task_files.append(path)
        self.mux.send_text(session, ref, enter=True)
        self._last_activity = monotonic()
        return path

    # ── Observation ─────────���────────────────────────────────────────

    def peek(self, n: int | None = None) -> str:
        """Raw screen capture. None = visible, n = last n lines from scrollback."""
        return self._read_screen(n)

    def read(self, n: int | None = None, *, since: str | None = None) -> str:
        """Parsed response from screen/scrollback.

        Args:
            n: Number of scrollback lines to capture. None = visible only.
            since: Marker string to read after. Use "last" for the most
                   recent send()'s marker, or pass a specific marker string.
        """
        raw = self._read_screen(n)

        marker = ""
        if since == "last" and self._markers:
            marker = self._markers[-1]
        elif since is not None and since != "last":
            marker = since

        return self.tui.parse_response(raw, marker)

    # ── Control ───────────────���──────────────────────────────────────

    def interrupt(self) -> None:
        """Send Ctrl-C to the agent."""
        session = self._require_session()
        self.mux.interrupt(session)

    def wait(self, *, timeout: float | None = None) -> IdleReason:
        """Block until the agent is idle. Returns the idle reason."""
        return wait_for_idle(self, self.config, timeout=timeout)

    def is_alive(self) -> bool:
        """Check if the pane exists and the TUI is still running.

        Returns False if pane is gone OR if the TUI exited (shell returned).
        Use meta() for more detail on why it's not alive.
        """
        if self._session is None:
            return False

        if not self.mux.session_exists(self._session):
            return False

        try:
            screen = self._read_screen()
            reason = self.tui.classify_idle(screen)
            return reason != IdleReason.ERROR
        except Exception:
            return False

    def meta(self) -> Meta:
        """Live snapshot of session state.

        Takes 4 screen captures over 1.5s (every 0.5s). If any consecutive
        pair differs, the agent is working. Otherwise idle.
        """
        session = self._require_session()

        alive = self.mux.session_exists(session)
        now = monotonic()

        state = AgentState.IDLE
        idle_reason: IdleReason | None = None
        screen = ""

        if alive:
            # 4 captures, 0.5s apart — detect any screen change
            captures = []
            for i in range(4):
                if i > 0:
                    sleep(0.5)
                captures.append(self._read_screen())

            screen = captures[-1]

            if any(captures[i] != captures[i + 1] for i in range(len(captures) - 1)):
                state = AgentState.WORKING
                idle_reason = None
            else:
                state = AgentState.IDLE
                idle_reason = self.tui.classify_idle(screen)
        else:
            idle_reason = IdleReason.ERROR

        # Parse TUI status line
        status = self.tui.parse_status(screen) if screen else Status()
        if alive and not status.model:
            try:
                history = self._read_screen(500, handle_gates=False)
                status = self.tui.parse_status(history)
            except Exception:
                pass

        return Meta(
            state=state,
            idle_reason=idle_reason,
            alive=alive,
            uptime=now - self._started_at if self._started_at else 0.0,
            last_activity=now - self._last_activity if self._last_activity else 0.0,
            tui=self.tui.kind,
            mux=self.mux.kind,
            pane_id=session.id,
            status=status,
        )

    # ── Context manager ──────────────────────────────────────────────

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.down()

    # ── Internal ───────���──────────────────────────────���──────────────

    def _require_session(self) -> SessionInfo:
        if self._session is None:
            raise SmallopsError("session not started — call up() first")
        return self._session
