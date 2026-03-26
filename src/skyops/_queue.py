"""``skyops queue``, ``skyops job``, ``skyops sweep`` — direct DB operations."""

from __future__ import annotations

import json
import subprocess

import typer
from sqlalchemy import func, select

from skyops.config import SkyopsConfig, load_config

queue_app = typer.Typer(help="Queue management commands.")
job_app = typer.Typer(help="Job management commands.")
sweep_app = typer.Typer(help="One-shot sweep operations.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _docker_exec_python(
    cfg: SkyopsConfig,
    code: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    cmd = [
        "docker", "compose",
        "-f", cfg.stack.compose_file,
        "-p", cfg.stack.project_name,
        "exec", "-T",
    ]
    for key, value in (env or {}).items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.extend(["control-plane", "python", "-c", code])
    subprocess.run(cmd, check=True, timeout=30)


def _redis_list(client, key: str) -> list[str]:
    return list(getattr(client, "lrange", lambda k, s, e: [])(key, 0, -1))


def _redis_set_members(client, key: str) -> list[str]:
    smembers = getattr(client, "smembers", None)
    if callable(smembers):
        values = smembers(key)
        return sorted(str(value) for value in values)
    return sorted(list(getattr(client, "sets", {}).get(key, set())))


def _redis_scan_keys(client, pattern: str) -> list[str]:
    scan_iter = getattr(client, "scan_iter", None)
    if callable(scan_iter):
        return sorted(str(value) for value in scan_iter(match=pattern))
    prefixes = pattern.rstrip("*")
    values = set(getattr(client, "lists", {}).keys())
    values.update(getattr(client, "sets", {}).keys())
    values.update(getattr(client, "hashes", {}).keys())
    return sorted(key for key in values if key.startswith(prefixes))


def _inspect_queue_state() -> dict[str, object]:
    from agp.config import settings
    from agp.db import SessionLocal
    from agp.models import Job, QueueDeliveryRecord
    from agp.queue_backend import RedisQueueBackend, InMemoryBrokerQueueBackend, get_queue_backend

    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        delivery_counts = dict(
            session.execute(
                select(QueueDeliveryRecord.state, func.count())
                .group_by(QueueDeliveryRecord.state)
            ).all()
        )
        queue_rows = session.execute(
            select(
                QueueDeliveryRecord.target_queue,
                QueueDeliveryRecord.state,
                func.count(),
            ).group_by(QueueDeliveryRecord.target_queue, QueueDeliveryRecord.state)
        ).all()
        queue_summary: dict[str, dict[str, int]] = {}
        for target_queue, state, count in queue_rows:
            queue_summary.setdefault(target_queue, {})[state] = int(count)
        result: dict[str, object] = {
            "backend": settings.queue_backend,
            "deliveries": delivery_counts,
            "queues": queue_summary,
        }

        if isinstance(backend, InMemoryBrokerQueueBackend):
            result["transport"] = {
                "queues": {name: list(values) for name, values in backend._queues.items()},
                "inflight": {
                    delivery_id: {
                        "job_id": item.job_id,
                        "target_queue": item.target_queue,
                        "delivery_attempt": item.delivery_attempt,
                    }
                    for delivery_id, item in backend._inflight.items()
                },
                "dead_lettered_jobs": sorted(backend._dead_lettered_jobs),
            }
        elif isinstance(backend, RedisQueueBackend):
            target_queues = {
                row[0]
                for row in session.execute(
                    select(Job.target_queue).distinct().where(Job.target_queue.is_not(None))
                ).all()
                if row[0]
            }
            target_queues.update(queue_summary.keys())
            queue_prefix = f"{backend.key_prefix}:queue:"
            pending_suffix = ":pending"
            for key in _redis_scan_keys(backend.client, f"{queue_prefix}*"):
                if not key.startswith(queue_prefix):
                    continue
                target_queue = key.removeprefix(queue_prefix)
                if target_queue.endswith(pending_suffix):
                    target_queue = target_queue[: -len(pending_suffix)]
                if target_queue:
                    target_queues.add(target_queue)
            inflight_keys = list(getattr(backend.client, "hkeys", lambda name: [])(backend._inflight_hash_key()))
            inflight_payloads = {
                delivery_id: json.loads(backend.client.hget(backend._inflight_hash_key(), delivery_id) or "{}")
                for delivery_id in inflight_keys
            }
            for payload in inflight_payloads.values():
                target_queue = payload.get("target_queue")
                if target_queue:
                    target_queues.add(str(target_queue))
            transport_queues: dict[str, dict[str, object]] = {}
            for target_queue in sorted(target_queues):
                queue_key = backend._queue_key(target_queue)
                pending_key = backend._pending_set_key(target_queue)
                queue_items = _redis_list(backend.client, queue_key)
                pending_items = _redis_set_members(backend.client, pending_key)
                transport_queues[target_queue] = {
                    "redis_queue_len": len(queue_items),
                    "redis_queue_items": queue_items,
                    "redis_pending_count": len(pending_items),
                    "redis_pending_items": pending_items,
                }
            dead_lettered_jobs = _redis_set_members(backend.client, backend._dead_lettered_jobs_key())
            result["transport"] = {
                "queues": transport_queues,
                "inflight": inflight_payloads,
                "dead_lettered_jobs": dead_lettered_jobs,
            }
        return result
    finally:
        session.close()


@queue_app.command("inspect")
def queue_inspect() -> None:
    """Inspect queue transport and delivery state."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        cmd = [
            "docker", "compose",
            "-f", cfg.stack.compose_file,
            "-p", cfg.stack.project_name,
            "exec", "-T", "control-plane",
            "python", "-c",
            (
                "import json; "
                "from skyops._queue import _inspect_queue_state; "
                "print(json.dumps(_inspect_queue_state(), indent=2, sort_keys=True, default=str))"
            ),
        ]
        subprocess.run(cmd, check=True, timeout=30)
        return
    _emit(_inspect_queue_state())


@queue_app.command("reconstruct")
def queue_reconstruct() -> None:
    """Rebuild queue from current DB state."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import json; "
            "from agp._ops_helpers import reconstruct_queue_from_state; "
            "print(json.dumps(reconstruct_queue_from_state(), indent=2, sort_keys=True, default=str))"
        )
        return
    from agp._ops_helpers import reconstruct_queue_from_state

    result = reconstruct_queue_from_state()
    _emit(result)


@queue_app.command("redrive")
def queue_redrive(
    visibility_timeout: int = typer.Option(30, "--visibility-timeout", help="Visibility timeout seconds."),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Max delivery attempts."),
) -> None:
    """Redrive stale in-flight deliveries."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import json, os; "
            "from agp.config import settings; "
            "from agp.db import SessionLocal; "
            "from agp.queue_backend import get_queue_backend; "
            "backend=get_queue_backend(settings.queue_backend); "
            "session=SessionLocal(); "
            "result=backend.redrive_stale_deliveries(session, visibility_timeout_seconds=int(os.environ['_VIS_TIMEOUT']), max_delivery_attempts=int(os.environ['_MAX_ATTEMPTS'])); "
            "session.commit(); "
            "session.close(); "
            "print(json.dumps(result, indent=2, sort_keys=True, default=str))",
            env={"_VIS_TIMEOUT": str(visibility_timeout), "_MAX_ATTEMPTS": str(max_attempts)},
        )
        return
    from agp.config import settings
    from agp.db import SessionLocal
    from agp.queue_backend import get_queue_backend

    backend = get_queue_backend(settings.queue_backend)
    session = SessionLocal()
    try:
        result = backend.redrive_stale_deliveries(
            session,
            visibility_timeout_seconds=visibility_timeout,
            max_delivery_attempts=max_attempts,
        )
        session.commit()
        _emit(result)
    finally:
        session.close()


@job_app.command("block")
def job_block(
    job_id: str = typer.Argument(help="Job ID to block."),
    reason: str = typer.Option("operator-block", "--reason", "-r", help="Block reason."),
) -> None:
    """Block a queued job."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import os; "
            "from agp.control_plane import _block_job, _require_job; "
            "from agp.db import SessionLocal; "
            "session=SessionLocal(); "
            "job=_require_job(session, job_id=os.environ['_JOB_ID']); "
            "_block_job(session, job=job, reason=os.environ['_REASON']); "
            "session.commit(); "
            "session.close(); "
            "print(f'Job {os.environ[\"_JOB_ID\"]} blocked.')",
            env={"_JOB_ID": job_id, "_REASON": reason},
        )
        return
    from agp.control_plane import _block_job, _require_job
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        job = _require_job(session, job_id=job_id)
        _block_job(session, job=job, reason=reason)
        session.commit()
        typer.echo(f"Job {job_id} blocked.")
    finally:
        session.close()


@job_app.command("unblock")
def job_unblock(
    job_id: str = typer.Argument(help="Job ID to unblock."),
    reason: str = typer.Option("operator-unblock", "--reason", "-r", help="Unblock reason."),
) -> None:
    """Unblock a blocked job."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import os; "
            "from agp.control_plane import _unblock_job, _require_job; "
            "from agp.db import SessionLocal; "
            "session=SessionLocal(); "
            "job=_require_job(session, job_id=os.environ['_JOB_ID']); "
            "_unblock_job(session, job=job, reason=os.environ['_REASON']); "
            "session.commit(); "
            "session.close(); "
            "print(f'Job {os.environ[\"_JOB_ID\"]} unblocked.')",
            env={"_JOB_ID": job_id, "_REASON": reason},
        )
        return
    from agp.control_plane import _unblock_job, _require_job
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        job = _require_job(session, job_id=job_id)
        _unblock_job(session, job=job, reason=reason)
        session.commit()
        typer.echo(f"Job {job_id} unblocked.")
    finally:
        session.close()


@sweep_app.command("leases")
def sweep_leases() -> None:
    """One-shot sweep of expired leases."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import json; "
            "from agp.control_plane import sweep_expired_leases; "
            "from agp.db import SessionLocal; "
            "session=SessionLocal(); "
            "result=sweep_expired_leases(session); "
            "session.close(); "
            "print(json.dumps(result, indent=2, sort_keys=True, default=str))",
        )
        return
    from agp.control_plane import sweep_expired_leases
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        result = sweep_expired_leases(session)
        _emit(result)
    finally:
        session.close()


@sweep_app.command("leases-loop")
def sweep_leases_loop(
    interval_seconds: float = typer.Option(1.0, "--interval-seconds", help="Sweep interval in seconds."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="Stop after N iterations."),
) -> None:
    """Continuously sweep expired leases."""
    from agp.cli import sweep_loop as agp_sweep_loop

    agp_sweep_loop(interval_seconds=interval_seconds, max_iterations=max_iterations)


@sweep_app.command("runtimes")
def sweep_runtimes(
    stale_timeout: int = typer.Option(90, "--timeout", help="Stale timeout in seconds."),
) -> None:
    """One-shot sweep of stale runtimes."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_exec_python(
            cfg,
            "import json, os; "
            "from agp.control_plane import sweep_stale_runtimes; "
            "from agp.db import SessionLocal; "
            "session=SessionLocal(); "
            "result=sweep_stale_runtimes(session, stale_timeout_seconds=int(os.environ['_STALE_TIMEOUT'])); "
            "session.close(); "
            "print(json.dumps(result, indent=2, sort_keys=True, default=str))",
            env={"_STALE_TIMEOUT": str(stale_timeout)},
        )
        return
    from agp.control_plane import sweep_stale_runtimes
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        result = sweep_stale_runtimes(session, stale_timeout_seconds=stale_timeout)
        _emit(result)
    finally:
        session.close()


@sweep_app.command("runtimes-loop")
def sweep_runtimes_loop(
    interval_seconds: float = typer.Option(1.0, "--interval-seconds", help="Sweep interval in seconds."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="Stop after N iterations."),
    stale_timeout: int | None = typer.Option(None, "--timeout", help="Stale timeout in seconds."),
) -> None:
    """Continuously sweep stale runtimes."""
    from agp.cli import sweep_runtimes_loop as agp_sweep_runtimes_loop

    agp_sweep_runtimes_loop(
        interval_seconds=interval_seconds,
        max_iterations=max_iterations,
        stale_timeout_seconds=stale_timeout,
    )


@sweep_app.command("idle")
def sweep_idle(
    timeout: int | None = typer.Option(None, "--timeout", help="Heartbeat grace period in seconds (uses config default if omitted)."),
) -> None:
    """One-shot sweep of stale agents (delete dead agents, drain empty draining agents)."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        env = {}
        if timeout is not None:
            env["_HEARTBEAT_GRACE"] = str(timeout)
        _docker_exec_python(
            cfg,
            "import json, os; "
            "from agp.control_plane import sweep_stale_agents; "
            "from agp.db import SessionLocal; "
            "session=SessionLocal(); "
            "kwargs={}; "
            "g=os.environ.get('_HEARTBEAT_GRACE'); "
            "kwargs.update({'heartbeat_grace_seconds': int(g)} if g else {}); "
            "result=sweep_stale_agents(session, **kwargs); "
            "session.close(); "
            "print(json.dumps(result, indent=2, sort_keys=True, default=str))",
            env=env or None,
        )
        return
    from agp.control_plane import sweep_stale_agents
    from agp.db import SessionLocal

    session = SessionLocal()
    try:
        kwargs = {}
        if timeout is not None:
            kwargs["heartbeat_grace_seconds"] = timeout
        result = sweep_stale_agents(session, **kwargs)
        _emit(result)
    finally:
        session.close()
