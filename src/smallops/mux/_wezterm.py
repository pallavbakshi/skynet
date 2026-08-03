"""WezTerm Mux implementation."""

from __future__ import annotations

import json
import shlex
import subprocess
from time import monotonic, sleep
from typing import Any

from smallops._types import SessionInfo
from smallops._util import is_shell_foreground

_PASTE_CHUNK_SIZE = 4096


class WezTermMux:
    """Terminal multiplexer backed by WezTerm.

    Sessions are wezterm panes within a workspace, identified by pane_id.
    Uses text-anchor diffing for output tracking (no absolute line numbers).
    """

    kind = "wezterm"

    def __init__(
        self,
        *,
        bin: str = "wezterm",
        workspace: str = "default",
        domain: str = "",
        scrollback: int = 5000,
    ) -> None:
        self._bin = bin
        self._workspace = workspace
        self._domain = domain
        self._scrollback = scrollback

    # ── Internal helpers ─────────────────────────────────────────────

    def _run(self, args: list[str], *, stdin_text: str | None = None) -> str:
        result = subprocess.run(
            [self._bin, "cli", *args],
            input=stdin_text,
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wezterm cli {' '.join(args)}: {(result.stderr or '').strip()}")
        return result.stdout or ""

    def _list_panes(self) -> list[dict[str, Any]]:
        raw = self._run(["list", "--format", "json"])
        if not raw:
            return []
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else []

    def _marker(self, name: str) -> str:
        return f"SMALLOPS:{name}"

    def _find_existing(self, name: str) -> SessionInfo | None:
        marker = self._marker(name)
        for pane in self._list_panes():
            if pane.get("workspace") != self._workspace:
                continue
            if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                return SessionInfo(
                    id=str(pane["pane_id"]),
                    name=name,
                    cwd=pane.get("cwd"),
                    metadata={"pane_id": pane["pane_id"], "workspace": self._workspace},
                )
        return None

    def _send_chunked(self, pane_args: list[str], text: str) -> None:
        """Send text in chunks to avoid terminal buffer overflow."""
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= _PASTE_CHUNK_SIZE:
            self._run([*pane_args, text])
            return

        chunks: list[str] = []
        current: list[str] = []
        current_size = 0

        for line in text.splitlines(keepends=True):
            line_size = len(line.encode("utf-8", errors="replace"))
            if current and current_size + line_size > _PASTE_CHUNK_SIZE:
                chunks.append("".join(current))
                current = []
                current_size = 0
            if line_size > _PASTE_CHUNK_SIZE:
                # Force-split oversized line
                line_enc = line.encode("utf-8", errors="replace")
                i = 0
                while i < len(line_enc):
                    end = min(i + _PASTE_CHUNK_SIZE, len(line_enc))
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

        if current:
            chunks.append("".join(current))

        for chunk in chunks:
            self._run([*pane_args, chunk])

    # ── Mux protocol ─────────────────────────────────────────────────

    def create_session(self, *, name: str, cwd: str | None = None) -> SessionInfo:
        existing = self._find_existing(name)
        if existing is not None:
            return existing

        marker = self._marker(name)

        # Try to reuse an unmarked idle pane in the workspace
        for pane in self._list_panes():
            if pane.get("workspace") != self._workspace:
                continue
            titles = (pane.get("window_title", ""), pane.get("tab_title", ""))
            if any(t.startswith("SMALLOPS:") for t in titles):
                continue
            pane_id = str(pane["pane_id"])
            self._run(["set-window-title", "--pane-id", pane_id, marker])
            self._run(["set-tab-title", "--pane-id", pane_id, marker])
            return SessionInfo(
                id=pane_id, name=name, cwd=cwd or pane.get("cwd"),
                metadata={"pane_id": pane["pane_id"], "workspace": self._workspace},
            )

        # Spawn a new pane
        args = ["spawn", "--new-window", "--workspace", self._workspace]
        if self._domain:
            args.extend(["--domain-name", self._domain])
        if cwd:
            args.extend(["--cwd", cwd])
        pane_id = self._run(args).strip()
        self._run(["set-window-title", "--pane-id", pane_id, marker])
        self._run(["set-tab-title", "--pane-id", pane_id, marker])
        sleep(0.3)  # let wezterm fully register the pane
        return SessionInfo(
            id=pane_id, name=name, cwd=cwd,
            metadata={"pane_id": int(pane_id), "workspace": self._workspace},
        )

    def destroy_session(self, session: SessionInfo) -> None:
        try:
            self._run(["kill-pane", "--pane-id", session.id])
        except RuntimeError:
            pass

    def session_exists(self, session: SessionInfo) -> bool:
        return any(str(p.get("pane_id")) == session.id for p in self._list_panes())

    def send_text(self, session: SessionInfo, text: str, *, enter: bool = True) -> None:
        pane_args = ["send-text", "--pane-id", session.id]
        is_multiline = "\n" in text

        if is_multiline:
            self._send_chunked(pane_args, text)
        else:
            self._run([*pane_args, "--no-paste", text])

        if enter:
            # Codex has paste-burst detection with a 120ms Enter suppression
            # window. After sending text as a burst, we must wait >120ms
            # before Enter, otherwise Enter is treated as a newline.
            sleep(0.50 if is_multiline else 0.15)
            self._run([*pane_args, "--no-paste", "\r"])

    def peek(self, session: SessionInfo, n: int | None = None) -> str:
        if n is not None and n > 0:
            return self._run(["get-text", "--pane-id", session.id, "--start-line", str(-n)])
        return self._run(["get-text", "--pane-id", session.id])

    def shell_idle(self, session: SessionInfo) -> bool:
        pane = next(
            (p for p in self._list_panes() if str(p.get("pane_id")) == session.id),
            None,
        )
        if pane is None:
            return False
        tty = pane.get("tty_name") or pane.get("tty") or pane.get("tty_path")
        if not tty:
            return False
        return is_shell_foreground(str(tty))

    def respawn(self, session: SessionInfo, command: str, *, env: dict[str, str] | None = None) -> SessionInfo:
        """Replace the pane's process with command. No TTY echo.

        WezTerm doesn't have respawn-pane — kills old pane and spawns new.
        Returns a new SessionInfo with the new pane ID.
        """
        cwd = session.cwd
        self.destroy_session(session)
        args = ["spawn", "--new-window", "--workspace", self._workspace]
        if self._domain:
            args.extend(["--domain-name", self._domain])
        if cwd:
            args.extend(["--cwd", cwd])
        # Build command with env var exports prepended
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
            command = f"env {exports} {command}"
        args.extend(["--", "sh", "-c", command])
        new_pane_id = self._run(args).strip()
        marker = self._marker(session.name)
        self._run(["set-window-title", "--pane-id", new_pane_id, marker])
        self._run(["set-tab-title", "--pane-id", new_pane_id, marker])
        sleep(0.3)  # let wezterm settle after respawn
        return SessionInfo(
            id=new_pane_id, name=session.name, cwd=cwd,
            metadata={"pane_id": int(new_pane_id), "workspace": self._workspace},
        )

    def interrupt(self, session: SessionInfo) -> None:
        try:
            self._run(["send-text", "--pane-id", session.id, "--no-paste", "\x03"])
        except RuntimeError:
            return
        # Give the shell a brief chance to settle after Ctrl-C
        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            if self.shell_idle(session):
                return
            sleep(0.05)
