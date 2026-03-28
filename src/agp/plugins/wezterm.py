"""WezTerm terminal host plugin."""
from __future__ import annotations
import hashlib
import json
import os
import shlex
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
_PROVIDER_ENV_KEYS_TO_RESET = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "AGP_SERVER_URL",
)


def _ensure_codex_config(base_url: str) -> None:
    """Best-effort: set ``openai_base_url`` in ``~/.codex/config.toml``."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f'openai_base_url = "{base_url}"\n')
        return
    try:
        existing = tomllib.loads(config_path.read_text())
    except Exception:
        return
    if existing.get("openai_base_url") == base_url:
        return


def _provider_env() -> dict[str, str]:
    """Collect provider API keys and endpoint overrides for WezTerm sessions."""
    env: dict[str, str] = {}

    openai_key = os.environ.get("OPENAI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    if openai_base_url:
        _ensure_codex_config(openai_base_url)
        env["OPENAI_BASE_URL"] = openai_base_url
    if openrouter_key and " -p " in f" {settings.codex_cli_command} ":
        env["OPENROUTER_API_KEY"] = openrouter_key

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key is not None:
        env["ANTHROPIC_API_KEY"] = anthropic_key

    for key in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val

    agp_server_url = os.environ.get("AGP_SERVER_URL")
    if agp_server_url:
        env["AGP_SERVER_URL"] = agp_server_url
    return env


class WezTermHost(TerminalHost):
    def __init__(
        self,
        *,
        wezterm_bin: str = "wezterm",
        workspace: str = "agp",
        domain: str = "",
        shell_argv: list[str] | None = None,
        runner: Any | None = None,
        scrollback_lines: int = 5000,
        checkpoint_dir: Path | str | None = None,
        default_cwd: str = "",
    ) -> None:
        self.wezterm_bin = wezterm_bin
        self.workspace = workspace
        self.domain = domain or settings.wezterm_domain
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

    def _export_provider_env(self, session: TerminalSession) -> None:
        provider_env = _provider_env()
        commands: list[str] = []
        stale_keys = [key for key in _PROVIDER_ENV_KEYS_TO_RESET if key not in provider_env]
        if stale_keys:
            commands.append("unset " + " ".join(stale_keys))
        if provider_env:
            exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in provider_env.items())
            commands.append(f"export {exports}")
        if not commands:
            return
        self._run(["send-text", "--pane-id", session.session_id, "--no-paste", "; ".join(commands) + "\r"])

    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        existing = self._find_existing(agent_id=agent_id)
        if existing is not None:
            return existing
        # Reuse an unmarked idle pane in the target workspace (e.g. the
        # default pane the mux-server creates on startup) so the agent
        # session is the pane the user lands on when connecting.
        marker = self._marker(agent_id)
        for pane in self._list_panes():
            if pane.get("workspace") != self.workspace:
                continue
            if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                continue
            # Skip panes already claimed by another agent.
            if any(
                t.startswith("AGP:") for t in (pane.get("window_title", ""), pane.get("tab_title", ""))
            ):
                continue
            pane_id = str(pane["pane_id"])
            session = TerminalSession(
                session_id=pane_id,
                agent_id=agent_id,
                workspace_ref=workspace_ref or pane.get("cwd"),
                metadata={"pane_id": pane["pane_id"], "workspace": self.workspace},
            )
            self._run(["set-window-title", "--pane-id", pane_id, marker])
            self._run(["set-tab-title", "--pane-id", pane_id, marker])
            self._export_provider_env(session)
            return session
        cwd = workspace_ref or self.default_cwd
        args = ["spawn", "--new-window", "--workspace", self.workspace]
        if self.domain:
            args.extend(["--domain-name", self.domain])
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
        self._run(["set-window-title", "--pane-id", pane_id, marker])
        self._run(["set-tab-title", "--pane-id", pane_id, marker])
        self._export_provider_env(session)
        return session

    # Maximum bytes per wezterm send-text call.  Larger payloads are
    # automatically chunked to avoid terminal buffer overflow (matching
    # the chunking strategy used by wezutils).
    _PASTE_CHUNK_SIZE = 4096

    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        pane_args = ["send-text", "--pane-id", session.session_id]
        is_multiline = "\n" in text

        if is_multiline:
            # Multiline: use paste mode (no --no-paste) so TUI receives
            # the full text as a single bracketed paste, then send Enter
            # separately after a short delay.
            self._send_chunked(pane_args, text)
        else:
            self._run([*pane_args, "--no-paste", text])

        if enter:
            # Give the TUI time to process the text before Enter.
            sleep(0.15 if is_multiline else 0.05)
            self._run([*pane_args, "--no-paste", "\r"])

    _MAX_CHUNKS = 1000
    _MAX_LINE_SIZE = 10_000_000

    def _send_chunked(self, base_args: list[str], text: str) -> None:
        """Send text in chunks to avoid terminal buffer overflow.

        Prefers splitting on line boundaries to avoid cutting mid-word.
        Falls back to byte-level splitting for individual lines that
        exceed the chunk size.  Guards against unbounded chunk creation
        and oversized lines.
        """
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self._PASTE_CHUNK_SIZE:
            self._run([*base_args, text])
            return

        chunks: list[str] = []
        current: list[str] = []
        current_size = 0

        for line in text.splitlines(keepends=True):
            if len(line) > self._MAX_LINE_SIZE:
                continue  # skip pathologically long lines
            line_size = len(line.encode("utf-8", errors="replace"))
            if current and current_size + line_size > self._PASTE_CHUNK_SIZE:
                chunks.append("".join(current))
                if len(chunks) >= self._MAX_CHUNKS:
                    break
                current = []
                current_size = 0
            if line_size > self._PASTE_CHUNK_SIZE:
                # Force-split oversized line by bytes.
                line_enc = line.encode("utf-8", errors="replace")
                i = 0
                while i < len(line_enc) and len(chunks) < self._MAX_CHUNKS:
                    end = min(i + self._PASTE_CHUNK_SIZE, len(line_enc))
                    if end < len(line_enc):
                        while end > i and (line_enc[end] & 0xC0) == 0x80:
                            end -= 1
                    if end <= i:
                        end = i + 1
                    chunks.append(line_enc[i:end].decode("utf-8", errors="replace"))
                    i = end
                continue
            current.append(line)
            current_size += line_size

        if current and len(chunks) < self._MAX_CHUNKS:
            chunks.append("".join(current))

        for chunk in chunks:
            self._run([*base_args, chunk])

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

        Detects both Codex TUI (› prompt marker) and Claude Code TUI
        (⏺ response, ────  separators, ⏵⏵ status bar, ╭/╰ welcome box).
        """
        screen = _strip_ansi(self.read_visible(session))
        if not screen.strip():
            return False
        lines = screen.strip().splitlines()
        # Check the last few non-empty lines for shell vs. TUI indicators.
        tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
        has_codex_tui = any("\u203a" in ln for ln in tail)  # › = Codex prompt
        # Claude Code indicators: ⏺ response, ────  separator, ⏵⏵ status bar, ╭/╰ box
        has_claude_tui = any(
            ln.startswith("\u23fa")  # ⏺
            or ln.startswith("\u25cf")  # ●
            or ln.startswith("\u256d") or ln.startswith("\u2570")  # ╭ ╰
            or "\u23f5\u23f5" in ln  # ⏵⏵ status bar
            or all(ch == "\u2500" for ch in ln if ch != " ")  # ────
            for ln in tail if ln
        )
        has_shell = any(
            ln[0] in _SHELL_PROMPT_CHARS or ln[-1] in ("$", "%", "#")
            for ln in tail if ln
        )
        if has_codex_tui or has_claude_tui:
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
