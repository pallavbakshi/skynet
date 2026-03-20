"""Database bootstrap for the AGP scaffold."""

from collections.abc import Generator
from importlib.metadata import PackageNotFoundError, version as package_version

from sqlalchemy import create_engine, event
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


def init_db() -> None:
    """Create all known tables for the initial scaffold."""

    from agp import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        existing = session.get(models.SystemMetadata, "schema_version")
        if existing is None:
            session.add(models.SystemMetadata(key="schema_version", value="0001_initial", updated_at=models.utc_now()))
        existing = session.get(models.SystemMetadata, "release_version")
        if existing is None:
            session.add(
                models.SystemMetadata(
                    key="release_version",
                    value=current_release_version(),
                    updated_at=models.utc_now(),
                )
            )
        session.commit()
    finally:
        session.close()


def get_db() -> Generator:
    """Yield a request-scoped database session."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
