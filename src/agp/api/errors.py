"""Exception handlers for the AGP control plane."""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agp.api.helpers import _error_response


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    code_map = {
        400: ("invalid_request", False),
        401: ("unauthenticated", False),
        403: ("forbidden", False),
        404: ("not_found", False),
        409: ("conflict", False),
        429: ("rate_limited", True),
    }
    code, retryable = code_map.get(exc.status_code, ("internal_error", False))
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if "stale fencing token" in detail:
        code = "stale_fencing_token"
    if "lease" in detail and "expired" in detail:
        code = "lease_expired"
    return _error_response(exc.status_code, code, detail, retryable)


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "invalid_request", str(exc), False)


async def generic_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    return _error_response(500, "internal_error", str(exc), False)


from agp.services.exceptions import DomainError


async def domain_exception_handler(_: Request, exc: DomainError) -> JSONResponse:
    code_map = {
        400: ("invalid_request", False),
        401: ("unauthenticated", False),
        403: ("forbidden", False),
        404: ("not_found", False),
        409: ("conflict", False),
        429: ("rate_limited", True),
    }
    code, retryable = code_map.get(exc.status_code, ("internal_error", False))
    if "stale fencing token" in exc.detail:
        code = "stale_fencing_token"
    if "lease" in exc.detail and "expired" in exc.detail:
        code = "lease_expired"
    return _error_response(exc.status_code, code, exc.detail, retryable)
