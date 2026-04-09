"""Response formatting and pagination helpers for the API layer."""

from __future__ import annotations

import base64
import json
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy import func, select


def _ok(data: object) -> dict:
    return {"ok": True, "data": data}


def _page(items: list[dict], *, limit: int, next_cursor: str | None) -> dict:
    return {
        "items": items,
        "page": {"limit": limit, "next_cursor": next_cursor, "has_more": next_cursor is not None},
    }


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 6)


def _serialize(model: object, fields: tuple[str, ...]) -> dict:
    return {field: getattr(model, field) for field in fields}


def _serialize_artifact_with_role(artifact: object, role: str) -> dict:
    payload = _serialize(
        artifact,
        ("artifact_id", "job_id", "run_id", "kind", "content_type", "storage_ref", "checksum", "size_bytes", "created_at"),
    )
    payload["role"] = role
    return payload


def _error_response(status_code: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"code": code, "message": message, "retryable": retryable}},
    )


def _encode_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, object] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def _cursor_field(cursor_payload: dict[str, object], field: str) -> object:
    """Extract a required field from a decoded cursor, or 400."""
    try:
        return cursor_payload[field]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"invalid cursor: missing {field}")


def _apply_created_cursor(query, model, cursor: str | None):
    cursor_payload = _decode_cursor(cursor)
    if cursor_payload is None:
        return query
    created_at = cursor_payload.get("created_at")
    entity_id = cursor_payload.get("id")
    if not isinstance(created_at, str) or not isinstance(entity_id, str):
        raise HTTPException(status_code=400, detail="invalid cursor")
    try:
        created_dt = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    pk_col = getattr(model, model.__mapper__.primary_key[0].key)
    return query.where(
        or_(
            model.created_at < created_dt,
            (model.created_at == created_dt) & (pk_col < entity_id),
        )
    )


def _count_by(db: Session, model, column, values: list[str]) -> dict[str, int]:
    return {
        value: int(db.scalar(select(func.count()).select_from(model).where(column == value)) or 0)
        for value in values
    }


def _prom_metric(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    escaped = []
    for key, item in labels.items():
        value_text = str(item).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'{key}="{value_text}"')
    return f"{name}{{{','.join(escaped)}}} {value}"
