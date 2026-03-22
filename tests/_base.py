"""Shared test base for AGP service-layer tests."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from agp.config import settings
from agp.db import Base, SessionLocal, engine, init_db
from agp.models import Capability, CapabilityPool, utc_now
from agp.queue_backend import reset_queue_backend_state
from agp.artifact_store import reset_artifact_store_state
from agp.services.events import reset_event_seq
import agp.queue_backend as queue_backend_module


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
        Base.metadata.drop_all(bind=engine)
        init_db()
        self._seed_capability()

    def tearDown(self) -> None:
        for key, val in _ORIGINAL_SETTINGS.items():
            setattr(settings, key, val)

    def _seed_capability(self) -> None:
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
            session.add(
                CapabilityPool(
                    capability_id="cap_python",
                    queue_id="capability:cap_python:v1",
                    routing_policy="least_recent",
                )
            )
            session.commit()
        finally:
            session.close()

    def _client(self):
        from agp.control_plane import build_app
        from fastapi.testclient import TestClient

        return TestClient(build_app())
