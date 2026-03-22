"""Lightweight migration runner for the AGP schema.

Tracks applied versions in the ``system_metadata`` table and applies SQL
files from the ``migrations/`` directory in order.  PostgreSQL environments
run the real migration SQL; SQLite falls back to ``create_all()`` with ORM
constraints, since many Postgres-specific constructs are unsupported.
"""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from agp.db import Base, SessionLocal, engine, current_release_version


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def _is_postgres() -> bool:
    return str(engine.url).startswith("postgresql")


def _current_schema_version(session: Session) -> str | None:
    try:
        row = session.execute(
            text("SELECT value FROM system_metadata WHERE key = 'schema_version'")
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _set_schema_version(session: Session, version: str) -> None:
    from agp.models import SystemMetadata, utc_now

    existing = session.get(SystemMetadata, "schema_version")
    if existing is None:
        session.add(SystemMetadata(key="schema_version", value=version, updated_at=utc_now()))
    else:
        existing.value = version
        existing.updated_at = utc_now()


def _discover_migrations() -> list[tuple[str, Path]]:
    """Return sorted (version_tag, path) pairs for SQL migrations."""
    if not _MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    result = []
    for f in files:
        match = re.match(r"^(\d{4}_\w+)\.sql$", f.name)
        if match:
            result.append((match.group(1), f))
    return result


def _version_ord(tag: str) -> int:
    """Extract the leading integer prefix for ordering."""
    m = re.match(r"^(\d+)", tag)
    return int(m.group(1)) if m else 0


def pending_migrations(session: Session) -> list[tuple[str, Path]]:
    """Return migrations not yet applied."""
    current = _current_schema_version(session)
    current_ord = _version_ord(current) if current else -1
    return [
        (tag, path)
        for tag, path in _discover_migrations()
        if _version_ord(tag) > current_ord
    ]


def apply_migrations(*, force_create_all: bool = False) -> dict:
    """Apply pending migrations.

    On PostgreSQL, executes SQL migration files in order.
    On SQLite (or when *force_create_all* is True), uses ``create_all()``
    to derive schema from ORM models, since SQLite cannot run the
    Postgres-specific DDL.

    Returns a summary dict with keys: applied, current_version, engine.
    """
    from agp import models  # noqa: F401  — ensure all models are imported

    session = SessionLocal()
    try:
        use_sql = _is_postgres() and not force_create_all
        applied: list[str] = []

        # Version incompatibility check: reject if DB schema is ahead of code
        current = _current_schema_version(session)
        if current is not None:
            available = _discover_migrations()
            available_tags = {tag for tag, _ in available}
            current_ord = _version_ord(current)
            max_available_ord = max((_version_ord(t) for t in available_tags), default=0)
            if current_ord > max_available_ord:
                raise RuntimeError(
                    f"database schema version '{current}' is ahead of the code "
                    f"(latest available migration: {max_available_ord:04d}). "
                    f"Upgrade the application or roll back the database."
                )

        if use_sql:
            # Postgres path: run raw SQL migrations
            for tag, path in pending_migrations(session):
                sql = path.read_text(encoding="utf-8")
                # Execute each statement (the migration files use BEGIN/COMMIT)
                session.execute(text(sql))
                _set_schema_version(session, tag)
                applied.append(tag)
            if not applied:
                # No pending migrations — ensure system_metadata exists
                current = _current_schema_version(session)
                if current is None:
                    # First run — apply all
                    for tag, path in _discover_migrations():
                        sql = path.read_text(encoding="utf-8")
                        session.execute(text(sql))
                        _set_schema_version(session, tag)
                        applied.append(tag)
        else:
            # SQLite path: create_all with ORM constraints
            Base.metadata.create_all(bind=engine)
            latest = _discover_migrations()
            version = latest[-1][0] if latest else "0001_initial"
            _set_schema_version(session, version)
            applied.append(f"create_all({version})")

        # Ensure release_version metadata
        release_row = session.get(models.SystemMetadata, "release_version")
        if release_row is None:
            session.add(
                models.SystemMetadata(
                    key="release_version",
                    value=current_release_version(),
                    updated_at=models.utc_now(),
                )
            )

        session.commit()
        current = _current_schema_version(session) or "unknown"
        return {
            "applied": applied,
            "current_version": current,
            "engine": "postgres" if use_sql else "sqlite_create_all",
        }
    finally:
        session.close()


def schema_status() -> dict:
    """Return current schema lifecycle info without modifying anything."""
    session = SessionLocal()
    try:
        current = _current_schema_version(session)
        available = _discover_migrations()
        pending = pending_migrations(session) if current else available
        return {
            "current_version": current or "not_initialized",
            "available_migrations": [tag for tag, _ in available],
            "pending_migrations": [tag for tag, _ in pending],
            "engine": "postgres" if _is_postgres() else "sqlite",
            "release_version": current_release_version(),
        }
    finally:
        session.close()
