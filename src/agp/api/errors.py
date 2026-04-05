"""Exception handlers for the AGP control plane."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agp.api.helpers import _error_response

_logger = logging.getLogger(__name__)
_INTERNAL_ERROR_MESSAGE = "internal server error"

_CODE_MAP = {
    400: ("invalid_request", False),
    401: ("unauthenticated", False),
    403: ("forbidden", False),
    404: ("not_found", False),
    409: ("conflict", False),
    429: ("rate_limited", True),
}


def _classify_error_code(status_code: int, detail: str) -> tuple[str, bool]:
    """Map an HTTP status and detail string to an (error_code, retryable) pair."""
    code, retryable = _CODE_MAP.get(status_code, ("internal_error", False))
    if "stale fencing token" in detail:
        code = "stale_fencing_token"
    if "lease" in detail and "expired" in detail:
        code = "lease_expired"
    return code, retryable


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code, retryable = _classify_error_code(exc.status_code, detail)
    return _error_response(exc.status_code, code, detail, retryable)


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "invalid_request", str(exc), False)


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.exception(
        "Unhandled exception while serving %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return _error_response(500, "internal_error", _INTERNAL_ERROR_MESSAGE, False)


from agp.services.exceptions import DomainError


async def domain_exception_handler(_: Request, exc: DomainError) -> JSONResponse:
    code, retryable = _classify_error_code(exc.status_code, exc.detail)
    return _error_response(exc.status_code, code, exc.detail, retryable)
