"""Control plane application assembly.

This module composes the FastAPI application from modular route, service,
middleware, and error-handler components.  It also re-exports symbols that
external code (tests, CLI, skyops) historically imported from here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator

import httpx  # noqa: F401 — kept at module level so tests can patch `control_plane_module.httpx`
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

import agp.services.events as _events_mod
from agp.config import settings
from agp.models import SystemMetadata  # noqa: F401 — re-export for test patching
from agp.services._helpers import _require_job  # noqa: F401
from agp.services.jobs import _block_job, _unblock_job  # noqa: F401

# ── Re-exports for backward compatibility ────────────────────────────
# Tests, CLI, skyops, and _ops_helpers import these from agp.control_plane.
from agp.services.sweep import (  # noqa: F401
    refresh_active_leases,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_stale_agents,
    sweep_stale_runtimes,
)


def __getattr__(name: str):
    """Proxy _event_seq_counter reads to the events module."""
    if name == "_event_seq_counter":
        return _events_mod._event_seq_counter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Route modules
from datetime import UTC

from agp.api.errors import (
    domain_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

# Middleware and error handlers
from agp.api.middleware import auth_middleware
from agp.api.routes import (
    admin,
    agents,
    artifacts,
    jobs,
    ops,
    runs,
    runtimes,
    security,
)

# Auth settings loader
from agp.services._helpers import _load_persisted_auth_settings
from agp.services.exceptions import DomainError


def _is_fatal_local_sqlite_guard_error(exc: Exception) -> bool:
    detail = str(exc)
    return (
        os.environ.get("AGP_ENFORCE_SQLITE_RUNTIME_GUARD") == "1"
        and "configured SQLite database" in detail
        and "while the control plane is running" in detail
    )


CRASH_BREADCRUMB_FILE = ".agp-crash"


def _write_crash_breadcrumb(reason: str) -> None:
    """Write a crash breadcrumb file that survives the restart."""
    import json
    from datetime import datetime

    try:
        breadcrumb = {
            "timestamp": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "reason": reason,
        }
        with open(CRASH_BREADCRUMB_FILE, "w") as f:
            json.dump(breadcrumb, f, indent=2)
            f.write("\n")
    except Exception:
        pass  # best-effort


def _schedule_fatal_local_shutdown() -> None:
    """Terminate the local CP soon after a fatal SQLite guard failure."""
    def _shutdown() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Timer(1.0, _shutdown)
    thread.daemon = True
    thread.start()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup hook: refresh active leases so the sweeper doesn't mass-expire after CP restart."""
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        count = refresh_active_leases(session)
        if count:
            import logging
            logging.getLogger("agp.control_plane").info("Refreshed %d active leases on startup", count)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    yield


def build_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)

    _load_persisted_auth_settings()

    if not settings.runtime_bearer_token and not settings.runtime_active_tokens_json:
        logging.getLogger("agp.control_plane").warning(
            "Runtime auth is not configured (AGP_RUNTIME_BEARER_TOKEN / AGP_RUNTIME_ACTIVE_TOKENS_JSON unset). "
            "Agent registration endpoints (/agents/up, /agents/{id}/down, /runtimes/register, /runs/*) "
            "are unauthenticated. Set AGP_RUNTIME_BEARER_TOKEN to secure them."
        )

    @app.middleware("http")
    async def local_sqlite_guard_middleware(request, call_next):  # type: ignore[override]
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if not _is_fatal_local_sqlite_guard_error(exc):
                raise
            detail = str(exc)
            logging.getLogger("agp.control_plane").critical(
                "Fatal local SQLite state failure detected; terminating control plane: %s",
                exc,
            )
            _write_crash_breadcrumb(detail)
            _schedule_fatal_local_shutdown()
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "database_unavailable",
                        "message": detail,
                        "retryable": False,
                        "hint": "Run `make local-restart` to recover state, or `make local-up` for a clean start.",
                    }
                },
            )

    app.middleware("http")(auth_middleware)

    app.exception_handler(DomainError)(domain_exception_handler)
    app.exception_handler(HTTPException)(http_exception_handler)
    app.exception_handler(RequestValidationError)(validation_exception_handler)
    app.exception_handler(Exception)(generic_exception_handler)

    app.include_router(admin.router)
    app.include_router(jobs.router)
    app.include_router(runs.router)
    app.include_router(agents.router)
    app.include_router(runtimes.router)
    app.include_router(artifacts.router)
    app.include_router(security.router)
    app.include_router(ops.router)

    return app
