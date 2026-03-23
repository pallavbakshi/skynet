"""Security and token management route handlers."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from agp.api.helpers import _ok
from agp.config import settings
from agp.db import get_db
from agp.schemas import RotateOperatorTokensRequest, RotateRuntimeTokensRequest
from agp.services.security import rotate_operator_tokens_service, rotate_runtime_tokens_service

router = APIRouter()


@router.get("/system/auth-status")
def system_auth_status() -> dict:
    role_counts: dict[str, int] = {}
    for role in settings.operator_token_roles_json.values():
        role_counts[role] = role_counts.get(role, 0) + 1
    return _ok({
        "operator": {
            "legacy_admin_token_configured": bool(settings.operator_bearer_token),
            "managed_token_count": len(settings.operator_token_roles_json),
            "managed_role_counts": role_counts,
        },
        "runtime": {
            "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
            "active_token_count": len(settings.runtime_active_tokens_json),
        },
    })


@router.post("/system/tokens/operator")
def system_rotate_operator_tokens(request: RotateOperatorTokensRequest, db: Session = Depends(get_db)) -> dict:
    result = rotate_operator_tokens_service(db, operator_bearer_token=request.operator_bearer_token, operator_token_roles_json=request.operator_token_roles_json)
    return _ok(result)


@router.post("/system/tokens/runtime")
def system_rotate_runtime_tokens(request: RotateRuntimeTokensRequest, db: Session = Depends(get_db)) -> dict:
    result = rotate_runtime_tokens_service(db, runtime_bearer_token=request.runtime_bearer_token, runtime_active_tokens_json=request.runtime_active_tokens_json)
    return _ok(result)
