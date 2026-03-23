"""Control plane application assembly.

This module composes the FastAPI application from modular route, service,
middleware, and error-handler components.  It also re-exports symbols that
external code (tests, CLI, skyops) historically imported from here.
"""

from __future__ import annotations

import httpx  # noqa: F401 — kept at module level so tests can patch `control_plane_module.httpx`

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from agp.config import settings
from agp.models import SystemMetadata  # noqa: F401 — re-export for test patching

# ── Re-exports for backward compatibility ────────────────────────────
# Tests, CLI, skyops, and _ops_helpers import these from agp.control_plane.

from agp.services.sweep import (  # noqa: F401
    sweep_draining_agents,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_idle_agents,
    sweep_stale_runtimes,
)
from agp.services.jobs import _block_job, _unblock_job  # noqa: F401
from agp.services._helpers import _require_job  # noqa: F401
import agp.services.events as _events_mod  # noqa: F401


def __getattr__(name: str):
    """Proxy _event_seq_counter reads to the events module."""
    if name == "_event_seq_counter":
        return _events_mod._event_seq_counter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Route modules
from agp.api.routes import admin, agents, artifacts, jobs, observability, runs, runtimes, security  # noqa: F401

# Middleware and error handlers
from agp.api.middleware import auth_middleware
from agp.api.errors import domain_exception_handler, generic_exception_handler, http_exception_handler, validation_exception_handler
from agp.services.exceptions import DomainError

# Auth settings loader
from agp.services._helpers import _load_persisted_auth_settings


def build_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0")

    _load_persisted_auth_settings()

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
    app.include_router(observability.router)
    app.include_router(security.router)

    return app
