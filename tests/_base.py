"""Shared test base for AGP service-layer tests."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from sqlalchemy import text

from agp._local_state import ensure_local_control_plane_stopped
from agp.config import settings
from agp.db import Base, SessionLocal, engine, init_db
from agp.models import Capability, utc_now
from agp.queue_backend import reset_queue_backend_state
from agp.artifact_store import reset_artifact_store_state
from agp.services.events import reset_event_seq
import agp.queue_backend as queue_backend_module


def _reset_sqlite_database() -> None:
    """Reset the SQLite database backing tests.

    The migration lifecycle now owns schema creation, so the most reliable
    reset is to remove the SQLite database files entirely after disposing
    pooled connections. This avoids table-ordering, foreign-key, and WAL
    lock issues from in-place teardown.
    """
    prefix = "sqlite+pysqlite:///"
    if settings.database_url.startswith(prefix):
        db_path = Path(settings.database_url.removeprefix(prefix))
        repo_root = Path(__file__).resolve().parents[1]
        local_db_path = (repo_root / "agp.db").resolve()
        if db_path.resolve() == local_db_path:
            ensure_local_control_plane_stopped(repo_root / ".skyops-pids" / "control-plane.pid", root=repo_root)
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{db_path}{suffix}")
            if path.exists():
                path.unlink()
        return

    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        tables = [
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).fetchall()
        ]
        for table in tables:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.commit()


def _drop_all_tables_sql() -> None:
    """Backward-compatible alias for older test modules."""
    _reset_sqlite_database()


class FakeRedisClient:
    """Minimal fake Redis for contract testing without a real Redis server."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}

    def flushdb(self) -> None:
        self.lists.clear()
        self.hashes.clear()
        self.sets.clear()

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def lpop(self, key: str) -> str | None:
        values = self.lists.setdefault(key, [])
        return values.pop(0) if values else None

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.setdefault(key, [])
        if end == -1:
            end = len(values) - 1
        return values[start : end + 1]

    def delete(self, key: str) -> None:
        self.lists.pop(key, None)
        self.hashes.pop(key, None)
        self.sets.pop(key, None)

    def hset(self, name: str, key: str, value: str) -> None:
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    def hdel(self, name: str, key: str) -> None:
        self.hashes.setdefault(name, {}).pop(key, None)

    def hkeys(self, name: str) -> list[str]:
        return list(self.hashes.get(name, {}).keys())

    def sadd(self, name: str, value: str) -> None:
        self.sets.setdefault(name, set()).add(value)

    def srem(self, name: str, value: str) -> None:
        self.sets.setdefault(name, set()).discard(value)

    def sismember(self, name: str, value: str) -> bool:
        return value in self.sets.setdefault(name, set())


_ORIGINAL_SETTINGS = {
    "database_url": settings.database_url,
    "artifact_root": settings.artifact_root,
    "log_root": settings.log_root,
}


class AgpTestCase(unittest.TestCase):
    """Base test case that bootstraps a clean AGP database per test."""

    def setUp(self) -> None:
        settings.artifact_root = Path("/tmp/agp-test-artifacts")
        settings.log_root = Path("/tmp/agp-test-logs")
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {}
        settings.runtime_bearer_token = None
        settings.runtime_active_tokens_json = []
        settings.queue_backend = "delivery_table"
        settings.redis_url = "redis://test"
        settings.redis_queue_key_prefix = "agp-test"
        settings.artifact_backend = "localfs"
        settings.observability_unreachable_runtime_threshold = 1
        settings.observability_expired_lease_alert_threshold = 3
        settings.observability_dead_letter_alert_threshold = 1
        settings.observability_terminal_failure_sample_size = 3
        settings.observability_terminal_failure_rate_threshold = 0.5
        queue_backend_module._REDIS_CLIENT_FACTORY = None
        reset_queue_backend_state()
        reset_artifact_store_state()
        reset_event_seq()
        if settings.log_root.exists():
            shutil.rmtree(settings.log_root)
        engine.dispose()
        _reset_sqlite_database()
        init_db()

    def tearDown(self) -> None:
        for key, val in _ORIGINAL_SETTINGS.items():
            setattr(settings, key, val)

    def _seed_capability(self) -> None:
        """Seed a capability record for tests that route to capability targets."""
        session = SessionLocal()
        try:
            session.add(
                Capability(
                    capability_id="cap_python",
                    name="Python Tester",
                    version="v1",
                    image_ref="test/agp:latest",
                    model_ref="test",
                    resource_tier="small",
                    permission_profile="default",
                    queue_mode="agent",
                    runtime_requirements_json={},
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            session.commit()
        finally:
            session.close()

    def _client(self):
        from agp.control_plane import build_app
        from fastapi.testclient import TestClient

        return TestClient(build_app())
