"""Herdr Mux implementation."""

from __future__ import annotations

import atexit
import json
import os
import shlex
import socket
import subprocess
import tempfile
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from smallops._types import SessionInfo

_SHELL_NAMES = frozenset({"bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh", "nu", "pwsh"})


class HerdrMux:
    """Terminal multiplexer backed by Herdr.

    Herdr exposes pane control through its CLI and socket server. By default
    this mux starts an isolated headless Herdr server with temporary config and
    runtime directories, so smallops sessions do not attach to a user's active
    Herdr UI session.
    """

    kind = "herdr"

    def __init__(
        self,
        *,
        bin: str = "herdr",
        prefix: str = "smallops",
        scrollback: int = 5000,
        socket_path: str | None = None,
        session: str | None = None,
        config_home: str | None = None,
        runtime_dir: str | None = None,
        auto_start: bool = True,
    ) -> None:
        self._bin = bin
        self._prefix = prefix
        self._scrollback = scrollback
        self._session = session
        self._auto_start = auto_start
        self._server: subprocess.Popen[str] | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None

        if auto_start and (
            (socket_path is None and session is None) or config_home is None or runtime_dir is None
        ):
            self._tempdir = tempfile.TemporaryDirectory(prefix="smallops-herdr-")
            base = Path(self._tempdir.name)
            socket_path = socket_path or (None if session is not None else str(base / "herdr.sock"))
            config_home = config_home or str(base / "config")
            runtime_dir = runtime_dir or str(base / "runtime")

        self._socket_path = socket_path
        self._config_home = config_home
        self._runtime_dir = runtime_dir
        if self._config_home:
            self._write_default_config(Path(self._config_home))
        if self._runtime_dir:
            Path(self._runtime_dir).mkdir(parents=True, exist_ok=True)

        atexit.register(self.close)

    # ── Internal helpers ─────────────────────────────────────────────

    def _write_default_config(self, config_home: Path) -> None:
        for app_dir in ("herdr", "herdr-dev"):
            path = config_home / app_dir
            path.mkdir(parents=True, exist_ok=True)
            config = path / "config.toml"
            if not config.exists():
                config.write_text("onboarding = false\n", encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._socket_path:
            env["HERDR_SOCKET_PATH"] = self._socket_path
        if self._config_home:
            env["XDG_CONFIG_HOME"] = self._config_home
        if self._runtime_dir:
            env["XDG_RUNTIME_DIR"] = self._runtime_dir
        env.setdefault("SHELL", "/bin/sh")
        env.pop("HERDR_CLIENT_SOCKET_PATH", None)
        env.pop("HERDR_ENV", None)
        return env

    def _base_args(self) -> list[str]:
        if self._session:
            return ["--session", self._session]
        return []

    def _server_running(self) -> bool:
        if self._socket_path:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.1)
                    sock.connect(self._socket_path)
                return True
            except OSError:
                return False

        result = subprocess.run(
            [self._bin, *self._base_args(), "workspace", "list"],
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        return result.returncode == 0

    def _ensure_server(self) -> None:
        if self._server_running():
            return
        if not self._auto_start:
            return
        self._server = subprocess.Popen(
            [self._bin, *self._base_args(), "server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=self._env(),
        )
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            if self._server.poll() is not None:
                break
            if self._server_running():
                return
            sleep(0.05)
        raise RuntimeError("herdr server did not become ready")

    def _run(
        self,
        args: list[str],
        *,
        allow_failure: bool = False,
        ensure_server: bool = True,
    ) -> str:
        if ensure_server:
            self._ensure_server()
        result = subprocess.run(
            [self._bin, *self._base_args(), *args],
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0 and not allow_failure:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout
            raise RuntimeError(f"herdr {' '.join(args)}: {detail}")
        return result.stdout or ""

    def _json(self, args: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
        raw = self._run(args, allow_failure=allow_failure)
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if allow_failure:
                return {}
            raise
        return payload if isinstance(payload, dict) else {}

    def _workspace_label(self, name: str) -> str:
        return f"{self._prefix}-{name}"

    def _find_existing(self, name: str) -> SessionInfo | None:
        label = self._workspace_label(name)
        payload = self._json(["workspace", "list"], allow_failure=True)
        workspaces = payload.get("result", {}).get("workspaces", [])
        if not isinstance(workspaces, list):
            return None
        for workspace in workspaces:
            if not isinstance(workspace, dict) or workspace.get("label") != label:
                continue
            workspace_id = str(workspace.get("workspace_id", ""))
            pane_payload = self._json(
                ["pane", "list", "--workspace", workspace_id],
                allow_failure=True,
            )
            panes = pane_payload.get("result", {}).get("panes", [])
            if not isinstance(panes, list) or not panes:
                continue
            pane = panes[0]
            if not isinstance(pane, dict) or not pane.get("pane_id"):
                continue
            return SessionInfo(
                id=str(pane["pane_id"]),
                name=name,
                cwd=pane.get("cwd"),
                metadata={"workspace_id": workspace_id, "label": label},
            )
        return None

    def _shell_command(self, command: str, env: dict[str, str] | None = None) -> str:
        if not env:
            return command
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        return f"env {exports} {command}"

    # ── Mux protocol ─────────────────────────────────────────────────

    def create_session(self, *, name: str, cwd: str | None = None) -> SessionInfo:
        existing = self._find_existing(name)
        if existing is not None:
            return existing

        payload = self._json(
            [
                "workspace",
                "create",
                "--label",
                self._workspace_label(name),
                "--no-focus",
                *(["--cwd", cwd] if cwd else []),
            ]
        )
        result = payload.get("result", {})
        pane = result.get("root_pane", {})
        workspace = result.get("workspace", {})
        pane_id = str(pane.get("pane_id", ""))
        workspace_id = str(workspace.get("workspace_id", ""))
        if not pane_id or not workspace_id:
            raise RuntimeError(f"herdr workspace create returned unexpected payload: {payload!r}")
        sleep(0.3)
        return SessionInfo(
            id=pane_id,
            name=name,
            cwd=cwd or pane.get("cwd"),
            metadata={"workspace_id": workspace_id, "label": self._workspace_label(name)},
        )

    def destroy_session(self, session: SessionInfo) -> None:
        workspace_id = session.metadata.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            self._run(["workspace", "close", workspace_id], allow_failure=True)
            return
        self._run(["pane", "close", session.id], allow_failure=True)

    def session_exists(self, session: SessionInfo) -> bool:
        payload = self._json(["pane", "get", session.id], allow_failure=True)
        return bool(payload.get("result", {}).get("pane", {}).get("pane_id"))

    def send_text(self, session: SessionInfo, text: str, *, enter: bool = True) -> None:
        if text == "\x1b[B":
            self._run(["pane", "send-keys", session.id, "Down"])
        elif text:
            self._run(["pane", "send-text", session.id, text])
        if enter:
            if text:
                sleep(0.50 if "\n" in text else 0.15)
            self._run(["pane", "send-keys", session.id, "Enter"])

    def peek(self, session: SessionInfo, n: int | None = None) -> str:
        args = ["pane", "read", session.id, "--source", "visible"]
        if n is not None and n > 0:
            args = [
                "pane",
                "read",
                session.id,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(n),
            ]
        return self._run(args)

    def shell_idle(self, session: SessionInfo) -> bool:
        payload = self._json(
            ["pane", "process-info", "--pane", session.id],
            allow_failure=True,
        )
        processes = payload.get("result", {}).get("process_info", {}).get("foreground_processes", [])
        if not isinstance(processes, list) or not processes:
            return False
        proc = processes[-1]
        if not isinstance(proc, dict):
            return False
        name = str(proc.get("name") or proc.get("argv0") or "").strip().lstrip("-").split("/")[-1].lower()
        return name in _SHELL_NAMES

    def respawn(
        self,
        session: SessionInfo,
        command: str,
        *,
        env: dict[str, str] | None = None,
    ) -> SessionInfo:
        """Start command in the pane's shell.

        Herdr's CLI currently offers input-driven ``pane run`` rather than a
        tmux-style process replacement primitive, so the launch command may be
        visible in scrollback.
        """
        self._run(["pane", "run", session.id, self._shell_command(command, env)])
        sleep(0.3)
        return session

    def interrupt(self, session: SessionInfo) -> None:
        try:
            self._run(["pane", "send-keys", session.id, "C-c"])
        except RuntimeError:
            return
        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            if self.shell_idle(session):
                return
            sleep(0.05)

    def close(self) -> None:
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._server.kill()
                self._server.wait(timeout=2)
            self._server = None
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None
