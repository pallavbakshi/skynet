"""Shared test base for MVP flow regression coverage."""
# ruff: noqa: F401, F811

from __future__ import annotations

import json
import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import mkdtemp
from threading import Thread
from time import sleep
from unittest.mock import patch

import httpx
import typer
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from typer.testing import CliRunner

import agp.control_plane as control_plane_module
import agp.queue_backend as queue_backend_module
from agp._ops_helpers import (
    create_backup_snapshot,
    get_upgrade_status,
    mark_upgrade,
    prune_observability_logs,
    reconstruct_queue_from_state,
    restore_and_recover_snapshot,
    restore_backup_snapshot,
    rollback_to_previous_version,
    run_failure_injection_scenario,
    validate_restored_state,
)
from agp.artifact_store import S3ArtifactStore, reset_artifact_store_state
from agp.cli import app
from agp.client import AgpClient
from agp.config import Settings, settings
from agp.control_plane import (
    _block_job,
    _require_job,
    _unblock_job,
    build_app,
    refresh_active_leases,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_stale_agents,
    sweep_stale_runtimes,
)
from agp.db import Base, SessionLocal, engine, init_db
from agp.enums import HealthStatus, RuntimeStatus
from agp.models import (
    Agent,
    Capability,
    Event,
    Job,
    Lease,
    Message,
    QueueDeliveryRecord,
    Run,
    Runtime,
    utc_now,
)
from agp.queue_backend import get_queue_backend, reset_queue_backend_state
from agp.runtime import (
    ClaudeCodeAdapter,
    CodexAdapter,
    DefaultAgentAdapter,
    InProcessTerminalHost,
    OutputCursor,
    PaneDied,
    RecoverableExecutionError,
    RuntimeClient,
    RuntimeIdentity,
    RuntimeSupervisor,
    _compute_output_delta,
    _OutputAccumulator,
    _strip_ansi,
    build_agent_adapter,
    build_terminal_host,
)
from agp.sweeper import SweeperService
from skyops.cli import app as skyops_app
from tests._base import FakeRedisClient


class _FakeWebhookResponse:
    def raise_for_status(self) -> None:
        return None

class _FakeWebhookClient:
    def __init__(self, sink: list[dict]) -> None:
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, json: dict) -> _FakeWebhookResponse:
        self.sink.append({"url": url, "json": json})
        return _FakeWebhookResponse()


class MvpFlowTestBase(unittest.TestCase):
    def _materialize_terminal_artifacts(
        self,
        names_to_roles: dict[str, str],
        *,
        contents: dict[str, str] | None = None,
    ) -> list[dict]:
        base = Path(mkdtemp(prefix="agp-test-artifacts-"))
        refs: list[dict] = []
        for name, role in names_to_roles.items():
            path = base / name
            content = (contents or {}).get(name, f"{role}:{name}\n")
            path.write_text(content, encoding="utf-8")
            refs.append(
                {
                    "role": role,
                    "storage_ref": path.resolve().as_uri(),
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": path.stat().st_size,
                }
            )
        return refs

    def setUp(self) -> None:
        self.cli_runner = CliRunner()
        self._original_settings = {
            "artifact_root": settings.artifact_root,
            "log_root": settings.log_root,
        }
        self._tmp_root = Path(mkdtemp(prefix="agp-mvp-flow-"))
        settings.artifact_root = self._tmp_root / "artifacts"
        settings.log_root = self._tmp_root / "logs"
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {}
        settings.runtime_active_tokens_json = []
        settings.runtime_bearer_token = None
        settings.queue_backend = "delivery_table"
        settings.redis_url = "redis://test"
        settings.redis_queue_key_prefix = "agp-test"
        settings.artifact_backend = "localfs"
        settings.observability_unreachable_runtime_threshold = 1
        settings.observability_expired_lease_alert_threshold = 3
        settings.observability_dead_letter_alert_threshold = 1
        settings.observability_terminal_failure_sample_size = 3
        settings.observability_terminal_failure_rate_threshold = 0.5
        from agp.queue_backend import _redis as _redis_mod
        _redis_mod._REDIS_CLIENT_FACTORY = None
        reset_queue_backend_state()
        reset_artifact_store_state()
        from agp.services.events import reset_event_seq
        reset_event_seq()
        from tests._base import _reset_sqlite_database
        engine.dispose()
        _reset_sqlite_database()
        init_db()
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        self.client = TestClient(build_app())
        self.agp = AgpClient(http_client=self.client)

    def tearDown(self) -> None:
        self.client.close()
        from tests._base import _reset_sqlite_database
        engine.dispose()
        _reset_sqlite_database()
        for key, value in self._original_settings.items():
            setattr(settings, key, value)
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def _cli_invoke(self, args: list[str]):
        from agp.cli import app as cli_app

        def _mock_setup(mock):
            mock.return_value.__enter__ = lambda s: self.agp
            mock.return_value.__exit__ = lambda *a: None

        with patch("agp.cli._helpers._make_client") as m1, \
             patch("agp.cli._lifecycle._make_client") as m2, \
             patch("agp.cli._status._make_client", new=m1):
            _mock_setup(m1)
            _mock_setup(m2)
            return self.cli_runner.invoke(cli_app, args)


__all__ = [name for name in globals() if not name.startswith("__")]
