"""Database bootstrap for the AGP scaffold."""

import os
import sqlite3
from collections.abc import Generator
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from agp.config import settings


class Base(DeclarativeBase):
    """Base class for ORM models."""


sqlite3.register_adapter(datetime, lambda value: value.isoformat(sep=" "))


engine_kwargs: dict[str, object] = {"future": True}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, **engine_kwargs)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[no-redef]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin_immediate(conn):  # type: ignore[no-redef]
        # SQLite's default deferred transactions can fail with immediate
        # "database is locked" errors when concurrent requests both upgrade
        # from read to write transactions. Acquiring the write lock at begin
        # time lets busy_timeout apply predictably under local CP load.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
_SQLITE_URL_PREFIX = "sqlite+pysqlite:///"
_sqlite_runtime_state_cache: dict[str, tuple[int, int, int]] = {}


def current_release_version() -> str:
    try:
        return package_version("agp")
    except PackageNotFoundError:
        return "0.1.0"


def is_postgres() -> bool:
    return str(engine.url).startswith("postgresql")


def init_db() -> None:
    """Initialize or migrate the database schema.

    On PostgreSQL, applies pending Postgres-dialect SQL migrations.
    On SQLite, applies pending SQLite-dialect SQL migrations.
    """
    from agp.migrations import apply_migrations

    apply_migrations()


def sqlite_db_path() -> Path | None:
    """Return the configured SQLite database path, if any."""
    if not settings.database_url.startswith(_SQLITE_URL_PREFIX):
        return None
    return Path(settings.database_url.removeprefix(_SQLITE_URL_PREFIX))


def ensure_sqlite_runtime_database_available() -> None:
    """Fail fast if the configured SQLite database vanished or lost its schema.

    A live CP on an unlinked/replaced SQLite file can drift into a split-brain
    state where old connections still work while new ones see an empty database.
    Detect that before request handling opens a new ORM session.
    """
    db_path = sqlite_db_path()
    if db_path is None:
        return
    try:
        stat = db_path.stat()
    except FileNotFoundError as exc:
        _sqlite_runtime_state_cache.pop(str(db_path), None)
        raise RuntimeError(
            "configured SQLite database file is missing while the control plane is running; "
            "run `make local-restart` to recover, or `make local-up` for a clean start"
        ) from exc

    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cache_key = str(db_path)
    if _sqlite_runtime_state_cache.get(cache_key) == signature:
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=1)
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"configured SQLite database is not readable in read/write mode: {exc}"
        ) from exc

    try:
        try:
            row = conn.execute(
                "SELECT value FROM system_metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "no such table" in message:
                raise RuntimeError(
                    "configured SQLite database is missing the AGP schema while the control plane is running; "
                    "run `make local-restart` to recover, or `make local-up` for a clean start"
                ) from exc
            raise RuntimeError(f"unable to verify configured SQLite database schema: {exc}") from exc
        if row is None or not row[0]:
            raise RuntimeError(
                "configured SQLite database is missing schema metadata while the control plane is running; "
                "run `make local-restart` to recover, or `make local-up` for a clean start"
            )
    finally:
        conn.close()

    _sqlite_runtime_state_cache[cache_key] = signature


def get_db() -> Generator:
    """Yield a request-scoped database session."""

    if os.environ.get("AGP_ENFORCE_SQLITE_RUNTIME_GUARD") == "1":
        ensure_sqlite_runtime_database_available()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def next_event_seq_db(session) -> int | None:
    """Allocate the next event sequence number from the database.

    On PostgreSQL, uses the native sequence ``events_event_seq_seq``.
    On SQLite, atomically increments the ``_sqlite_sequences`` table
    (created by the SQLite migration).

    Returns ``None`` only if the sequences table doesn't exist yet
    (pre-migration state).
    """
    if is_postgres():
        result = session.execute(text("SELECT nextval('events_event_seq_seq')"))
        return result.scalar()
    # SQLite: atomic increment on _sqlite_sequences table
    try:
        session.execute(text(
            "UPDATE _sqlite_sequences SET value = value + 1 WHERE name = 'events_event_seq'"
        ))
        row = session.execute(text(
            "SELECT value FROM _sqlite_sequences WHERE name = 'events_event_seq'"
        ))
        val = row.scalar()
        return val
    except Exception:
        return None
