"""Lightweight migration runner for the AGP schema.

Tracks applied versions in the ``system_metadata`` table and applies SQL
files from the ``migrations/`` directory in order.  PostgreSQL environments
run the Postgres-dialect SQL; SQLite environments run SQLite-dialect SQL
(``*.sqlite.sql``).
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agp.db import SessionLocal, current_release_version, engine


def _resolve_migrations_dir(anchor: Path | None = None) -> Path:
    """Locate the migrations directory across source and installed layouts."""
    anchor = (anchor or Path(__file__)).resolve()
    candidates = [
        anchor.parent.parent.parent / "migrations",  # source checkout: src/agp/migrations.py -> repo/migrations
        Path.cwd() / "migrations",  # invoked from repo root or mounted app dir
        Path("/app/migrations"),  # Docker image layout
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


_MIGRATIONS_DIR = _resolve_migrations_dir()


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

    def _strip_sql_comments(stmt: str) -> str:
        lines = []
        for line in stmt.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    # SQLite: split and execute statements individually
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        canonical = _strip_sql_comments(stmt)
        if not canonical or canonical.upper() in ("BEGIN", "COMMIT", "END"):
            continue
        try:
            session.execute(text(stmt))
        except Exception as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue  # idempotent: column/table already present
            raise


def _is_postgres() -> bool:
    return str(engine.url).startswith("postgresql")


def _current_schema_version(session: Session) -> str | None:
    try:
        row = session.execute(
            text("SELECT value FROM system_metadata WHERE key = 'schema_version'")
        ).fetchone()
        return row[0] if row else None
    except Exception:
        session.rollback()
        return None


def _probe_schema_version(session: Session) -> str | None:
    """Return schema version, distinguishing missing schema from other DB failures."""
    try:
        row = session.execute(
            text("SELECT value FROM system_metadata WHERE key = 'schema_version'")
        ).fetchone()
        return row[0] if row else None
    except SQLAlchemyError as exc:
        session.rollback()
        message = str(exc).lower()
        missing_markers = (
            "no such table",
            "does not exist",
            "undefined table",
        )
        if any(marker in message for marker in missing_markers):
            return None
        raise


def _set_schema_version(session: Session, version: str) -> None:
    from agp.models import SystemMetadata, utc_now

    # Expire cached state so session.get() sees rows created by raw SQL migrations
    session.expire_all()
    existing = session.get(SystemMetadata, "schema_version")
    if existing is None:
        session.add(SystemMetadata(key="schema_version", value=version, updated_at=utc_now()))
        session.flush()
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
    from agp import models

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
        dialect = "postgres" if _is_postgres() else "sqlite"
        available = _discover_migrations(dialect=dialect)
        pending = pending_migrations(session) if current else available
        return {
            "current_version": current or "not_initialized",
            "available_migrations": [tag for tag, _ in available],
            "pending_migrations": [tag for tag, _ in pending],
            "engine": dialect,
            "release_version": current_release_version(),
        }
    finally:
        session.close()


def require_initialized_schema() -> None:
    """Raise when the configured database has no initialized AGP schema."""
    session = SessionLocal()
    try:
        current = _probe_schema_version(session)
        if current is None:
            raise RuntimeError(
                "database schema is missing or uninitialized; run `agp initdb` before `agp serve`"
            )
    finally:
        session.close()
