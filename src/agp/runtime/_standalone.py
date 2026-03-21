"""Standalone plugin runner for local testing without control-plane semantics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agp.runtime._abc import AgentAdapter, TerminalHost
from agp.runtime._supervisor import _failure_snapshot_payloads
from agp.runtime._types import ArtifactPayload, ExecutionResult


@dataclass(slots=True)
class StandaloneArtifactRecord:
    role: str
    name: str
    path: str
    content_type: str
    size_bytes: int


@dataclass(slots=True)
class StandaloneRunResult:
    ok: bool
    host_kind: str
    adapter_kind: str
    agent_id: str
    session_id: str
    run_id: str
    output_dir: str
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: list[StandaloneArtifactRecord] = field(default_factory=list)
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    exception_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "host_kind": self.host_kind,
            "adapter_kind": self.adapter_kind,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "output_dir": self.output_dir,
            "summary": self.summary,
            "artifacts": [
                {
                    "role": artifact.role,
                    "name": artifact.name,
                    "path": artifact.path,
                    "content_type": artifact.content_type,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in self.artifacts
            ],
            "progress_events": self.progress_events,
            "error": self.error,
            "exception_type": self.exception_type,
        }


class _StandaloneSupervisorContext:
    def __init__(self) -> None:
        self.progress_events: list[dict[str, Any]] = []
        identity = type("Identity", (), {"runtime_id": "standalone-runtime"})()
        self.client = type("Client", (), {"identity": identity})()

    def check_interrupt(self, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        return None

    def emit_progress(self, claimed: dict[str, Any], *, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "message": message,
            "details": details or {},
            "job_id": claimed["job"]["job_id"],
            "run_id": claimed["run"]["run_id"],
        }
        self.progress_events.append(event)
        return event


class StandalonePluginRunner:
    """Run one host+adapter task locally without control-plane semantics."""

    def __init__(
        self,
        *,
        host: TerminalHost,
        adapter: AgentAdapter,
        output_root: str | Path = ".agp-plugin-runs",
    ) -> None:
        self.host = host
        self.adapter = adapter
        self.output_root = Path(output_root)

    def _build_claimed(self, *, agent_id: str, task: str, run_id: str) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "job": {"job_id": f"local-job-{run_id}"},
            "run": {"run_id": run_id},
            "message": {"text": task, "metadata": {"standalone": True}},
            "lease": {"lease_id": f"local-lease-{run_id}", "fencing_token": 1},
        }

    def _write_artifacts(self, *, run_dir: Path, artifacts: list[ArtifactPayload]) -> list[StandaloneArtifactRecord]:
        run_dir.mkdir(parents=True, exist_ok=True)
        written: list[StandaloneArtifactRecord] = []
        for index, artifact in enumerate(artifacts, start=1):
            path = run_dir / f"{index:02d}-{artifact.role}-{artifact.name}"
            path.write_text(artifact.content, encoding="utf-8")
            written.append(
                StandaloneArtifactRecord(
                    role=artifact.role,
                    name=artifact.name,
                    path=str(path),
                    content_type=artifact.content_type,
                    size_bytes=path.stat().st_size,
                )
            )
        return written

    def run_once(
        self,
        *,
        agent_id: str,
        task: str,
        workspace_ref: str | None = None,
        keep_session: bool = False,
        run_id: str | None = None,
    ) -> StandaloneRunResult:
        actual_run_id = run_id or f"plugin-run-{uuid.uuid4().hex[:12]}"
        claimed = self._build_claimed(agent_id=agent_id, task=task, run_id=actual_run_id)
        session = self.host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
        restored = self.host.load_cursor(session)
        if restored is not None:
            session.metadata["restored_cursor"] = restored
        self.adapter.ensure_bootstrapped(host=self.host, session=session, claimed=claimed)
        context = _StandaloneSupervisorContext()
        run_dir = self.output_root / actual_run_id
        try:
            result = self.adapter.execute_run(
                host=self.host,
                session=session,
                claimed=claimed,
                supervisor=context,  # type: ignore[arg-type]
            )
            written = self._write_artifacts(run_dir=run_dir, artifacts=result.artifacts)
            return StandaloneRunResult(
                ok=True,
                host_kind=self.host.kind,
                adapter_kind=self.adapter.kind,
                agent_id=agent_id,
                session_id=session.session_id,
                run_id=actual_run_id,
                output_dir=str(run_dir),
                summary=result.summary,
                artifacts=written,
                progress_events=context.progress_events,
            )
        except Exception as exc:
            failure_result = self.adapter.build_failure_result(
                host=self.host,
                session=session,
                claimed=claimed,
                error=exc,
                supervisor=context,  # type: ignore[arg-type]
            )
            failure_result.artifacts.extend(_failure_snapshot_payloads(host=self.host, session=session, error=exc))
            written = self._write_artifacts(run_dir=run_dir, artifacts=failure_result.artifacts)
            return StandaloneRunResult(
                ok=False,
                host_kind=self.host.kind,
                adapter_kind=self.adapter.kind,
                agent_id=agent_id,
                session_id=session.session_id,
                run_id=actual_run_id,
                output_dir=str(run_dir),
                summary=failure_result.summary,
                artifacts=written,
                progress_events=context.progress_events,
                error=str(exc),
                exception_type=type(exc).__name__,
            )
        finally:
            if not keep_session:
                try:
                    self.host.terminate_session(session)
                except Exception:  # noqa: BLE001
                    pass
