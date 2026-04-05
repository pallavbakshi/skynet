"""Background sweeper services."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event
from time import sleep
from typing import Any

_logger = logging.getLogger(__name__)


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
    ) -> list[dict[str, Any]]:
        stop_event = stop_event or Event()
        iterations = 0
        results: list[dict[str, Any]] = []
        while not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            session = self._session_factory()
            try:
                result = self._sweep_fn(session)
                results.append(result)
            except Exception as exc:
                _logger.exception("sweep iteration failed")
                results.append({"error": str(exc)})
            finally:
                session.close()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            stop_event.wait(self._interval_seconds)
        return results


class LeaseSweeperService(SweeperService):
    """Backward-compatible alias for lease expiry sweep loops."""
