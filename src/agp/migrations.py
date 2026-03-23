"""Lightweight migration runner for the AGP schema.

Tracks applied versions in the ``system_metadata`` table and applies SQL
files from the ``migrations/`` directory in order.  PostgreSQL environments
run the Postgres-dialect SQL; SQLite environments run SQLite-dialect SQL
(``*.sqlite.sql``).
"""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from agp.db import Base, SessionLocal, engine, current_release_version


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def _execute_sql(session: Session, sql: str) -> None:
    """Execute a SQL migration script, handling multi-statement files.

    SQLite's ``cursor.execute()`` cannot run multiple statements in one
    call, so we split on semicolons (outside of string literals) and
    execute each statement individually.  Postgres handles the full
    script in a single ``execute()`` call.
    """
    if _is_postgres():
        session.execute(text(sql))
        return
    # SQLite: split and execute statements individually
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if not stmt or stmt.upper() in ("BEGIN", "COMMIT", "END"):
            continue
        session.execute(text(stmt))


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


def _discover_migrations(*, dialect: str = "postgres") -> list[tuple[str, Path]]:
    """Return sorted (version_tag, path) pairs for SQL migrations.

    When dialect is "sqlite", looks for *.sqlite.sql files.
    When dialect is "postgres", looks for *.sql files excluding *.sqlite.sql.
    """
    if not _MIGRATIONS_DIR.is_dir():
        return []
    if dialect == "sqlite":
        files = sorted(_MIGRATIONS_DIR.glob("*.sqlite.sql"))
        result = []
        for f in files:
            match = re.match(r"^(\d{4}_\w+)\.sqlite\.sql$", f.name)
            if match:
                result.append((match.group(1), f))
        return result
    # Postgres: *.sql but not *.sqlite.sql
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    result = []
    for f in files:
        if f.name.endswith(".sqlite.sql"):
            continue
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
    dialect = "postgres" if _is_postgres() else "sqlite"
    return [
        (tag, path)
        for tag, path in _discover_migrations(dialect=dialect)
        if _version_ord(tag) > current_ord
    ]


def apply_migrations(*, force_create_all: bool = False) -> dict:
    """Apply pending migrations.

    On PostgreSQL, executes Postgres-dialect SQL migration files.
    On SQLite, executes SQLite-dialect SQL migration files.

    The *force_create_all* parameter is accepted for backward compatibility
    but no longer changes behavior — both dialects run real migrations.

    Returns a summary dict with keys: applied, current_version, engine.
    """
    from agp import models  # noqa: F401  — ensure all models are imported

    session = SessionLocal()
    try:
        dialect = "postgres" if _is_postgres() else "sqlite"
        applied: list[str] = []

        # Version incompatibility check: reject if DB schema is ahead of code
        current = _current_schema_version(session)
        if current is not None:
            available = _discover_migrations(dialect=dialect)
            available_tags = {tag for tag, _ in available}
            current_ord = _version_ord(current)
            max_available_ord = max((_version_ord(t) for t in available_tags), default=0)
            if current_ord > max_available_ord:
                raise RuntimeError(
                    f"database schema version '{current}' is ahead of the code "
                    f"(latest available migration: {max_available_ord:04d}). "
                    f"Upgrade the application or roll back the database."
                )

        # Both Postgres and SQLite: run SQL migrations
        for tag, path in pending_migrations(session):
            sql = path.read_text(encoding="utf-8")
            _execute_sql(session, sql)
            _set_schema_version(session, tag)
            applied.append(tag)
        if not applied:
            # No pending migrations — ensure system_metadata exists
            current = _current_schema_version(session)
            if current is None:
                # First run — apply all
                for tag, path in _discover_migrations(dialect=dialect):
                    sql = path.read_text(encoding="utf-8")
                    _execute_sql(session, sql)
                    _set_schema_version(session, tag)
                    applied.append(tag)

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
            "engine": "postgres" if _is_postgres() else "sqlite",
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
