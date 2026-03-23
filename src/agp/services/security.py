"""Security domain operations — token rotation."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from agp.config import settings
from agp.services._helpers import _set_system_metadata_value
from agp.services.events import _create_event


def rotate_operator_tokens_service(
    db: Session,
    *,
    operator_bearer_token: str | None,
    operator_token_roles_json: dict[str, str],
) -> dict:
    settings.operator_bearer_token = operator_bearer_token
    settings.operator_token_roles_json = dict(operator_token_roles_json)
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
    return {
        "rotated": "operator",
        "legacy_admin_token_configured": bool(settings.operator_bearer_token),
        "managed_token_count": len(settings.operator_token_roles_json),
        "managed_role_counts": role_counts,
        "audit_event_id": event.event_id,
    }


def rotate_runtime_tokens_service(
    db: Session,
    *,
    runtime_bearer_token: str | None,
    runtime_active_tokens_json: list[str],
) -> dict:
    settings.runtime_bearer_token = runtime_bearer_token
    settings.runtime_active_tokens_json = list(runtime_active_tokens_json)
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
    return {
        "rotated": "runtime",
        "legacy_runtime_token_configured": bool(settings.runtime_bearer_token),
        "active_token_count": len(settings.runtime_active_tokens_json),
        "audit_event_id": event.event_id,
    }
