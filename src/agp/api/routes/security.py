"""Security and token management route handlers."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from agp.api.helpers import _ok
from agp.config import settings
from agp.db import get_db
from agp.schemas import RotateOperatorTokensRequest, RotateRuntimeTokensRequest
from agp.services._helpers import _set_system_metadata_value
from agp.services.events import _create_event

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
    settings.operator_bearer_token = request.operator_bearer_token
    settings.operator_token_roles_json = dict(request.operator_token_roles_json)
    _set_system_metadata_value(db, "operator_bearer_token", settings.operator_bearer_token)
    _set_system_metadata_value(db, "operator_token_roles_json", json.dumps(settings.operator_token_roles_json, sort_keys=True))
    role_counts: dict[str, int] = {}
    for role in settings.operator_token_roles_json.values():
        role_counts[role] = role_counts.get(role, 0) + 1
    event = _create_event(
        db,
        event_type="system.operator_tokens_rotated",
        body={
            "legacy_admin_token_configured": bool(settings.operator_bearer_token),
            "managed_token_count": len(settings.operator_token_roles_json),
            "managed_role_counts": role_counts,
        },
    )
    db.commit()
    return _ok({
        "rotated": "operator",
        "legacy_admin_token_configured": bool(settings.operator_bearer_token),
        "managed_token_count": len(settings.operator_token_roles_json),
        "managed_role_counts": role_counts,
        "audit_event_id": event.event_id,
    })


@router.post("/system/tokens/runtime")
def system_rotate_runtime_tokens(request: RotateRuntimeTokensRequest, db: Session = Depends(get_db)) -> dict:
    settings.runtime_bearer_token = request.runtime_bearer_token
    settings.runtime_active_tokens_json = list(request.runtime_active_tokens_json)
    _set_system_metadata_value(db, "runtime_bearer_token", settings.runtime_bearer_token)
    _set_system_metadata_value(db, "runtime_active_tokens_json", json.dumps(settings.runtime_active_tokens_json))
    event = _create_event(
        db,
        event_type="system.runtime_tokens_rotated",
        body={
            "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
            "active_token_count": len(settings.runtime_active_tokens_json),
        },
    )
    db.commit()
    return _ok({
        "rotated": "runtime",
        "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
        "active_token_count": len(settings.runtime_active_tokens_json),
        "audit_event_id": event.event_id,
    })
