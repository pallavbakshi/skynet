"""Abstract base classes for terminal hosts and agent adapters."""

from __future__ import annotations

import contextlib
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from os.path import basename
from pathlib import Path
from time import sleep, time
from typing import Any
from urllib.parse import unquote, urlparse

from agp.plugins._output_contracts import prompt_for_claim
from agp.plugins._provider_env import PROVIDER_ENV_VARS
from agp.runtime._types import (
    ArtifactPayload,
    ExecutionResult,
    OutputCursor,
    OutputReadResult,
    SessionHealth,
    TerminalSession,
)


class TerminalHost(ABC):
    _LAUNCH_ROOT_DIR = Path(tempfile.gettempdir()) / "agp-launches"
    _STALE_LAUNCH_GRACE_SECONDS = 3600.0

    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
        raise NotImplementedError

    @abstractmethod
    def send_text(self, session: TerminalSession, text: str, *, enter: bool = True) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        raise NotImplementedError

    @abstractmethod
    def read_output(self, session: TerminalSession, cursor: OutputCursor) -> OutputReadResult:
        raise NotImplementedError

    @abstractmethod
    def interrupt(self, session: TerminalSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_session(self, session: TerminalSession) -> TerminalSession:
        raise NotImplementedError

    @abstractmethod
    def terminate_session(self, session: TerminalSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def session_exists(self, session: TerminalSession) -> bool:
        raise NotImplementedError

    def load_cursor(self, session: TerminalSession) -> OutputCursor | None:
        """Load a persisted cursor from a previous runtime process.

        Returns None if no checkpoint exists.  Hosts that support
        restart-safe cursors should override this.
        """
        return None

    def read_visible(self, session: TerminalSession) -> str:
        """Read the currently visible screen content (including alternate buffer).

        Default returns empty string.  Hosts that can capture the alternate
        screen buffer should override this.
        """
        return ""

    def read_scrollback(self, session: TerminalSession, *, lines: int | None = None) -> str:
        """Read the full pane including scrollback history.

        Returns as much content as the host can provide.  Tmux hosts
        capture up to ``scrollback_lines`` of history; other hosts fall
        back to :meth:`read_visible`.

        If *lines* is given, return only the last *lines* lines of
        scrollback.  None means return all available history.
        """
        return self.read_visible(session)

    @abstractmethod
    def health(self, session: TerminalSession) -> SessionHealth:
        raise NotImplementedError

    # ── Process-based idle detection ───────────────────────────────────

    _SHELL_NAMES = frozenset({
        "sh", "ash", "bash", "dash", "fish", "ksh", "mksh", "pdksh",
        "tcsh", "zsh", "nu",
    })

    def _get_pane_tty(self, session: TerminalSession) -> str | None:
        """Return the TTY path for the session's pane.

        Subclasses should override this — the default returns None
        (disabling process-based idle detection).
        """
        return None

    def _foreground_command(self, session: TerminalSession) -> str | None:
        """Return the foreground process command via ps, or None.

        Prefer ``pgid == tpgid`` when available because it survives shells that
        temporarily lose the ``+`` state marker during fast job-control
        transitions. On BSD/macOS ``+`` marks all members of the foreground
        process group, so both the shell and its child can have ``+``. We take
        the *last* matching entry because ``ps`` lists parent before child, and
        the deepest child is the actual foreground process.
        """
        tty = self._get_pane_tty(session)
        if not tty:
            return None
        tty_name = tty.removeprefix("/dev/")
        try:
            completed = subprocess.run(
                ["ps", "-o", "pid=", "-o", "pgid=", "-o", "tpgid=", "-o", "comm=", "-t", tty_name],
                capture_output=True, text=True, check=False,
            )
        except (FileNotFoundError, OSError):
            completed = None
        if completed is not None and completed.returncode == 0:
            fg_cmd = None
            for line in completed.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) != 4:
                    continue
                _pid, pgid, tpgid, comm = parts
                if pgid == tpgid:
                    fg_cmd = comm.strip()
            if fg_cmd:
                return fg_cmd
        try:
            completed = subprocess.run(
                ["ps", "-o", "state=", "-o", "comm=", "-t", tty_name],
                capture_output=True, text=True, check=False,
            )
        except (FileNotFoundError, OSError):
            return None
        if completed.returncode != 0:
            return None
        fg_cmd = None
        for line in completed.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and "+" in parts[0]:
                fg_cmd = parts[1].strip()
        return fg_cmd

    def shell_idle(self, session: TerminalSession) -> bool:
        """Return True when the foreground process is an interactive shell."""
        cmd = self._foreground_command(session)
        if not cmd:
            return False
        # Normalise: login shells appear as "-zsh", "-bash" etc.
        name = basename(cmd).lower().lstrip("-")
        return name in self._SHELL_NAMES

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
        """Block until pane output stops changing.

        Returns True when idle is detected, False on timeout.
        *on_poll* is called each iteration and may raise to abort.
        Default implementation returns True immediately (for in-process hosts).
        """
        return True

    def _cleanup_launch_artifacts(self, *paths: Path) -> None:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                continue
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _launch_owner_is_alive(self, owner_pid: int) -> bool:
        if owner_pid <= 0:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(owner_pid, 0)
            return True
        return False

    def _reap_stale_launch_directories(self) -> None:
        launch_root = self._LAUNCH_ROOT_DIR
        if not launch_root.exists():
            return
        for path in launch_root.glob("agp-launch-*"):
            if not path.is_dir():
                continue
            owner_pid = -1
            owner_path = path / ".owner-pid"
            with contextlib.suppress(OSError, ValueError):
                owner_pid = int(owner_path.read_text(encoding="utf-8").strip())
            try:
                age_seconds = max(0.0, time() - path.stat().st_mtime)
            except OSError:
                continue
            try:
                owner_is_alive = self._launch_owner_is_alive(owner_pid)
            except PermissionError:
                continue
            if owner_is_alive:
                continue
            if age_seconds < self._STALE_LAUNCH_GRACE_SECONDS:
                continue
            self._cleanup_launch_artifacts(path)

    def _reap_prior_launch_scripts(self, session: TerminalSession) -> None:
        pending = session.metadata.pop("_pending_launch_scripts", [])
        for item in pending:
            script_path = item.get("script_path")
            script_dir = item.get("script_dir")
            paths: list[Path] = []
            if script_path:
                paths.append(Path(script_path))
            if script_dir:
                paths.append(Path(script_dir))
            self._cleanup_launch_artifacts(*paths)

    def _resolve_login_shell(self) -> str:
        candidates: list[str] = []
        env_shell = os.environ.get("SHELL")
        if env_shell:
            candidates.append(env_shell)
        with contextlib.suppress(KeyError):
            passwd_shell = pwd.getpwuid(os.getuid()).pw_shell
            if passwd_shell:
                candidates.append(passwd_shell)
        for candidate in candidates:
            resolved = candidate
            if not os.path.isabs(resolved):
                resolved = shutil.which(resolved) or resolved
            if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
                return resolved
        return "/bin/sh"

    def launch_command(
        self,
        session: TerminalSession,
        *,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.Popen[str]:
        """Launch a foreground CLI in the pane without echoing secrets.

        The default implementation writes a short-lived shell script containing
        the environment setup and command, then asks the pane's existing shell
        to execute that script in the foreground. This preserves normal shell
        job-control semantics while keeping provider env out of visible
        scrollback.
        """
        self._reap_stale_launch_directories()
        self._reap_prior_launch_scripts(session)
        launch_cwd = cwd or session.workspace_ref
        if launch_cwd and "://" in launch_cwd:
            parsed = urlparse(launch_cwd)
            if parsed.scheme == "file":
                launch_cwd = unquote(parsed.path)
        self._LAUNCH_ROOT_DIR.mkdir(parents=True, exist_ok=True)
        script_dir = Path(tempfile.mkdtemp(prefix="agp-launch-", dir=self._LAUNCH_ROOT_DIR))
        script_fd, script_path_raw = tempfile.mkstemp(
            prefix=".agp-launch-",
            suffix=".sh",
            dir=script_dir,
            text=True,
        )
        script_path = Path(script_path_raw)
        shell_setup = [f"unset {key}" for key in PROVIDER_ENV_VARS]
        for key, value in (env or {}).items():
            shell_setup.append(f"export {key}={shlex.quote(value)}")
        inner_command_parts: list[str] = []
        if launch_cwd:
            inner_command_parts.append(f"cd {shlex.quote(str(Path(launch_cwd)))}")
        inner_command_parts.extend(shell_setup)
        inner_command_parts.append(f"rmdir {shlex.quote(str(script_dir))} >/dev/null 2>&1 || true")
        inner_command_parts.append(command)
        inner_command = "; ".join(inner_command_parts)
        script_lines = [
            "#!/bin/sh",
            "set -eu",
            "rm -f -- \"$0\"",
        ]
        script_lines.append(shlex.join(["exec", self._resolve_login_shell(), "-l", "-c", inner_command]))
        try:
            with os.fdopen(script_fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(script_lines) + "\n")
            script_path.chmod(0o700)
            (script_dir / ".owner-pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            pending = session.metadata.setdefault("_pending_launch_scripts", [])
            pending.append({
                "script_path": str(script_path),
                "script_dir": str(script_dir),
            })
            self.send_text(session, shlex.quote(str(script_path)), enter=True)
        except Exception:
            self._cleanup_launch_artifacts(script_path, script_dir)
            raise
        proc = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
        return proc


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:
        return None

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        return {
            "adapter_kind": self.kind,
            "supported": False,
            "run_id": run_id,
            "text_length": len(text),
        }

    @abstractmethod
    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: RuntimeSupervisor,
    ) -> ExecutionResult:
        raise NotImplementedError

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,
        error: Exception,
        supervisor: RuntimeSupervisor,
    ) -> None:
        sleep(0.01)

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: RuntimeSupervisor,
    ) -> ExecutionResult:
        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt_for_claim(claimed=claimed)),
                ArtifactPayload(
                    role="transcript_log",
                    name="transcript.txt",
                    content=f"runtime.failed\nerror={type(error).__name__}: {error}\n",
                ),
                ArtifactPayload(role="exec_log", name="exec.txt", content="failure-path\n"),
                ArtifactPayload(
                    role="failure_evidence",
                    name="failure.txt",
                    content=f"{type(error).__name__}: {error}\n",
                ),
            ],
            summary={"adapter": self.kind, "exception_type": type(error).__name__},
        )


# Avoid circular import — use string annotation above and resolve here
from agp.runtime._supervisor import (
    RuntimeSupervisor,
)
