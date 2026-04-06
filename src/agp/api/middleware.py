"""Auth middleware for the AGP control plane."""

from __future__ import annotations

from fastapi import Request

from agp.api.helpers import _error_response
from agp.config import settings


_OPERATOR_ROLE_RANK = {
    "read_only": 1,
    "operator": 2,
    "lifecycle": 3,
    "security_admin": 4,
}


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _required_operator_role(method: str, path: str) -> str | None:
    if path == "/system/auth-status" or path.startswith("/system/tokens/"):
        return "security_admin"
    if not (
        path.startswith("/messages/")
        or path.startswith("/jobs")
        or path.startswith("/system")
        or path.startswith("/observability")
        or path.startswith("/queue")
        or path.startswith("/agents")
        or path.startswith("/capabilities")
        or path.startswith("/artifacts")
        or path.startswith("/nudges")
        or path.startswith("/ops")
        or (path.startswith("/runtimes") and path != "/runtimes/register")
    ):
        return None

    if method == "GET":
        return "read_only"
    if path.startswith("/messages/"):
        return "operator"
    if path.endswith("/interrupt") or path.endswith("/handoff"):
        return "operator"
    if path.startswith("/agents"):
        return "lifecycle"
    if path.startswith("/ops"):
        return "operator"
    return "security_admin"


def _operator_role_for_token(token: str | None) -> str | None:
    if token is None:
        return None
    if settings.operator_bearer_token and token == settings.operator_bearer_token:
        return "security_admin"
    return settings.operator_token_roles_json.get(token)


def _runtime_token_allowed(token: str | None) -> bool:
    if token is None:
        return False
    if settings.runtime_bearer_token and token == settings.runtime_bearer_token:
        return True
    return token in settings.runtime_active_tokens_json


async def auth_middleware(request: Request, call_next):  # type: ignore[override]
    path = request.url.path
    method = request.method.upper()
    if path == "/health":
        return await call_next(request)

    token = _extract_bearer_token(request.headers.get("Authorization"))
    is_runtime_write = (
        path == "/runtimes/register"
        or path.startswith("/runs/")
        or path == "/agents/up"
        or (path.startswith("/agents/") and path.endswith("/down"))
        or (path.startswith("/runtimes/") and path.endswith("/peek-result"))
    )
    required_role = _required_operator_role(method, path)
    # Runtime-write endpoints are authed by runtime token, not operator token
    is_operator_surface = required_role is not None and not is_runtime_write

    if is_runtime_write and (settings.runtime_bearer_token or settings.runtime_active_tokens_json):
        if not _runtime_token_allowed(token):
            # /agents/{id}/down also accepts operator tokens (force-delete
            # requires lifecycle role, checked in the route handler).
            is_agent_down = path.startswith("/agents/") and path.endswith("/down")
            if not (is_agent_down and _operator_role_for_token(token) is not None):
                return _error_response(401, "unauthenticated", "runtime authentication required", False)
    if is_operator_surface and (settings.operator_bearer_token or settings.operator_token_roles_json):
        role = _operator_role_for_token(token)
        if role is None:
            return _error_response(401, "unauthenticated", "operator authentication required", False)
        if _OPERATOR_ROLE_RANK[role] < _OPERATOR_ROLE_RANK[required_role]:
            return _error_response(403, "forbidden", "operator role insufficient for requested action", False)

    return await call_next(request)
