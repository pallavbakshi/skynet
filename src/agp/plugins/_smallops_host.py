"""TerminalHost implementation backed by a smallops Mux.

Thin wrapper that adapts smallops.Mux to AGP's TerminalHost ABC.
The Mux handles all terminal operations; this class maps between
AGP's TerminalSession/SessionHealth types and smallops' SessionInfo.
"""
from __future__ import annotations

import logging
from typing import Any

from smallops import SessionInfo as _SessionInfo
from smallops._protocols import Mux

from agp.runtime._types import (
    OutputCursor,
    OutputReadResult,
    SessionHealth,
    TerminalSession,
)
from agp.runtime._abc import TerminalHost

_logger = logging.getLogger(__name__)


def _to_info(session: TerminalSession) -> _SessionInfo:
    """Convert AGP TerminalSession to smallops SessionInfo."""
    return _SessionInfo(
        id=session.session_id,
        name=session.agent_id,
        cwd=session.workspace_ref,
        metadata=session.metadata,
    )


def _to_terminal_session(info: _SessionInfo, agent_id: str) -> TerminalSession:
    """Convert smallops SessionInfo to AGP TerminalSession."""
    return TerminalSession(
        session_id=info.id,
        agent_id=agent_id,
        workspace_ref=info.cwd,
        metadata=info.metadata if info.metadata else {},
    )


class SmallopsTerminalHost(TerminalHost):
    """TerminalHost backed by a smallops Mux.

    Provides the full TerminalHost ABC for supervisor compatibility.
    Cursor/accumulator features are no-ops — the adapters use
    smallops Session.send() which handles output tracking internally.
    """

    def __init__(self, mux: Mux) -> None:
        self._mux = mux

    @property
    def mux(self) -> Mux:
        return self._mux

    @property
    def kind(self) -> str:
        return self._mux.kind

    # ── Session lifecycle ────────────────────────────────────────────

    def get_or_create_session(
        self, *, agent_id: str, workspace_ref: str | None = None,
    ) -> TerminalSession:
        info = self._mux.create_session(name=agent_id, cwd=workspace_ref)
        return _to_terminal_session(info, agent_id)

    def session_exists(self, session: TerminalSession) -> bool:
        return self._mux.session_exists(_to_info(session))

    def terminate_session(self, session: TerminalSession) -> None:
        self._mux.destroy_session(_to_info(session))

    def reset_session(self, session: TerminalSession) -> TerminalSession:
        self._mux.destroy_session(_to_info(session))
        return self.get_or_create_session(
            agent_id=session.agent_id, workspace_ref=session.workspace_ref,
        )

    # ── Text I/O ─────────────────────────────────────────────────────

    def send_text(
        self, session: TerminalSession, text: str, *, enter: bool = True,
    ) -> None:
        self._mux.send_text(_to_info(session), text, enter=enter)

    def interrupt(self, session: TerminalSession) -> None:
        self._mux.interrupt(_to_info(session))

    # ── Screen reading ───────────────────────────────────────────────

    def read_visible(self, session: TerminalSession) -> str:
        return self._mux.peek(_to_info(session))

    def read_scrollback(
        self, session: TerminalSession, *, lines: int | None = None,
    ) -> str:
        return self._mux.peek(_to_info(session), n=lines)

    # ── Cursor (minimal stubs for ABC compliance) ────────────────────

    def create_cursor(self, session: TerminalSession) -> OutputCursor:
        return OutputCursor(session_id=session.session_id)

    def read_output(
        self, session: TerminalSession, cursor: OutputCursor,
    ) -> OutputReadResult:
        text = self.read_visible(session)
        prev = cursor.metadata.get("_prev", "")
        changed = text != prev
        return OutputReadResult(
            session_id=session.session_id,
            cursor=OutputCursor(
                session_id=session.session_id,
                metadata={"_prev": text},
            ),
            text=text if changed else "",
            full_text=text,
            changed=changed,
        )

    def load_cursor(self, session: TerminalSession) -> OutputCursor | None:
        return None

    # ── Health & diagnostics ─────────────────────────────────────────

    def health(self, session: TerminalSession) -> SessionHealth:
        exists = self.session_exists(session)
        return SessionHealth(
            session_id=session.session_id,
            exists=exists,
            healthy=exists,
            reason=None if exists else "pane_missing",
            metadata={"host_kind": self.kind},
        )

    def snapshot(self, session: TerminalSession) -> dict[str, Any]:
        try:
            text = self.read_scrollback(session, lines=500)
        except Exception:
            _logger.debug("snapshot: read_scrollback failed", exc_info=True)
            text = ""
        return {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "text": text,
        }

    # ── TUI lifecycle detection ──────────────────────────────────────

    def shell_idle(self, session: TerminalSession) -> bool:
        return self._mux.shell_idle(_to_info(session))

    def is_foreground_tui(self, session: TerminalSession) -> bool:
        """Return True if a TUI agent (not a shell) is in the foreground.

        Used by the supervisor's heartbeat thread to detect TUI death.
        Inverse of shell_idle: if the foreground process is a shell,
        the TUI has exited.
        """
        if not self.session_exists(session):
            return False
        # If the foreground is a shell, the TUI is gone
        if self._mux.shell_idle(_to_info(session)):
            return False
        # Pane exists and foreground is not a shell — TUI is alive
        return True

    def _get_pane_tty(self, session: TerminalSession) -> str | None:
        # Not needed — shell_idle delegates to mux directly
        return None
