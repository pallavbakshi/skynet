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

    On PostgreSQL, applies pending SQL migrations from ``migrations/``.
    On SQLite, uses ``create_all()`` from ORM metadata (which now
    includes CheckConstraints aligned with the migration SQL).
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
    """Allocate the next event sequence from the database sequence (Postgres only).

    Returns the next sequence value, or None if not on PostgreSQL.
    """
    if not is_postgres():
        return None
    result = session.execute(text("SELECT nextval('events_event_seq_seq')"))
    return result.scalar()
