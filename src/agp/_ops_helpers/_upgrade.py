"""Upgrade, rollback, and version management."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from agp.config import settings
from agp.db import SessionLocal, current_release_version
from agp.models import SystemMetadata, utc_now


def get_upgrade_status() -> dict:
    session = SessionLocal()
    try:
        entries = {
            row.key: row.value
            for row in session.scalars(select(SystemMetadata)).all()
        }
    finally:
        session.close()
    return {
        "release_version": entries.get("release_version", current_release_version()),
        "schema_version": entries.get("schema_version", "unknown"),
        "previous_release_version": entries.get("previous_release_version"),
        "previous_schema_version": entries.get("previous_schema_version"),
        "package_version": current_release_version(),
        "rollback_target_release_version": entries.get("previous_release_version"),
        "rollback_target_schema_version": entries.get("previous_schema_version"),
    }


def mark_upgrade(*, schema_version: str, release_version: str) -> dict:
    session = SessionLocal()
    try:
        now = utc_now()

        def _get(key: str) -> SystemMetadata | None:
            return session.get(SystemMetadata, key)

        def _set(key: str, value: str) -> None:
            row = _get(key)
            if row is None:
                session.add(SystemMetadata(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

        current_release = _get("release_version")
        current_schema = _get("schema_version")
        if current_release is not None:
            _set("previous_release_version", current_release.value)
        if current_schema is not None:
            _set("previous_schema_version", current_schema.value)
        _set("release_version", release_version)
        _set("schema_version", schema_version)
        session.commit()
    finally:
        session.close()
    return get_upgrade_status()


def rollback_to_previous_version() -> dict:
    session = SessionLocal()
    try:
        now = utc_now()

        def _get(key: str) -> SystemMetadata | None:
            return session.get(SystemMetadata, key)

        def _set(key: str, value: str) -> None:
            row = _get(key)
            if row is None:
                session.add(SystemMetadata(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

        previous_release = _get("previous_release_version")
        previous_schema = _get("previous_schema_version")
        current_release = _get("release_version")
        current_schema = _get("schema_version")
        if previous_release is None or previous_schema is None:
            raise ValueError("no rollback target is currently recorded")
        if current_release is None or current_schema is None:
            raise ValueError("current upgrade metadata is incomplete")

        previous_release_value = previous_release.value
        previous_schema_value = previous_schema.value
        current_release_value = current_release.value
        current_schema_value = current_schema.value

        _set("release_version", previous_release_value)
        _set("schema_version", previous_schema_value)
        _set("previous_release_version", current_release_value)
        _set("previous_schema_version", current_schema_value)
        session.commit()
    finally:
        session.close()
    return get_upgrade_status()
