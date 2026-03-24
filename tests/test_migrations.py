"""Tests for the migration framework and schema lifecycle."""

from __future__ import annotations

import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory

from agp.config import settings
from agp.db import Base, SessionLocal, engine, init_db
from agp.migrations import (
    apply_migrations,
    schema_status,
    _current_schema_version,
    _discover_migrations,
    _resolve_migrations_dir,
)
from tests._base import _reset_sqlite_database


_ORIGINAL_DATABASE_URL = settings.database_url


class MigrationInitTest(unittest.TestCase):
    """Verify schema initialization via the migration runner."""

    def setUp(self) -> None:
        engine.dispose()
        _reset_sqlite_database()

    def tearDown(self) -> None:
        settings.database_url = _ORIGINAL_DATABASE_URL

    def test_init_db_sets_schema_version(self) -> None:
        init_db()
        session = SessionLocal()
        try:
            from agp.models import SystemMetadata
            row = session.get(SystemMetadata, "schema_version")
            self.assertIsNotNone(row)
            self.assertNotEqual(row.value, "")
        finally:
            session.close()

    def test_init_db_sets_release_version(self) -> None:
        init_db()
        session = SessionLocal()
        try:
            from agp.models import SystemMetadata
            row = session.get(SystemMetadata, "release_version")
            self.assertIsNotNone(row)
        finally:
            session.close()

    def test_init_db_is_idempotent(self) -> None:
        init_db()
        init_db()  # second call should not raise
        status = schema_status()
        self.assertNotEqual(status["current_version"], "not_initialized")

    def test_schema_status_reports_engine_and_version(self) -> None:
        init_db()
        status = schema_status()
        self.assertEqual(status["engine"], "sqlite")
        self.assertIn("current_version", status)
        self.assertEqual(status["pending_migrations"], [])

    def test_apply_migrations_returns_summary(self) -> None:
        result = apply_migrations()
        self.assertIn("applied", result)
        self.assertIn("current_version", result)
        self.assertIn("engine", result)
        self.assertEqual(result["engine"], "sqlite")

    def test_discover_migrations_finds_sql_files(self) -> None:
        migrations = _discover_migrations()
        self.assertGreater(len(migrations), 0)
        tags = [tag for tag, _ in migrations]
        self.assertIn("0001_initial", tags)


class CheckConstraintEnforcementTest(unittest.TestCase):
    """Verify that ORM CheckConstraints reject invalid values on SQLite."""

    def setUp(self) -> None:
        engine.dispose()
        _reset_sqlite_database()
        init_db()

    def tearDown(self) -> None:
        settings.database_url = _ORIGINAL_DATABASE_URL

    def test_invalid_job_status_rejected(self) -> None:
        from agp.models import Job, Message, Capability, utc_now
        from sqlalchemy.exc import IntegrityError
        session = SessionLocal()
        try:
            session.add(Capability(capability_id="cap_t", name="t", version="v1", image_ref="", model_ref="", resource_tier="small", permission_profile="default", queue_mode="agent", created_at=utc_now(), updated_at=utc_now()))
            session.add(Message(message_id="msg_t", target_type="agent", target_id="x", text="t", created_at=utc_now()))
            session.flush()
            session.add(Job(job_id="job_t", message_id="msg_t", target_queue="agent:x", status="INVALID_STATUS", created_at=utc_now(), updated_at=utc_now()))
            with self.assertRaises(IntegrityError):
                session.flush()
        finally:
            session.rollback()
            session.close()

    def test_invalid_runtime_status_rejected(self) -> None:
        from agp.models import Runtime, utc_now
        from sqlalchemy.exc import IntegrityError
        session = SessionLocal()
        try:
            session.add(Runtime(runtime_id="rtm_bad", hostname="h", status="BOGUS", health_status="healthy", last_seen_at=utc_now(), created_at=utc_now(), updated_at=utc_now()))
            with self.assertRaises(IntegrityError):
                session.flush()
        finally:
            session.rollback()
            session.close()

    def test_invalid_lease_status_rejected(self) -> None:
        from agp.models import Lease, utc_now
        from sqlalchemy.exc import IntegrityError
        session = SessionLocal()
        try:
            session.add(Lease(lease_id="l_bad", run_id="r", agent_id="a", runtime_id="rt", fencing_token=1, status="NOPE", expires_at=utc_now(), created_at=utc_now()))
            with self.assertRaises(IntegrityError):
                session.flush()
        finally:
            session.rollback()
            session.close()


class MigrationSessionRecoveryTest(unittest.TestCase):
    """Verify migration helpers recover cleanly from pre-schema probes."""

    def test_current_schema_version_rolls_back_failed_probe(self) -> None:
        session = Mock()
        session.execute.side_effect = RuntimeError("system_metadata missing")

        result = _current_schema_version(session)

        self.assertIsNone(result)
        session.rollback.assert_called_once_with()

    def test_resolve_migrations_dir_falls_back_to_cwd_when_installed_layout_lacks_bundle(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "migrations").mkdir()
            anchor = tmp / "site-packages" / "agp" / "migrations.py"
            anchor.parent.mkdir(parents=True)
            anchor.write_text("# stub", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp)
                resolved = _resolve_migrations_dir(anchor)
            finally:
                os.chdir(old_cwd)
            self.assertEqual(resolved, tmp / "migrations")


class VersionIncompatibilityTest(unittest.TestCase):
    """Verify that apply_migrations rejects a DB schema ahead of code."""

    def setUp(self) -> None:
        engine.dispose()
        _reset_sqlite_database()
        init_db()

    def tearDown(self) -> None:
        settings.database_url = _ORIGINAL_DATABASE_URL

    def test_future_schema_version_raises(self) -> None:
        from agp.models import SystemMetadata, utc_now
        session = SessionLocal()
        try:
            row = session.get(SystemMetadata, "schema_version")
            row.value = "9999_future"
            row.updated_at = utc_now()
            session.commit()
        finally:
            session.close()
        with self.assertRaises(RuntimeError) as ctx:
            apply_migrations()
        self.assertIn("ahead of the code", str(ctx.exception))
