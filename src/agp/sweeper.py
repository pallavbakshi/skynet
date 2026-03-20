"""Background sweeper services."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from time import sleep
from typing import Any


class SweeperService:
    """Run a sweep function on a fixed interval."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        sweep_fn: Callable[..., dict[str, int]],
        interval_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._sweep_fn = sweep_fn
        self._interval_seconds = interval_seconds

    def run_forever(
        self,
        *,
        max_iterations: int | None = None,
        stop_event: Event | None = None,
    ) -> list[dict[str, int]]:
        stop_event = stop_event or Event()
        iterations = 0
        results: list[dict[str, int]] = []
        while not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            session = self._session_factory()
            try:
                results.append(self._sweep_fn(session))
            finally:
                session.close()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            stop_event.wait(self._interval_seconds)
        return results


class LeaseSweeperService(SweeperService):
    """Backward-compatible alias for lease expiry sweep loops."""
