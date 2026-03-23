"""Database bootstrap for the AGP scaffold."""

from collections.abc import Generator
from importlib.metadata import PackageNotFoundError, version as package_version

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from agp.config import settings


class Base(DeclarativeBase):
    """Base class for ORM models."""


engine_kwargs: dict[str, object] = {"future": True}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, **engine_kwargs)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[no-redef]  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


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


def get_db() -> Generator:
    """Yield a request-scoped database session."""

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
