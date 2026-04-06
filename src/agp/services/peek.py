"""In-memory peek request/response store.

Provides a thread-safe ephemeral store for terminal peek requests
(operator → CP → runtime → CP → operator). Entries auto-expire after
TTL_SECONDS.

NOTE: This is an in-memory singleton — it only works with a single-process
CP deployment. Multi-worker setups (gunicorn workers > 1) will silently
fail because the POST and heartbeat may hit different processes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic


@dataclass(slots=True)
class PeekRequest:
    request_id: str
    runtime_id: str
    lines: int  # 0 = visible screen only
    created_at: float  # monotonic timestamp


@dataclass(slots=True)
class PeekResult:
    request_id: str
    text: str
    session_id: str
    host_kind: str
    captured_at: datetime


class PeekStore:
    """Thread-safe ephemeral store for peek requests and results."""

    TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PeekRequest] = {}  # runtime_id → request
        self._results: dict[str, PeekResult] = {}  # request_id → result

    def request_peek(self, *, runtime_id: str, lines: int, request_id: str) -> PeekRequest:
        """Create or replace a pending peek request for a runtime."""
        req = PeekRequest(
            request_id=request_id,
            runtime_id=runtime_id,
            lines=lines,
            created_at=monotonic(),
        )
        with self._lock:
            self._gc()
            self._pending[runtime_id] = req
        return req

    def get_pending(self, runtime_id: str) -> PeekRequest | None:
        """Return the pending request for a runtime without removing it.

        The request stays in the store until submit_result is called or
        it expires via GC. This way, if the runtime fails to capture,
        the next heartbeat will retry.
        """
        with self._lock:
            self._gc()
            return self._pending.get(runtime_id)

    def consume_pending(self, runtime_id: str) -> None:
        """Remove a pending request after successful result submission."""
        with self._lock:
            self._pending.pop(runtime_id, None)

    def submit_result(
        self,
        request_id: str,
        *,
        runtime_id: str,
        text: str,
        session_id: str,
        host_kind: str,
    ) -> None:
        """Runtime submits captured terminal text and consumes the pending request."""
        result = PeekResult(
            request_id=request_id,
            text=text,
            session_id=session_id,
            host_kind=host_kind,
            captured_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._gc()
            self._results[request_id] = result
            self._pending.pop(runtime_id, None)

    def get_result(self, request_id: str) -> PeekResult | None:
        """Poll for a result. Returns None if not yet available."""
        with self._lock:
            return self._results.get(request_id)

    def _gc(self) -> None:
        """Remove entries older than TTL_SECONDS. Must hold _lock."""
        now = monotonic()
        cutoff = now - self.TTL_SECONDS
        self._pending = {
            k: v for k, v in self._pending.items() if v.created_at > cutoff
        }
        utc_cutoff = datetime.now(timezone.utc).timestamp() - self.TTL_SECONDS
        self._results = {
            k: v for k, v in self._results.items()
            if v.captured_at.timestamp() > utc_cutoff
        }


# Module-level singleton
peek_store = PeekStore()
