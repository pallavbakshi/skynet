"""Polling loops for bootstrap and send/wait operations.

All gate handling is ambient — every screen read checks for gates
and auto-dismisses them regardless of which operation is running.
"""

from __future__ import annotations

import logging
from time import monotonic, sleep
from typing import TYPE_CHECKING

from smallops._types import (
    BootstrapTimeout,
    Config,
    FatalGate,
    IdleReason,
    PaneDied,
    Response,
    SendTimeout,
)
from smallops._util import strip_ansi

if TYPE_CHECKING:
    from smallops import Session
    from smallops._protocols import Mux, Tui
    from smallops._types import SessionInfo

_log = logging.getLogger(__name__)


def _check_alive(session: Session) -> None:
    """Raise PaneDied if the pane no longer exists."""
    if not session.mux.session_exists(session._session):
        raise PaneDied(f"pane {session._session.id} disappeared")


def _handle_gate(mux: Mux, tui: Tui, pane: SessionInfo, screen: str) -> bool:
    """Check for gate prompt and auto-dismiss if possible.

    Returns True if a gate was dismissed (caller should re-poll).
    Raises FatalGate for gates that require human intervention.
    """
    if tui.is_fatal_gate(screen):
        raise FatalGate("fatal gate detected — requires human intervention")
    response = tui.gate_response(screen)
    if response is None:
        return False
    _log.debug("gate detected, sending response: %r", response)
    mux.send_text(pane, response, enter=True)
    return True


def wait_for_ready(session: Session, config: Config) -> None:
    """Block until the agent TUI shows a ready prompt.

    Handles gates during bootstrap. Raises BootstrapTimeout or FatalGate.
    """
    deadline = monotonic() + config.bootstrap_timeout
    prev = ""
    unchanged = 0
    gate_count = 0
    last_gate_response: str | None = None
    repeated_gate_polls = 0

    while monotonic() < deadline:
        sleep(config.poll_interval)
        _check_alive(session)

        screen = session._read_screen(handle_gates=False)

        gate_response = session.tui.gate_response(screen)
        if gate_response is not None:
            if gate_response == last_gate_response:
                repeated_gate_polls += 1
                if repeated_gate_polls < config.idle_threshold:
                    unchanged = 0
                    continue
            else:
                repeated_gate_polls = 0
            last_gate_response = gate_response
            if session.tui.is_fatal_gate(screen):
                raise FatalGate("fatal gate detected — requires human intervention")
            _log.debug("gate detected, sending response: %r", gate_response)
            session.mux.send_text(session._session, gate_response, enter=True)
            gate_count += 1
            repeated_gate_polls = 0
            if gate_count > config.max_gate_dismissals:
                raise FatalGate("too many gate dismissals during bootstrap")
            unchanged = 0
            continue
        repeated_gate_polls = 0
        last_gate_response = None

        if screen == prev:
            unchanged += 1
        else:
            unchanged = 0
        prev = screen

        if unchanged < config.idle_threshold:
            continue

        reason = session.tui.classify_idle(screen)
        if reason == IdleReason.READY:
            return
        if reason == IdleReason.ERROR:
            raise FatalGate("agent error during bootstrap")

    raise BootstrapTimeout(
        f"agent did not become ready within {config.bootstrap_timeout}s"
    )


def poll_until_done(
    session: Session,
    marker: str,
    config: Config,
    *,
    timeout: float | None = None,
) -> Response:
    """Send has already happened — poll until the agent finishes.

    Uses idle-reset timeout: deadline extends while the screen is changing.
    Hard ceiling prevents infinite waits.
    """
    effective_timeout = timeout or config.timeout
    start = monotonic()
    deadline = start + effective_timeout
    hard_deadline = start + config.hard_ceiling

    prev = ""
    unchanged = 0
    gate_count = 0

    while True:
        now = monotonic()
        if now > hard_deadline:
            raise SendTimeout(f"hard ceiling {config.hard_ceiling}s exceeded")
        if now > deadline:
            raise SendTimeout(f"agent idle for too long (timeout {effective_timeout}s)")

        sleep(config.poll_interval)
        _check_alive(session)

        screen = session._read_screen(handle_gates=False)

        if _handle_gate(session.mux, session.tui, session._session, screen):
            gate_count += 1
            if gate_count > config.max_gate_dismissals:
                raise FatalGate("too many gate dismissals")
            unchanged = 0
            deadline = monotonic() + effective_timeout
            continue

        if screen != prev:
            unchanged = 0
            deadline = monotonic() + effective_timeout  # idle-reset
        else:
            unchanged += 1
        prev = screen

        if unchanged < config.idle_threshold:
            continue

        reason = session.tui.classify_idle(screen)

        if reason == IdleReason.READY:
            break

        if reason == IdleReason.ERROR:
            raise PaneDied("agent error detected on screen")

        if reason == IdleReason.GATE:
            _log.debug("gate or active working indicator with no auto-response; continuing to poll")
            continue

    # Extract response using scrollback
    raw = strip_ansi(session.mux.peek(session._session, n=config.idle_threshold * 100))
    parsed = session.tui.parse_blocks(raw, marker)
    elapsed = monotonic() - start

    return Response(text=parsed.text, raw=raw, elapsed=elapsed, marker=marker, parsed=parsed)


def wait_for_idle(
    session: Session,
    config: Config,
    *,
    timeout: float | None = None,
) -> IdleReason:
    """Block until the agent is idle (any reason).

    Returns the idle reason. Handles gates during the wait.
    """
    effective_timeout = timeout or config.timeout
    deadline = monotonic() + effective_timeout
    hard_deadline = monotonic() + config.hard_ceiling

    prev = ""
    unchanged = 0
    gate_count = 0

    while True:
        now = monotonic()
        if now > hard_deadline or now > deadline:
            raise SendTimeout(f"wait timed out after {effective_timeout}s")

        sleep(config.poll_interval)
        _check_alive(session)

        screen = session._read_screen(handle_gates=False)

        if _handle_gate(session.mux, session.tui, session._session, screen):
            gate_count += 1
            if gate_count > config.max_gate_dismissals:
                raise FatalGate("too many gate dismissals")
            unchanged = 0
            deadline = monotonic() + effective_timeout
            continue

        if screen != prev:
            unchanged = 0
            deadline = monotonic() + effective_timeout
        else:
            unchanged += 1
        prev = screen

        if unchanged < config.idle_threshold:
            continue

        return session.tui.classify_idle(screen)
