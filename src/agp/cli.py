"""CLI entrypoint for the AGP scaffold.

Primarily exposes the agent-facing client surface (send, wait, status,
ls, info, nudge, etc.) that talks to a running control plane over HTTP.
Operational commands still exist here as hidden compatibility shims so
older scripts keep working, but the intended operator entrypoint is the
``skyops`` CLI.

All server-side imports are deferred to command bodies so that
``pip install agp`` (without ``[server]``) can still import
``agp.client`` without pulling in uvicorn/sqlalchemy/pydantic-settings.
"""

import json
import logging
import os
import re as _re
import sys
import time
import uuid
from pathlib import Path

import typer

app = typer.Typer(help="AGP agent CLI.")


def _require_server_extra() -> None:
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
    except ImportError:
        typer.echo(
            "This command requires server dependencies.\n"
            "Install with: pip install 'agp[server]'",
            err=True,
        )
        raise typer.Exit(1)


def _connectable_host(host: str) -> str:
    """Replace 0.0.0.0 with 127.0.0.1 for client connections."""
    return "127.0.0.1" if host == "0.0.0.0" else host


def _default_server_url() -> str:
    """Derive server URL from AGP_HOST/AGP_PORT env or settings."""
    host = os.environ.get("AGP_HOST") or "127.0.0.1"
    port = os.environ.get("AGP_PORT") or "7860"
    return f"http://{_connectable_host(host)}:{port}"


def _format_http_error(exc) -> str:
    """Extract a clean error message from an httpx.HTTPStatusError.

    The CP returns ``{"ok": false, "error": {"code": ..., "message": ...}}``.
    """
    try:
        body = exc.response.json()
        err = body.get("error", {})
        message = err.get("message") or err.get("code") or str(body)
    except Exception:
        message = exc.response.text or str(exc)
    return f"[HTTP {exc.response.status_code}] {message}"


def _heartbeat_age_seconds(iso_timestamp: str | None) -> float | None:
    """Compute seconds since an ISO-8601 heartbeat timestamp, or None."""
    if not iso_timestamp:
        return None
    from datetime import datetime, timezone
    try:
        hb_dt = datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - hb_dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _cli_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _repair_json_string(text: str) -> str:
    """Best-effort repair for unescaped quotes inside JSON strings."""
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    repaired = text
    for _ in range(32):
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            pass

        in_string = False
        escape = False
        fixed = False
        for idx, char in enumerate(repaired):
            if not in_string:
                if char == '"':
                    in_string = True
                continue
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char != '"':
                continue

            next_non_ws = idx + 1
            while next_non_ws < len(repaired) and repaired[next_non_ws] in " \t\r\n":
                next_non_ws += 1
            if next_non_ws >= len(repaired) or repaired[next_non_ws] in ",}]:":
                in_string = False
                continue

            repaired = repaired[:idx] + '\\"' + repaired[idx + 1 :]
            fixed = True
            break
        if not fixed:
            break
    # Only return repaired text if it's actually valid JSON now.
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        return text  # return original — don't return half-repaired garbage


def _strip_tui_action_traces(text: str) -> str:
    """Extract the final summary from a Codex TUI result artifact.

    The Codex TUI can emit streamed progress, shell traces, and footer notices
    before the delivered summary. Use trace lines and horizontal rules as block
    boundaries, then keep only the final non-noise block.
    """
    lines = text.splitlines()
    trace_prefixes = (
        "Explored", "└ ", "Read ", "Search ", "Edited ", "Working (",
        "Waited for", "Waiting for", "FAILED ", "ERROR ", "› ",
    )
    pytest_summary = _re.compile(r"^\d+\s+(?:failed|passed|error|errors)(?:,|\s|$)")
    background_notice = _re.compile(r"^\d+\s+background terminal running\b")
    pid_text = _re.compile(r"\bPID\s+\d+\b|\(pid\s+\d+\)")
    command_trace = _re.compile(r"^Ran (?:(?:python|pytest|pyright|mypy|ruff|git|rg|sed|cat|ls|find|bash|sh|zsh)\b|`)")
    pytest_noise = (
        "platform ",
        "cachedir:",
        "rootdir:",
        "configfile:",
        "plugins:",
        "asyncio:",
        "collected ",
        "=============================",
    )

    blocks: list[str] = []
    current: list[str] = []

    def flush_block() -> None:
        nonlocal current
        if current:
            blocks.append("\n".join(current).strip())
            current = []

    for line in lines:
        s = line.strip()
        if not s:
            flush_block()
            continue
        if ((s and all(ch in "─━═—-" for ch in s)) or (s.startswith("─") and len(s) > 20)):
            flush_block()
            continue
        if any(s.startswith(prefix) for prefix in trace_prefixes):
            flush_block()
            continue
        if command_trace.match(s):
            flush_block()
            continue
        if any(s.startswith(prefix) for prefix in pytest_noise):
            flush_block()
            continue
        if background_notice.match(s):
            flush_block()
            continue
        if pytest_summary.match(s) or s.startswith("RuntimeError:"):
            flush_block()
            continue
        current.append(pid_text.sub("pid redacted", line))

    flush_block()
    return (blocks[-1] if blocks else "").strip()


def _extract_trailing_json_payload(text: str) -> dict | None:
    def _candidate_attempts(raw: str) -> list[str]:
        attempts = [
            raw,
            "".join(line.strip() for line in raw.splitlines()),
            " ".join(line.strip() for line in raw.splitlines()),
        ]
        stripped = raw.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            fence_end = stripped.find("\n")
            if fence_end != -1:
                fenced_body = stripped[fence_end + 1 : -3].strip()
                attempts.extend(
                    [
                        fenced_body,
                        "".join(line.strip() for line in fenced_body.splitlines()),
                        " ".join(line.strip() for line in fenced_body.splitlines()),
                    ]
                )
        return attempts

    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    for idx in range(len(stripped) - 1, -1, -1):
        if stripped[idx] not in "[{":
            continue
        suffix = stripped[idx:]
        fence_start = stripped.rfind("```", 0, idx)
        if fence_start != -1 and stripped.find("\n", fence_start, idx) != -1:
            suffix = stripped[fence_start:]
        for attempt in _candidate_attempts(suffix):
            for text_to_parse in (attempt, _repair_json_string(attempt)):
                try:
                    payload, end = decoder.raw_decode(text_to_parse)
                except json.JSONDecodeError:
                    continue
                if text_to_parse[end:].strip():
                    continue
                if isinstance(payload, dict):
                    return payload
    return None


def _review_attachment_note(*, attachment_name: str, short_output_guidance: str) -> str:
    return (
        f"Source job result is attached as {attachment_name}. "
        f"AGP should also materialize that attachment under .agp-tmp/attachments/ in the workspace before execution; "
        f"search by the attached filename if needed. "
        f"{short_output_guidance}"
    )


def _review_fix_attachment_note(*, attachment_name: str, short_output_guidance: str) -> str:
    return (
        f"Updated result is attached as {attachment_name}. "
        f"AGP should also materialize that attachment under .agp-tmp/attachments/ in the workspace before execution; "
        f"search by the attached filename if needed. "
        f"{short_output_guidance}"
    )


def _build_review_state(
    *,
    review_session_id: str,
    source_job_id: str,
    reviewer_id: str,
    dev_id: str,
    max_rounds: int,
    current_round: int,
    phase: str,
    conversation_id: str,
    active_job_id: str | None = None,
    last_verdict: str | None = None,
    review_attempt_id: str | None = None,
) -> dict:
    from datetime import datetime, timezone

    return {
        "review_session_id": review_session_id,
        "source_job_id": source_job_id,
        "reviewer_id": reviewer_id,
        "dev_id": dev_id,
        "max_rounds": max_rounds,
        "current_round": current_round,
        "phase": phase,
        "conversation_id": conversation_id,
        "active_job_id": active_job_id,
        "last_verdict": last_verdict,
        "review_attempt_id": review_attempt_id or review_session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_review_state(client, state: dict) -> None:
    import json as _json

    client.upload_artifact(
        namespace=state["conversation_id"],
        job_id=state["source_job_id"],
        name="review-state.json",
        content=_json.dumps(state),
        role="review-state",
        content_type="application/json",
        register=True,
    )


def _load_review_state(client, source_job_id: str) -> dict | None:
    import json as _json

    data = client.list_job_artifacts(source_job_id, role="review-state")
    items = data.get("items", [])
    if not items:
        return None
    latest = items[-1]
    artifact_id = latest["artifact_id"]
    content_data = client.fetch_artifact(artifact_id, content=True)
    return _json.loads(content_data["content"])


def _parse_reviewer_verdict(client, review_job: dict) -> tuple[str, str, str]:
    """Extract verdict from a completed reviewer job.

    Returns ``(verdict, summary, review_payload_text)``.
    """
    import json as _json

    review_artifact_id = review_job.get("result_artifact_id")
    if not review_artifact_id:
        typer.echo("[warn] Reviewer job produced no result artifact — treating as changes_requested", err=True)
        return "changes_requested", "", '{"findings":[],"summary":"","verdict":"changes_requested"}'

    review_artifact = client.fetch_artifact(review_artifact_id, content=True)
    content = review_artifact.get("content", "")

    verdict = "changes_requested"
    summary = ""
    structured: dict | None = None
    try:
        structured = _json.loads(content)
    except _json.JSONDecodeError:
        structured = _extract_trailing_json_payload(content)
        if structured is None:
            typer.echo("[warn] Could not parse reviewer output as JSON — treating as changes_requested", err=True)
            summary = content[:500]
        else:
            verdict = structured.get("verdict", "changes_requested")
            summary = structured.get("summary", "")
    else:
        verdict = structured.get("verdict", "changes_requested")
        summary = structured.get("summary", "")

    review_payload_text = (
        _json.dumps(structured, separators=(",", ":"), sort_keys=True)
        if isinstance(structured, dict)
        else _json.dumps(
            {"verdict": verdict, "summary": summary, "findings": []},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return verdict, summary, review_payload_text


def _capture_git_diff(*, include_untracked: bool = False) -> tuple[str | None, str | None]:
    """Best-effort capture of ``git diff HEAD`` (staged + unstaged) and stat.

    Optionally lists untracked files via
    ``git ls-files --others --exclude-standard``. Returns ``(stat, diff)``
    where either or both may be ``None`` if the command fails or we are not
    inside a git repo. Never raises.
    """
    import shutil
    import subprocess

    if not shutil.which("git"):
        return None, None
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except Exception:
        return None, None

    stat_parts: list[str] = []
    diff_parts: list[str] = []

    # Staged + unstaged changes vs HEAD
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        stat_output = result.stdout.strip()
        if stat_output:
            stat_parts.append(stat_output)
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        diff_output = result.stdout.strip()
        if diff_output:
            diff_parts.append(diff_output)
    except Exception:
        pass

    if include_untracked:
        # Deliberately opt-in: untracked files often include local notes or
        # prompts that are not part of the code under review.
        try:
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            untracked = result.stdout.strip()
            if untracked:
                stat_parts.append(f"\nUntracked files:\n{untracked}")
                diff_parts.append(f"Untracked files:\n{untracked}")
        except Exception:
            pass

    stat_final = "\n".join(stat_parts).strip() or None
    diff_final = "\n".join(diff_parts).strip() or None
    return stat_final, diff_final


@app.command(hidden=True)
def initdb() -> None:
    """Initialize or migrate the database schema."""
    _require_server_extra()

    from agp.db import init_db

    init_db()
    typer.echo("Initialized database schema.")


@app.command(name="db-status", hidden=True)
def db_status() -> None:
    """Show current schema version and pending migrations."""
    _require_server_extra()

    from agp.migrations import schema_status

    info = schema_status()
    typer.echo(f"Schema version:  {info['current_version']}")
    typer.echo(f"Engine:          {info['engine']}")
    typer.echo(f"Release version: {info['release_version']}")
    if info["pending_migrations"]:
        typer.echo(f"Pending:         {', '.join(info['pending_migrations'])}")
    else:
        typer.echo("Pending:         (none)")


@app.command(name="db-migrate", hidden=True)
def db_migrate() -> None:
    """Apply pending schema migrations."""
    _require_server_extra()

    from agp.migrations import apply_migrations

    result = apply_migrations()
    if result["applied"]:
        for tag in result["applied"]:
            typer.echo(f"  Applied: {tag}")
    else:
        typer.echo("No pending migrations.")
    typer.echo(f"Current version: {result['current_version']}")


@app.command(hidden=True)
def serve(
    host: str = typer.Option(None, help="Bind host (default: AGP_HOST or 127.0.0.1)."),
    port: int = typer.Option(None, help="Bind port (default: AGP_PORT or 7860)."),
) -> None:
    """Run the AGP control plane API server."""
    _require_server_extra()

    import uvicorn
    from agp.config import settings
    from agp.control_plane import build_app
    from agp.migrations import require_initialized_schema

    actual_host = host if host is not None else settings.host
    actual_port = port if port is not None else settings.port
    try:
        require_initialized_schema()
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    os.environ["AGP_ENFORCE_SQLITE_RUNTIME_GUARD"] = "1"
    uvicorn.run(build_app(), host=actual_host, port=actual_port)


def _runtime_debug_log(runtime_id: str, entry: dict) -> None:
    """Write a structured entry to the runtime JSONL log if debug logging is on."""
    _rtl = logging.getLogger("agp")
    if not _rtl.isEnabledFor(logging.DEBUG):
        return
    try:
        from datetime import datetime, UTC
        from agp.logs import append_jsonl_log
        from agp.config import settings
        path = settings.log_root / f"runtime-{runtime_id}.jsonl"
        payload = {"created_at": datetime.now(UTC).isoformat(), "kind": "runtime_lifecycle", **entry}
        append_jsonl_log(path, payload, rotation_bytes=settings.observability_log_rotation_bytes)
    except Exception:  # noqa: BLE001
        pass


@app.command(hidden=True)
def runtime_work_loop(
    runtime_id: str,
    server_url: str = typer.Option(None, help="CP base URL (default: AGP_HOST:AGP_PORT)."),
    hostname: str | None = None,
    agent_id: str | None = None,
    capability_id: str | None = None,
    capabilities: str | None = typer.Option(None, help="Comma-separated capability list (e.g. 'code,python')."),
    artifact_root: str = ".agp-artifacts",
    idle_sleep_seconds: float = 0.25,
    max_iterations: int | None = None,
    max_local_recoveries: int = 1,
    host_kind: str = typer.Option(None, help="Terminal host kind (default: AGP_RUNTIME_TERMINAL_HOST_KIND or inprocess)."),
    adapter_kind: str = typer.Option(None, help="Agent adapter kind (default: AGP_RUNTIME_AGENT_ADAPTER_KIND or default)."),
    log_level: str = typer.Option("WARNING", help="Python log level (DEBUG, INFO, WARNING, ERROR)."),
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""
    _require_server_extra()
    if isinstance(log_level, str):
        level = getattr(logging, log_level.upper(), logging.WARNING)
        # Scope to agp loggers only — avoid flooding stderr with httpcore/httpx transport noise
        logging.basicConfig(level=logging.WARNING, force=True)
        logging.getLogger("agp").setLevel(level)

    import socket as _socket
    from threading import Event

    from agp.config import settings
    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.plugins import build_terminal_host, build_agent_adapter
    from agp.runtime import RuntimeSupervisor

    actual_server_url = server_url if server_url is not None else _default_server_url()
    actual_host_kind = host_kind if host_kind is not None else settings.runtime_terminal_host_kind
    actual_adapter_kind = adapter_kind if adapter_kind is not None else settings.runtime_agent_adapter_kind

    actual_hostname = hostname or _socket.gethostname()
    runtime_token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN") or None
    resolved_capabilities = [
        "".join(ch for ch in c.strip() if ch.isprintable())
        for c in capabilities.split(",")
        if c.strip()
    ] if capabilities else None
    payload: list[dict] = []
    restart_attempt = 0
    max_restart_attempts = int(os.environ.get("AGP_MAX_RUNTIME_RESTARTS", "3"))
    _runtime_debug_log(runtime_id, {"action": "startup", "host_kind": actual_host_kind, "adapter_kind": actual_adapter_kind, "agent_id": agent_id})

    while True:
        stop_event = Event()
        client = RuntimeClient(
            RuntimeIdentity(
                runtime_id=runtime_id,
                hostname=actual_hostname,
                server_url=actual_server_url,
                token=runtime_token,
            )
        )
        worker = RuntimeSupervisor(
            client,
            host=build_terminal_host(actual_host_kind, workspace=settings.wezterm_workspace),
            adapter=build_agent_adapter(actual_adapter_kind),
            artifact_root=artifact_root,
        )
        import httpx as _httpx
        try:
            batch = worker.run_forever(
                agent_id=agent_id,
                capability_id=capability_id,
                capabilities=resolved_capabilities,
                idle_sleep_seconds=idle_sleep_seconds,
                max_iterations=max_iterations,
                stop_event=stop_event,
                max_local_recoveries=max_local_recoveries,
            )
            payload.extend(batch)
            _runtime_debug_log(runtime_id, {"action": "shutdown_clean", "iterations": len(payload)})
            break
        except _httpx.HTTPStatusError as exc:
            # 4xx errors are non-retryable (auth failure, bad config, etc.)
            if 400 <= exc.response.status_code < 500:
                _runtime_debug_log(runtime_id, {"action": "fatal_http_error", "status": exc.response.status_code, "error": str(exc)})
                typer.echo(
                    f"[runtime] fatal HTTP {exc.response.status_code}: {exc}; exiting",
                    err=True,
                )
                raise typer.Exit(1) from exc
            # 5xx — transient, fall through to retry
            _runtime_debug_log(runtime_id, {"action": "transient_http_error", "status": exc.response.status_code, "error": str(exc), "attempt": restart_attempt + 1})
            restart_attempt += 1
        except (ValueError, TypeError) as exc:
            # Config/setup errors — non-retryable
            _runtime_debug_log(runtime_id, {"action": "fatal_config_error", "error": f"{type(exc).__name__}: {exc}"})
            typer.echo(
                f"[runtime] fatal config error: {type(exc).__name__}: {exc}; exiting",
                err=True,
            )
            raise typer.Exit(1) from exc
        except Exception as exc:  # noqa: BLE001
            _runtime_debug_log(runtime_id, {"action": "worker_crash", "error": f"{type(exc).__name__}: {exc}", "attempt": restart_attempt + 1})
            restart_attempt += 1
        finally:
            stop_event.set()
            client.close()
        if restart_attempt > max_restart_attempts:
            _runtime_debug_log(runtime_id, {"action": "shutdown_max_restarts", "attempts": restart_attempt})
            typer.echo(
                f"[runtime] giving up after {restart_attempt} restart attempts; exiting",
                err=True,
            )
            raise typer.Exit(1)
        backoff_seconds = min(30.0, max(idle_sleep_seconds, 0.25) * (2 ** (restart_attempt - 1)))
        typer.echo(
            f"[runtime] worker error (attempt {restart_attempt}/{max_restart_attempts}); "
            f"reinitializing in {backoff_seconds:.1f}s",
            err=True,
        )
        time.sleep(backoff_seconds)
    typer.echo(json.dumps(payload))


def _runtime_binding_warning(client, agent_id: str) -> str | None:
    runtime_id = f"rtm_{agent_id}"
    try:
        getter = getattr(client, "ops_get_runtime", None) or getattr(client, "get_runtime", None)
        runtime = getter(runtime_id) if getter is not None else None
    except Exception:  # noqa: BLE001
        runtime = None
    if not runtime:
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    if str(runtime.get("hostname") or "").strip().lower() in {"", "unknown"}:
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    agents = runtime.get("agents") or []
    if agents and not any(item.get("agent_id") == agent_id for item in agents):
        return f"WARNING: No runtime bound. Start one with: make runtime AGP_RUNTIME_AGENT_ID={agent_id}"
    return None


@app.command(hidden=True)
def sweep_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
) -> None:
    """Continuously expire stale leases on a fixed interval."""
    _require_server_extra()

    from agp.control_plane import sweep_expired_leases
    from agp.db import SessionLocal
    from agp.sweeper import LeaseSweeperService

    service = LeaseSweeperService(
        session_factory=SessionLocal,
        sweep_fn=sweep_expired_leases,
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


@app.command(hidden=True)
def sweep_runtimes_loop(
    interval_seconds: float = 1.0,
    max_iterations: int | None = None,
    stale_timeout_seconds: int = typer.Option(None, help="Override AGP_RUNTIME_STALE_TIMEOUT_SECONDS."),
) -> None:
    """Continuously mark stale runtimes offline and detach or degrade bound agents."""
    _require_server_extra()

    from agp.config import settings
    from agp.control_plane import sweep_stale_runtimes
    from agp.db import SessionLocal
    from agp.sweeper import SweeperService

    actual_timeout = stale_timeout_seconds if stale_timeout_seconds is not None else settings.runtime_stale_timeout_seconds

    service = SweeperService(
        session_factory=SessionLocal,
        sweep_fn=lambda session: sweep_stale_runtimes(
            session,
            stale_timeout_seconds=actual_timeout,
        ),
        interval_seconds=interval_seconds,
    )
    for payload in service.run_forever(max_iterations=max_iterations):
        typer.echo(payload)


# ── SDK client commands (no server deps needed) ─────────────────────

_SEPARATOR = "========================================="


def _make_client(server_url: str | None = None):
    """Build an AgpClient that honours profile/env auth.

    If *server_url* is explicitly passed (e.g. via ``--server-url``), it
    overrides the URL from the profile/env, but the token is still loaded
    from the profile resolution chain (env → file → fallback).
    """
    from agp.client import AgpClient, AgpProfile

    profile = AgpProfile.load()
    if server_url:
        profile.server_url = server_url
    return AgpClient(profile=profile)


def _show_crash_breadcrumb() -> None:
    """If a crash breadcrumb file exists, display it and offer recovery hints."""
    import json
    breadcrumb_path = Path(".agp-crash")
    if not breadcrumb_path.exists():
        return
    try:
        data = json.loads(breadcrumb_path.read_text())
        ts = data.get("timestamp", "?")
        reason = data.get("reason", "unknown")
        typer.echo("", err=True)
        typer.echo("--- LAST CRASH ---", err=True)
        typer.echo(f"  When:   {ts}", err=True)
        typer.echo(f"  Reason: {reason}", err=True)
        typer.echo("  Recover: `make local-restart` (preserves DB state)", err=True)
        typer.echo("  Clean:   `make local-up` (wipes everything)", err=True)
        typer.echo("------------------", err=True)
    except Exception:
        pass


def _cli_client(server_url: str | None = None):
    """_make_client wrapper that converts transport errors to friendly messages.

    Use this in user-facing CLI commands so that connection-refused /
    DNS-failure / timeout errors produce a one-line message instead of a
    raw Python traceback.  Commands with their own retry logic (e.g. ``up``)
    should continue using ``_make_client`` directly.
    """
    from contextlib import contextmanager

    import httpx as _httpx

    @contextmanager
    def _ctx():
        try:
            with _make_client(server_url) as client:
                yield client
        except _httpx.TransportError as exc:
            typer.echo(f"connection error: control plane unreachable ({exc})", err=True)
            _show_crash_breadcrumb()
            raise typer.Exit(1)

    return _ctx()


def _parse_attachment_option(value: str) -> tuple[Path, str]:
    path_text, sep, role = value.rpartition(":")
    if not sep or not path_text or not role:
        raise typer.BadParameter("--attach must be <path>:<role>")
    path = Path(path_text)
    if not path.is_file():
        raise typer.BadParameter(f"attachment file not found: {path}")
    return path, role


def _print_banner(label: str, subtitle: str) -> None:
    typer.echo(_SEPARATOR)
    typer.echo(f"[{label}] {subtitle}")
    typer.echo(_SEPARATOR)


def _print_job_result(job: dict, client) -> None:
    """Print structured terminal output for a completed/failed job."""
    job_status = job["status"]
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)

    if job_status == "completed":
        _print_banner("COMPLETED", "Task Finished")
    elif job_status == "cancelled":
        _print_banner("CANCELLED", "Task Cancelled")
    else:
        suffix = " with Errors" if retry_count > 0 else ""
        _print_banner("FAILED", f"Task Failed{suffix}")

    status_label = {"completed": "SUCCESS", "cancelled": "CANCELLED", "failed": "FAILED"}.get(job_status, job_status.upper())
    typer.echo(f"JOB_ID:       {job['job_id']}")
    typer.echo(f"AGENT:        {job.get('target_agent_id', 'unknown')}")
    typer.echo(f"STATUS:       {status_label}")
    if retry_count > 0:
        typer.echo(f"RETRIES:      {retry_count}/{max_retries}")

    # Print result artifact content
    if job.get("result_artifact_id"):
        try:
            art = client.fetch_artifact(job["result_artifact_id"], content=True)
            typer.echo(f"RESULT:       artifact {job['result_artifact_id']}")
            typer.echo("---")
            typer.echo(art.get("content", "(no content)"))
        except Exception:
            typer.echo(f"RESULT:       artifact {job['result_artifact_id']} (fetch failed)")
    elif job_status == "failed":
        # Try failure_evidence artifact
        try:
            artifacts = client.list_job_artifacts(job["job_id"], role="failure_evidence")
            items = artifacts.get("items", [])
            if items:
                art = client.fetch_artifact(items[0]["artifact_id"], content=True)
                typer.echo("---")
                typer.echo(art.get("content", "(no content)"))
            else:
                typer.echo("(no artifact)")
        except Exception:
            typer.echo("(no artifact)")

    if job_status == "failed" and retry_count > 0:
        typer.echo("")
        typer.echo(
            f"Notice: System exhausted best-effort retries ({retry_count} attempts). "
            "Review the error log and pivot your strategy."
        )


def _peek_tip(agent_id: str) -> str | None:
    """Return a terminal-specific tip for peeking at an agent's live output.

    Detects the local terminal host by probing for tmux sessions or
    wezterm panes.  Returns None when no local session is found (remote
    runtime or non-interactive environment).
    """
    import shutil
    import subprocess

    # Try tmux first
    if shutil.which("tmux"):
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", f"agp-{agent_id}"],
                capture_output=True, timeout=3,
            )
            if result.returncode == 0:
                return (
                    f"Tip: peek at live output with:\n"
                    f"  tmux capture-pane -t agp-{agent_id} -p -S -30"
                )
        except Exception:
            pass

    # Try wezterm
    if shutil.which("wezterm"):
        try:
            result = subprocess.run(
                ["wezterm", "cli", "list", "--format", "json"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                import json as _json
                for pane in _json.loads(result.stdout):
                    title = pane.get("title", "")
                    if f"AGP:{agent_id}" in title:
                        pane_id = pane.get("pane_id")
                        return (
                            f"Tip: peek at live output with:\n"
                            f"  wezterm cli get-text --pane-id {pane_id}"
                        )
        except Exception:
            pass

    return None


def _print_peek_tip(agent_id: str) -> None:
    """Print a peek tip if one is available."""
    tip = _peek_tip(agent_id)
    if tip:
        typer.echo(tip)


def _print_detached(job_id: str, agent_id: str) -> None:
    _print_banner("ACCEPTED", "Task Detached (Running Long)")
    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {agent_id}")
    typer.echo(f"STATUS:       IN_PROGRESS")
    typer.echo("")
    typer.echo("Notice: The CLI has detached to free your terminal.")
    typer.echo(f"- To check status manually:  agp status {job_id}")
    typer.echo(f"- To wait synchronously:     agp wait {job_id}")
    _print_peek_tip(agent_id)


def _poll_until_done(client, job_id: str, timeout: float, heartbeat_interval: float = 10.0):
    """Poll job until terminal or timeout.  Returns (job_dict, timed_out)."""
    import time
    from datetime import datetime, timezone

    start = time.monotonic()
    deadline = start + timeout
    last_heartbeat = start

    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return job, False

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start)
            hint = ""
            try:
                events_data = client.get_job_events(job_id, limit=200)
                items = events_data.get("items") or []
                progress_ev = None
                for ev in reversed(items):
                    body = ev.get("body") or {}
                    if body.get("message") == "runtime.progress_heartbeat":
                        progress_ev = ev
                        break
                if progress_ev:
                    details = (progress_ev.get("body") or {}).get("details") or {}
                    last_line = (details.get("last_line") or "").strip()
                    output_chars = details.get("output_chars")
                    if last_line:
                        hint = f" \u2014 {last_line[:60]}"
                    elif output_chars:
                        hint = f" \u2014 {output_chars:,} chars output"
                    created_at = progress_ev.get("created_at", "")
                    if created_at:
                        ev_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - ev_time).total_seconds() > 30:
                            hint += " (stalled)"
            except Exception:
                pass
            typer.echo(f"[..] Agent working... ({elapsed}s elapsed){hint}")
            last_heartbeat = now

        time.sleep(2)

    return client.get_job(job_id), True  # last check before giving up


# ── 0a. up ──────────────────────────────────────────────────────────


def _poll_agent_ready(
    client, agent_id: str, *, timeout: float = 120.0, heartbeat_interval: float = 5.0
) -> dict:
    """Poll until agent reaches idle status.  Returns agent dict.

    Currently the server transitions agents to IDLE synchronously inside
    the ``POST /agents/up`` response, so the first poll always succeeds.
    The loop exists for forward-compatibility with async provisioning
    (e.g. waiting for a runtime to bind).
    """
    import time

    import httpx as _httpx

    start = time.monotonic()
    deadline = start + timeout
    last_heartbeat = start

    while time.monotonic() < deadline:
        try:
            agent = client.get_agent(agent_id)
        except _httpx.HTTPStatusError as exc:
            # Non-retryable HTTP errors — bail immediately
            if exc.response.status_code in (401, 403, 404):
                raise
            # 5xx or other — keep polling
        except _httpx.TransportError:
            # Network-level failures (timeout, DNS, connection reset) — keep polling
            pass
        else:
            status = agent.get("status")
            if status == "idle":
                return agent
            # Terminal statuses will never become idle — exit early
            if status in ("error", "failed"):
                return agent

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start)
            typer.echo(f"[..] Waiting for agent registration... ({elapsed}s elapsed)")
            last_heartbeat = now

        time.sleep(1)

    # Final check — guarded so a down server doesn't produce a raw traceback
    try:
        return client.get_agent(agent_id)
    except (_httpx.HTTPStatusError, _httpx.TransportError):
        return {"status": "unknown"}


@app.command()
def up(
    capability_name: str = typer.Argument(..., help="Capability name (must match agp ls output)."),
    server_url: str = typer.Option(None, help="CP URL."),
    agent_id: str = typer.Option(None, "--agent-id", help="Explicit agent ID (default: auto-generated)."),
    workspace_ref: str = typer.Option(None, "--workspace", help="Working directory for the agent."),
    timeout: int = typer.Option(120, help="Max seconds to wait for agent to become idle."),
    max_retries: int = typer.Option(3, help="Provisioning retry attempts on server error."),
) -> None:
    """Provision an agent from a capability. Blocks until the agent is IDLE.

    Resolves the capability by display name (as shown in agp ls), creates an
    agent, and waits for it to become ready.
    """
    import time

    import httpx as _httpx

    with _make_client(server_url) as client:
        # Self-registration model: pass capability name directly as a
        # capability string.  No need to resolve against /capabilities table.
        typer.echo(f"[..] Provisioning capability '{capability_name}'...")

        # Retry loop for provisioning
        data: dict | None = None
        for attempt in range(1, max_retries + 1):
            typer.echo(f"[..] Registering agent... (Attempt {attempt}/{max_retries})")
            try:
                data = client.register_agent(
                    agent_id=agent_id,
                    capabilities=[capability_name],
                    workspace_ref=workspace_ref,
                )
                break
            except _httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 409:
                    detail = agent_id or "(auto-generated)"
                    _print_banner("ERROR", "Provisioning Failed")
                    typer.echo(f"FATAL: Agent already exists: {detail}")
                    raise typer.Exit(1)
                if status >= 500 and attempt < max_retries:
                    typer.echo(f"[..] Server error. Retrying... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(2)
                    continue
                _print_banner("ERROR", "Provisioning Failed")
                typer.echo(f"FATAL: Could not bring up '{capability_name}' after {attempt} attempts.")
                typer.echo(f"REASON: {exc}")
                raise typer.Exit(1)
            except _httpx.TransportError:
                if attempt < max_retries:
                    typer.echo(f"[..] Network error. Retrying... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(2)
                    continue
                _print_banner("ERROR", "Provisioning Failed")
                typer.echo(f"FATAL: Could not reach server after {max_retries} attempts.")
                raise typer.Exit(1)

        if data is None:
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Could not bring up '{capability_name}' after {max_retries} attempts.")
            typer.echo("REASON: Infrastructure unavailable or insufficient resources.")
            typer.echo("ACTION: Pivot your strategy or try a different capability.")
            raise typer.Exit(1)

        resolved_agent_id = data["agent_id"]

        # Print agent ID early so the user can recover if polling fails
        typer.echo(f"[..] Agent {resolved_agent_id} created. Waiting for IDLE...")

        # Poll until idle
        agent = _poll_agent_ready(client, resolved_agent_id, timeout=timeout)

        if agent.get("status") != "idle":
            _print_banner("ERROR", "Provisioning Failed")
            typer.echo(f"FATAL: Agent {resolved_agent_id} did not reach IDLE within {timeout}s.")
            typer.echo(f"STATUS: {agent.get('status', '?').upper()}")
            typer.echo("ACTION: Check runtime logs or try again.")
            raise typer.Exit(1)

        _print_banner("SUCCESS", "Agent Provisioned Successfully")
        typer.echo(f"CAPABILITY: {capability_name}")
        typer.echo(f"AGENT_ID:   {resolved_agent_id}")
        typer.echo(f"STATUS:     {agent.get('status', 'idle').upper()}")
        typer.echo(f"CWD:        {agent.get('workspace_ref') or '-'}")
        warning = _runtime_binding_warning(client, resolved_agent_id)
        if warning:
            typer.echo(warning)
        typer.echo("-----------------------------------------")
        typer.echo("Ready. You may now route tasks using:")
        typer.echo(f"  agp send {resolved_agent_id} \"your prompt here\"")


# ── 0b. down ────────────────────────────────────────────────────────


@app.command()
def down(
    agent_id: str = typer.Argument(..., help="Agent ID to tear down."),
    server_url: str = typer.Option(None, help="CP URL."),
    force: bool = typer.Option(False, "--force", help="Force teardown even if agent is busy (cancels active jobs)."),
) -> None:
    """Tear down an agent and release its resources.

    If the agent is busy, use --force to cancel active jobs and destroy it.
    Without --force, busy agents will be rejected — use --force explicitly.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        typer.echo(f"[..] Locating agent {agent_id}...")

        try:
            agent = client.get_agent(agent_id)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                _print_banner("ERROR", "Teardown Failed")
                typer.echo(f"FATAL: Agent not found: {agent_id}")
                raise typer.Exit(1)
            raise

        agent_status = agent.get("status", "unknown")

        # Statuses that may have active work — require --force
        _HAS_ACTIVE_WORK = ("busy", "draining")

        if agent_status in _HAS_ACTIVE_WORK and not force:
            _print_banner("ERROR", "Teardown Blocked")
            typer.echo(f"Agent {agent_id} is {agent_status.upper()} (may have active work).")
            typer.echo("Use --force to cancel active jobs and destroy it:")
            typer.echo(f"  agp down {agent_id} --force")
            raise typer.Exit(1)

        # Determine mode — force for active-work agents, drain for idle
        if agent_status in _HAS_ACTIVE_WORK:
            typer.echo(f"[..] WARNING: Agent is {agent_status.upper()}.")
            typer.echo("[..] Aborting active jobs and clearing queue...")
            mode = "force"
        else:
            typer.echo(f"[..] Agent is {agent_status.upper()}. Proceeding with teardown...")
            mode = "force"

        try:
            result = client.agent_down(agent_id, mode=mode)
        except _httpx.HTTPStatusError as exc:
            # 409 from TOCTOU guard — agent changed state between our check and the call
            if exc.response.status_code == 409:
                try:
                    detail = exc.response.json().get("error", {}).get("message", "")
                except Exception:
                    detail = ""
                if "force" in detail:
                    _print_banner("ERROR", "Teardown Blocked")
                    typer.echo(f"Agent {agent_id} has active work.")
                    typer.echo("Use --force to cancel active jobs and destroy it:")
                    typer.echo(f"  agp down {agent_id} --force")
                else:
                    _print_banner("ERROR", "Teardown Failed")
                    typer.echo(f"FATAL: {detail or exc}")
                raise typer.Exit(1)
            _print_banner("ERROR", "Teardown Failed")
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)

        result_status = result.get("status", "deleted").upper()
        if mode == "force":
            _print_banner("SUCCESS", "Agent Forcefully Destroyed")
        else:
            _print_banner("SUCCESS", "Agent Destroyed")

        typer.echo(f"AGENT_ID:   {agent_id}")
        typer.echo(f"STATUS:     {result_status}")


# ── 0c. interrupt ────────────────────────────────────────────────────


@app.command()
def interrupt(
    target: str = typer.Argument(..., help="Agent ID or Job ID to interrupt."),
    server_url: str = typer.Option(None, help="CP URL."),
    purge: bool = typer.Option(False, "--purge", help="Also cancel all queued jobs for the agent."),
) -> None:
    """Halt active execution on an agent or cancel a specific job.

    TARGET can be an Agent ID (interrupts its active job) or a Job ID
    (cancels that specific job).  Use --purge with an agent target to
    also empty the agent's pending queue.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Detect target type: try agent first, fall back to job
        is_agent = True
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                is_agent = False
            else:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if is_agent:
            _interrupt_agent(client, target, purge=purge)
        else:
            if purge:
                typer.echo("Warning: --purge is ignored when targeting a job.", err=True)
            _interrupt_job(client, target)


def _interrupt_agent(client, agent_id: str, *, purge: bool) -> None:
    import httpx as _httpx

    typer.echo(f"[..] Locating agent {agent_id}...")

    try:
        result = client.agent_interrupt(agent_id, purge=purge)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)
        if exc.response.status_code == 404:
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: Agent not found: {agent_id}")
            raise typer.Exit(1)
        typer.echo(_format_http_error(exc), err=True)
        raise typer.Exit(1)

    halted = result.get("halted_job_id")
    dropped = result.get("dropped_job_ids", [])
    remaining = result.get("remaining_queue_size", 0)
    new_status = result.get("status", "idle").upper()

    if halted:
        typer.echo(f"[..] Requesting interrupt for active execution ({halted})...")

    if purge and dropped:
        typer.echo(f"[..] Purging {len(dropped)} pending jobs from the queue...")

    if purge and halted:
        _print_banner("SUCCESS", "Interrupt Requested and Queue Purged")
    elif purge:
        _print_banner("SUCCESS", "Agent Purged and Reset")
    else:
        _print_banner("SUCCESS", "Execution Interrupted")

    typer.echo(f"AGENT:        {agent_id}")
    if halted:
        typer.echo(f"HALTED JOB:   {halted} (interrupt requested)")
    else:
        typer.echo("HALTED JOB:   (none — no active execution)")

    if purge and dropped:
        typer.echo(f"DROPPED JOBS: {', '.join(dropped)}")

    typer.echo(f"NEW STATUS:   {new_status} ({remaining} jobs in queue)")
    if remaining > 0 and not purge:
        typer.echo("")
        typer.echo("Next queued job will be claimed on the runtime's next poll cycle.")
    elif purge and not halted:
        typer.echo("")
        typer.echo("Agent is completely reset and ready for immediate, fresh tasking.")
    elif purge and halted:
        typer.echo("")
        typer.echo("Queued backlog was purged. The active run will stop once the runtime processes the interrupt.")


def _interrupt_job(client, job_id: str) -> None:
    import httpx as _httpx

    typer.echo(f"[..] Locating job {job_id}...")

    try:
        result = client.interrupt(job_id)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: Not found: {job_id}")
            typer.echo("Neither an agent nor a job was found with this ID.")
            raise typer.Exit(1)
        if exc.response.status_code == 409:
            try:
                detail = exc.response.json().get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            _print_banner("ERROR", "Interrupt Failed")
            typer.echo(f"FATAL: {detail}")
            raise typer.Exit(1)
        typer.echo(_format_http_error(exc), err=True)
        raise typer.Exit(1)

    job_status = result.get("status", "cancelled")

    if job_status == "cancelled":
        _print_banner("SUCCESS", "Job Removed from Queue")
        typer.echo(f"JOB_ID:       {job_id}")
        typer.echo(f"STATUS:       CANCELLED")
        typer.echo("")
        typer.echo(
            "Notice: This job was in the queue and had not yet started execution."
            " The active job was not affected."
        )
    else:
        _print_banner("SUCCESS", "Job Interrupt Requested")
        typer.echo(f"JOB_ID:       {job_id}")
        typer.echo(f"STATUS:       {job_status.upper()}")
        typer.echo("")
        typer.echo(
            "Notice: The job is currently running. An interrupt signal has been sent."
            " The runtime will cancel execution at the next checkpoint."
        )


# ── 1. send ──────────────────────────────────────────────────────────


@app.command(context_settings={"allow_extra_args": True, "allow_interspersed_args": True})
def send(
    ctx: typer.Context,
    agent_id: str = typer.Argument(..., help="Target agent ID."),
    task: str | None = typer.Argument(None, help="Task text to send (reads from stdin when omitted)."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    detach: bool = typer.Option(False, "--detach", help="Fire and forget — skip the sync window."),
    timeout: int = typer.Option(90, "--poll-timeout", "--timeout", help="How long the CLI waits before auto-detaching (seconds). The agent keeps running after detach."),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds", help="Per-job execution timeout hint in seconds."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (for detached tasks)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
    reply_to: str | None = typer.Option(None, "--reply-to", help="Parent message ID for a multi-turn reply."),
    attach: list[str] = typer.Option(None, "--attach", help="Attach a text file as <path>:<role>. Repeatable."),
) -> None:
    """Send a task to an agent with smart detach.

    Default: waits up to 90s for completion, then auto-detaches.
    Use --detach for fire-and-forget.  Use --poll-timeout to adjust the sync window.
    Use --nudge <orc_id> to get a push notification when the task finishes.
    Task text can be passed as unquoted words after the agent ID.
    """
    # Absorb extra positional tokens into task (unquoted multi-word support)
    if ctx.args:
        parts = [task] if task else []
        parts.extend(ctx.args)
        task = " ".join(parts)
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    conversation_id: str | None = None
    attachments: list[dict[str, str]] = []
    if output_contract is not None:
        try:
            parsed_output_contract = json.loads(output_contract)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"invalid JSON for --output-contract: {exc.msg}") from exc
        if not isinstance(parsed_output_contract, dict):
            raise typer.BadParameter("--output-contract must decode to a JSON object")
    for item in attach or []:
        path, role = _parse_attachment_option(item)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise typer.BadParameter(f"attachment not found: {path}") from None
        except (PermissionError, OSError) as exc:
            raise typer.BadParameter(f"cannot read attachment {path}: {exc}") from None
        except UnicodeDecodeError:
            raise typer.BadParameter(f"attachment is not valid UTF-8: {path}") from None
        attachments.append({"name": path.name, "role": role, "content": content})
    if task is None:
        if sys.stdin.isatty():
            typer.echo("[..] Reading task from stdin (Ctrl-D to end, Ctrl-C to cancel)...")
        task = sys.stdin.read().strip()
    if not task:
        raise typer.BadParameter("task is required (pass as argument or pipe via stdin)")

    import httpx as _httpx

    with _cli_client(server_url) as client:
        typer.echo(f"[..] Dispatching to {agent_id}...")
        try:
            result = client.send(
                "agent", agent_id, task,
                metadata=metadata,
                output_contract=parsed_output_contract,
                conversation_id=conversation_id,
                reply_to_message_id=reply_to,
                timeout_seconds=timeout_seconds,
                attachments=attachments,
                idempotency_key=_cli_idempotency_key("cli"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        job_id = result["job_id"]

        # Fire-and-forget
        if detach:
            _print_detached(job_id, agent_id)
            return

        # Smart detach: sync window with heartbeat
        _print_peek_tip(agent_id)
        try:
            job, timed_out = _poll_until_done(client, job_id, timeout)
        except KeyboardInterrupt:
            _print_detached(job_id, agent_id)
            raise typer.Exit(0)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        # Auto-detach — job still running
        _print_detached(job_id, agent_id)


@app.command(context_settings={"allow_extra_args": True, "allow_interspersed_args": True})
def reply(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Source job ID to reply to."),
    task: str | None = typer.Argument(None, help="Reply text to send; if omitted, read from stdin."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    detach: bool = typer.Option(False, "--detach", help="Fire and forget — skip the sync window."),
    timeout: int = typer.Option(90, help="Sync window in seconds before auto-detach (default: 90)."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (for detached tasks)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
) -> None:
    """Reply to an existing job, preserving its conversation context.

    Reply text can be passed as unquoted words after the job ID.
    """
    # Absorb extra positional tokens into task (unquoted multi-word support)
    if ctx.args:
        parts = [task] if task else []
        parts.extend(ctx.args)
        task = " ".join(parts)
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    if output_contract is not None:
        try:
            parsed_output_contract = json.loads(output_contract)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"invalid JSON for --output-contract: {exc.msg}") from exc
        if not isinstance(parsed_output_contract, dict):
            raise typer.BadParameter("--output-contract must decode to a JSON object")
    if task is None:
        task = sys.stdin.read().strip()
    if not task:
        raise typer.BadParameter("task is required (pass as argument or pipe via stdin)")

    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            source_job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        message_id = source_job.get("message_id")
        if not message_id:
            typer.echo("source job is missing message_id", err=True)
            raise typer.Exit(1)
        conversation_id = source_job.get("conversation_id") or job_id
        agent_id = source_job.get("target_agent_id")
        if not agent_id:
            typer.echo("source job is missing target_agent_id", err=True)
            raise typer.Exit(1)

        # Fetch parent job's prompt + result artifacts to provide conversation context
        context_task = task
        prompt_text = ""
        result_text = ""
        try:
            latest_run_id = source_job.get("latest_run_id")
            if latest_run_id:
                prompt_arts = client.list_run_artifacts(latest_run_id, role="prompt").get("items", [])
                if prompt_arts:
                    p_art = client.fetch_artifact(prompt_arts[0]["artifact_id"], content=True)
                    prompt_text = p_art.get("content", "")
            result_artifact_id = source_job.get("result_artifact_id")
            if result_artifact_id:
                r_art = client.fetch_artifact(result_artifact_id, content=True)
                result_text = r_art.get("content", "")
        except Exception:
            pass  # proceed without context if artifact fetch fails
        if prompt_text or result_text:
            parts = ["Previous exchange:\n---"]
            if prompt_text:
                parts.append(f"Prompt: {prompt_text}")
            if result_text:
                parts.append(f"Response: {result_text}")
            parts.append(f"---\nFollow-up: {task}")
            context_task = "\n".join(parts)

        typer.echo(f"[..] Replying to {job_id} via {agent_id}...")
        try:
            result = client.send(
                "agent",
                agent_id,
                context_task,
                metadata=metadata,
                output_contract=parsed_output_contract,
                conversation_id=conversation_id,
                reply_to_message_id=message_id,
                idempotency_key=_cli_idempotency_key("cli-reply"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        new_job_id = result["job_id"]

        if detach:
            _print_detached(new_job_id, agent_id)
            return

        try:
            job, timed_out = _poll_until_done(client, new_job_id, timeout)
        except KeyboardInterrupt:
            _print_detached(new_job_id, agent_id)
            raise typer.Exit(0)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        _print_detached(new_job_id, agent_id)


# ── 1c. review ──────────────────────────────────────────────────────────


@app.command(name="review")
def review_cmd(
    job_id: str = typer.Argument(..., help="Source job ID whose result should be reviewed."),
    reviewer_id: str = typer.Argument(..., help="Agent ID of the reviewer."),
    max_rounds: int = typer.Option(3, "--max-rounds", help="Maximum review rounds."),
    dev_id: str = typer.Option(None, "--dev", help="Agent ID of the developer (defaults to the source job's agent)."),
    prompt: str = typer.Option(
        "Review the attached output artifact for correctness, edge cases, and security. "
        "The artifact is the primary subject of review. "
        "If a git diff is also attached, it is supplementary context only — use it to verify or clarify claims in the artifact, but do not review unrelated files in the diff. "
        "Respond with a JSON object: {\"verdict\": \"approved\" or \"changes_requested\", \"summary\": \"...\", \"findings\": [{\"severity\": \"high|medium|low\", \"description\": \"...\"}]}. "
        "Also write findings to /tmp/review-findings.md for reference.",
        "--prompt", help="Review prompt template.",
    ),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout_per_round: int = typer.Option(300, "--timeout", help="Seconds to wait per round."),
    attach_diff: bool = typer.Option(False, "--diff/--no-diff", help="Attach local git diff alongside the source artifact (best-effort, opt-in)."),
    resume: str = typer.Option(None, "--resume", help="Resume a detached review session by source job ID."),
) -> None:
    """Run an automated review loop on a job's output artifact.

    The reviewer receives the source job's result text as an attachment and
    judges it against the review prompt. If changes are requested, findings
    are sent to the dev agent for fixes, then the reviewer re-reviews.

    Use --diff to attach the local git diff as supplementary context (tracked
    changes only). The primary subject of review is always the job output
    artifact, not the diff.

    Uses conversation threading and output contracts to structure the loop.
    Terminates when the reviewer approves or max_rounds is reached.

    Use --resume <job_id> to resume a previously detached review session.
    """
    import json
    import time
    import httpx as _httpx

    def _print_review_detached(state: dict) -> None:
        phase_label = "reviewer" if state["phase"] == "poll_reviewer" else "dev"
        _print_banner("DETACHED", "Review Paused")
        typer.echo(
            f"[DETACHED] Review paused at round {state['current_round']}/{state['max_rounds']} "
            f"(waiting for {phase_label})"
        )
        typer.echo(f"  Resume:  agp review --resume {state['source_job_id']}")
        if state.get("active_job_id"):
            agent_label = state["reviewer_id"] if phase_label == "reviewer" else state["dev_id"]
            typer.echo(f"  {phase_label.title()} job: agp wait {state['active_job_id']}")
            _print_peek_tip(agent_label)

    def _send_to_dev(client, *, dev_agent, round_num, review_payload_text, conversation_id, job_id, review_attempt_id):
        """Send findings to dev for fixing. Returns (fix_result dict)."""
        typer.echo(f"[review] Sending findings to dev {dev_agent}...")
        fix_text = (
            f"The reviewer found issues that need fixing (round {round_num}):\n\n"
            f"{review_payload_text}\n\n"
            "INSTRUCTIONS FOR YOUR RESPONSE:\n"
            "1. Make the code changes needed to address the findings.\n"
            "2. Do NOT edit any artifact files or .agp-artifacts/ content.\n"
            "3. Your final response must be ONLY a short summary:\n"
            "   - One sentence per change: what file, what you changed, why.\n"
            "   - Last line: the test command you ran (if any).\n"
            "4. Do NOT include execution traces, tool output, diff fragments, "
            "or internal narration in your response.\n"
            "5. Describe only verification you actually completed; if a run "
            "failed or did not finish, say that plainly and do not call "
            "failures existing or unrelated unless you verified that.\n"
            "6. Do NOT mention background terminals, PIDs, or other local runtime details."
        )
        fix_result = client.send(
            "agent", dev_agent, fix_text,
            conversation_id=conversation_id,
            idempotency_key=f"fix-{job_id}-r{round_num}-{review_attempt_id}",
        )
        return fix_result

    with _cli_client(server_url) as client:
        # ── Resume path ──────────────────────────────────────────
        if resume:
            state = _load_review_state(client, resume)
            if not state:
                typer.echo("[review] No review session state found for that job.", err=True)
                raise typer.Exit(1)

            typer.echo(
                f"[review] Resuming session {state['review_session_id']} "
                f"at round {state['current_round']}/{state['max_rounds']} "
                f"(phase: {state['phase']})"
            )

            # Restore saved state into local variables
            original_job_id = state["source_job_id"]
            job_id = original_job_id
            reviewer_id = state["reviewer_id"]
            dev_agent = state["dev_id"]
            max_rounds = state["max_rounds"]
            conversation_id = state["conversation_id"]
            review_session_id = state["review_session_id"]
            review_attempt_id = state.get("review_attempt_id", review_session_id)
            current_round = state["current_round"]
            active_job_id = state["active_job_id"]
            phase = state["phase"]

            try:
                source_job = client.get_job(job_id)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

            # Handle send phases — crash happened before dispatch, just re-enter loop
            if phase in ("send_to_reviewer", "send_to_dev"):
                source_job = client.get_job(job_id) if phase == "send_to_reviewer" else source_job
                start_round = current_round
                # Fall through to the shared review loop below

            else:
                # poll_reviewer or poll_dev — need to check the pending job
                try:
                    active_job = client.get_job(active_job_id)
                except _httpx.HTTPStatusError as exc:
                    typer.echo(f"[review] Could not fetch active job {active_job_id}: {_format_http_error(exc)}", err=True)
                    raise typer.Exit(1)

                active_status = active_job.get("status", "")

                # If still running, re-poll
                if active_status not in ("completed", "failed", "cancelled"):
                    typer.echo(f"[review] Active job {active_job_id} still running, re-polling...")
                    active_job, timed_out = _poll_until_done(client, active_job_id, timeout_per_round)
                    if timed_out:
                        from datetime import datetime, timezone
                        _save_review_state(client, {**state, "updated_at": datetime.now(timezone.utc).isoformat()})
                        _print_review_detached(state)
                        return
                    active_status = active_job.get("status", "")

                if active_status == "failed":
                    phase_label = "Reviewer" if phase == "poll_reviewer" else "Dev"
                    typer.echo(f"[review] {phase_label} job failed while detached.")
                    _print_job_result(active_job, client)
                    raise typer.Exit(1)

                if active_status == "cancelled":
                    typer.echo("[review] Active job was cancelled while detached.", err=True)
                    raise typer.Exit(1)

                # ── Process completed pending job ────────────────────
                if phase == "poll_reviewer":
                    verdict, summary, review_payload_text = _parse_reviewer_verdict(client, active_job)
                    typer.echo(f"[review] Verdict: {verdict}")
                    typer.echo(f"[review] Summary: {summary[:200]}")

                    if verdict == "approved":
                        _save_review_state(client, _build_review_state(
                            review_session_id=review_session_id, source_job_id=job_id,
                            reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                            current_round=current_round, phase="completed",
                            conversation_id=conversation_id, last_verdict="approved",
                            review_attempt_id=review_attempt_id,
                        ))
                        typer.echo(f"[review] Approved after {current_round} round(s).")
                        return

                    if current_round >= max_rounds:
                        typer.echo(f"[review] Max rounds ({max_rounds}) reached without approval.")
                        return

                    # Need to send to dev, poll dev, then continue to next round
                    try:
                        fix_result = _send_to_dev(
                            client, dev_agent=dev_agent, round_num=current_round,
                            review_payload_text=review_payload_text,
                            conversation_id=conversation_id, job_id=job_id,
                            review_attempt_id=review_attempt_id,
                        )
                    except _httpx.HTTPStatusError as exc:
                        typer.echo(_format_http_error(exc), err=True)
                        raise typer.Exit(1)
                    fix_job_id = fix_result["job_id"]

                    state_update = _build_review_state(
                        review_session_id=review_session_id, source_job_id=job_id,
                        reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                        current_round=current_round, phase="poll_dev",
                        conversation_id=conversation_id, active_job_id=fix_job_id,
                        last_verdict=verdict, review_attempt_id=review_attempt_id,
                    )
                    _save_review_state(client, state_update)

                    fix_job, fix_timed_out = _poll_until_done(client, fix_job_id, timeout_per_round)
                    if fix_timed_out:
                        _print_review_detached(state_update)
                        return
                    if fix_job["status"] == "failed":
                        typer.echo("[review] Dev fix job failed.")
                        _print_job_result(fix_job, client)
                        raise typer.Exit(1)

                    source_job = fix_job
                    job_id = fix_job_id
                    start_round = current_round + 1

                elif phase == "poll_dev":
                    # Dev completed while detached — continue to next round
                    source_job = active_job
                    job_id = active_job_id
                    start_round = current_round + 1

                else:
                    typer.echo(f"[review] Unknown phase '{phase}' in saved state.", err=True)
                    raise typer.Exit(1)

        # ── Fresh start path ─────────────────────────────────────
        else:
            try:
                source_job = client.get_job(job_id)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

            source_agent = source_job.get("target_agent_id") or source_job.get("target_queue", "")
            dev_agent = dev_id or source_agent
            if not dev_id and dev_agent == reviewer_id:
                typer.echo(
                    "[review] Error: source job's agent is the reviewer itself. "
                    "Use --dev to specify which agent should apply fixes.",
                    err=True,
                )
                raise typer.Exit(1)

            conversation_id = source_job.get("conversation_id")
            original_job_id = job_id
            review_session_id = f"rev_{uuid.uuid4().hex[:12]}"
            review_attempt_id = uuid.uuid4().hex[:12]
            start_round = 1

            # Save initial review state
            _save_review_state(client, _build_review_state(
                review_session_id=review_session_id, source_job_id=job_id,
                reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                current_round=1, phase="send_to_reviewer",
                conversation_id=conversation_id,
                review_attempt_id=review_attempt_id,
            ))

        # ── Shared review loop ───────────────────────────────────
        short_output_guidance = (
            "The attached result may legitimately be short, single-line, or an exact-output-only reply. "
            "Do not infer staging failure or incompleteness from short length alone; review the content that was actually delivered."
        )

        for round_num in range(start_round, max_rounds + 1):
            typer.echo(f"[review] Round {round_num}/{max_rounds}")

            # Build attachments list for the review send
            review_attachments: list[dict[str, str]] = []

            if round_num == 1:
                # First round: send source job result to reviewer
                result_artifact_id = source_job.get("result_artifact_id")
                review_text = prompt
                if result_artifact_id:
                    try:
                        artifact = client.fetch_artifact(result_artifact_id, content=True)
                        artifact_content = _strip_tui_action_traces(artifact.get("content", ""))
                        if artifact_content:
                            attachment_name = f"agp-review-{job_id}-source.txt"
                            review_attachments.append({"name": attachment_name, "role": "source-output", "content": artifact_content})
                            review_text = f"{prompt}\n\n" + _review_attachment_note(
                                attachment_name=attachment_name,
                                short_output_guidance=short_output_guidance,
                            )
                    except Exception:
                        review_text = f"{prompt}\n\n(Could not fetch source job artifact.)"
                # Best-effort: attach git diff alongside source output
                if attach_diff:
                    _stat, _diff = _capture_git_diff()
                    if _stat:
                        review_attachments.append({"name": f"agp-review-{job_id}-diff-stat.txt", "role": "diff-summary", "content": _stat})
                    if _diff:
                        review_attachments.append({"name": f"agp-review-{job_id}-diff.txt", "role": "diff-full", "content": _diff})
                        review_text += f"\n\nA git diff is attached as agp-review-{job_id}-diff.txt for supplementary context. Use it only to verify claims in the primary artifact — do not review unrelated diff content."
            else:
                # Subsequent rounds: send dev's fixes to reviewer
                fix_artifact_id = source_job.get("result_artifact_id")
                if fix_artifact_id:
                    try:
                        fix_artifact = client.fetch_artifact(fix_artifact_id, content=True)
                        fix_content = _strip_tui_action_traces(fix_artifact.get("content", ""))
                        attachment_note = ""
                        if fix_content:
                            attachment_name = f"agp-review-{job_id}-fix-r{round_num}.txt"
                            review_attachments.append({"name": attachment_name, "role": "fix-output", "content": fix_content})
                            attachment_note = _review_fix_attachment_note(
                                attachment_name=attachment_name,
                                short_output_guidance=short_output_guidance,
                            )
                        review_text = (
                            f"{prompt}\n\n"
                            f"[Round {round_num}] The developer addressed issues from the previous review.\n"
                            f"{attachment_note or 'Updated result is attached and should also be materialized into the workspace.'}"
                        )
                    except Exception:
                        review_text = f"[Round {round_num}] Please re-review the changes. The developer was asked to fix issues from the previous review."
                else:
                    review_text = f"[Round {round_num}] Please re-review the changes. The developer was asked to fix issues from the previous review."

            output_contract = {
                "format": "json",
                "json_schema": {
                    "type": "object",
                    "required": ["verdict", "summary"],
                    "properties": {
                        "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
                        "summary": {"type": "string"},
                        "findings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "description": {"type": "string"},
                                    "file": {"type": "string"},
                                    "line": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            }

            # Save state: about to send to reviewer
            _save_review_state(client, _build_review_state(
                review_session_id=review_session_id, source_job_id=original_job_id,
                reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                current_round=round_num, phase="send_to_reviewer",
                conversation_id=conversation_id, review_attempt_id=review_attempt_id,
            ))

            typer.echo(f"[review] Sending to reviewer {reviewer_id}...")
            try:
                review_result = client.send(
                    "agent", reviewer_id, review_text,
                    conversation_id=conversation_id,
                    output_contract=output_contract,
                    attachments=review_attachments,
                    idempotency_key=f"review-{job_id}-r{round_num}-{review_attempt_id}",
                )
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)
            review_job_id = review_result["job_id"]

            # Save state: polling reviewer
            review_state = _build_review_state(
                review_session_id=review_session_id, source_job_id=original_job_id,
                reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                current_round=round_num, phase="poll_reviewer",
                conversation_id=conversation_id, active_job_id=review_job_id,
                review_attempt_id=review_attempt_id,
            )
            _save_review_state(client, review_state)

            review_job, timed_out = _poll_until_done(client, review_job_id, timeout_per_round)

            if timed_out:
                typer.echo(f"[review] Round {round_num} timed out waiting for reviewer.")
                _print_review_detached(review_state)
                return

            if review_job["status"] == "failed":
                typer.echo(f"[review] Round {round_num} reviewer job failed.")
                _print_job_result(review_job, client)
                raise typer.Exit(1)

            # Parse reviewer output
            verdict, summary, review_payload_text = _parse_reviewer_verdict(client, review_job)

            typer.echo(f"[review] Verdict: {verdict}")
            typer.echo(f"[review] Summary: {summary[:200]}")

            if verdict == "approved":
                _save_review_state(client, _build_review_state(
                    review_session_id=review_session_id, source_job_id=original_job_id,
                    reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                    current_round=round_num, phase="completed",
                    conversation_id=conversation_id, last_verdict="approved",
                    review_attempt_id=review_attempt_id,
                ))
                typer.echo(f"[review] Approved after {round_num} round(s).")
                return

            if round_num < max_rounds:
                # Send findings to dev for fixing
                try:
                    fix_result = _send_to_dev(
                        client, dev_agent=dev_agent, round_num=round_num,
                        review_payload_text=review_payload_text,
                        conversation_id=conversation_id, job_id=job_id,
                        review_attempt_id=review_attempt_id,
                    )
                except _httpx.HTTPStatusError as exc:
                    typer.echo(_format_http_error(exc), err=True)
                    raise typer.Exit(1)
                fix_job_id = fix_result["job_id"]

                # Save state: polling dev
                dev_state = _build_review_state(
                    review_session_id=review_session_id, source_job_id=original_job_id,
                    reviewer_id=reviewer_id, dev_id=dev_agent, max_rounds=max_rounds,
                    current_round=round_num, phase="poll_dev",
                    conversation_id=conversation_id, active_job_id=fix_job_id,
                    last_verdict=verdict, review_attempt_id=review_attempt_id,
                )
                _save_review_state(client, dev_state)

                fix_job, fix_timed_out = _poll_until_done(client, fix_job_id, timeout_per_round)
                if fix_timed_out:
                    typer.echo("[review] Dev fix timed out.")
                    _print_review_detached(dev_state)
                    return
                if fix_job["status"] == "failed":
                    typer.echo("[review] Dev fix job failed.")
                    _print_job_result(fix_job, client)
                    raise typer.Exit(1)

                # Update source_job reference for next round's context
                source_job = fix_job
                job_id = fix_job_id

        typer.echo(f"[review] Max rounds ({max_rounds}) reached without approval.")


# ── 1c. review-status / review-diagnose ──────────────────────────────


@app.command(name="review-status")
def review_status_cmd(
    source_job_id: str = typer.Argument(..., help="Source job ID of the review session."),
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show the current state of a review session."""
    with _cli_client(server_url) as client:
        state = _load_review_state(client, source_job_id)
        if state is None:
            typer.echo(f"No review session found for job {source_job_id}.", err=True)
            raise typer.Exit(1)

        if output_json:
            typer.echo(json.dumps(state, indent=2, default=str))
            return

        typer.echo(f"Review Session: {state.get('review_session_id', '?')}")
        typer.echo(f"  source_job:    {state.get('source_job_id', '?')}")
        typer.echo(f"  reviewer:      {state.get('reviewer_id', '?')}")
        typer.echo(f"  dev:           {state.get('dev_id', '?')}")
        typer.echo(f"  round:         {state.get('current_round', '?')}/{state.get('max_rounds', '?')}")
        typer.echo(f"  phase:         {state.get('phase', '?')}")
        typer.echo(f"  active_job:    {state.get('active_job_id', 'none')}")
        typer.echo(f"  last_verdict:  {state.get('last_verdict', 'none')}")
        typer.echo(f"  updated_at:    {state.get('updated_at', '?')}")

        # Show active job status if there's one running
        active = state.get("active_job_id")
        if active:
            try:
                job = client.get_job(active)
                typer.echo(f"\n  Active Job ({active}):")
                typer.echo(f"    status:  {job.get('status', '?').upper()}")
                run_id = job.get("latest_run_id")
                if run_id:
                    typer.echo(f"    run:     {run_id}")
            except Exception:
                typer.echo(f"\n  Active Job ({active}): unreachable")


@app.command(name="review-diagnose")
def review_diagnose_cmd(
    source_job_id: str = typer.Argument(..., help="Source job ID of the review session."),
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Diagnose a review session — show state, job health, extraction diagnostics."""
    with _cli_client(server_url) as client:
        state = _load_review_state(client, source_job_id)
        if state is None:
            typer.echo(f"No review session found for job {source_job_id}.", err=True)
            raise typer.Exit(1)

        diagnosis: dict = {"review_state": state, "jobs": {}, "extraction_diagnostics": {}}

        # Gather job states for active and source jobs
        for label, jid in [("source", source_job_id), ("active", state.get("active_job_id"))]:
            if not jid:
                continue
            try:
                job = client.get_job(jid)
                diagnosis["jobs"][label] = {
                    "job_id": jid,
                    "status": job.get("status"),
                    "target_agent_id": job.get("target_agent_id"),
                    "latest_run_id": job.get("latest_run_id"),
                    "updated_at": job.get("updated_at"),
                }
                # Check for extraction diagnostics artifact
                try:
                    arts = client.list_job_artifacts(jid, role="extraction_diagnostics")
                    items = arts.get("items", [])
                    if items:
                        art = client.fetch_artifact(items[-1]["artifact_id"], content=True)
                        diagnosis["extraction_diagnostics"][label] = json.loads(art.get("content", "{}"))
                except Exception as diag_exc:
                    typer.echo(f"[warn] Failed to fetch extraction diagnostics for {label}: {diag_exc}", err=True)
            except Exception as job_exc:
                typer.echo(f"[warn] Failed to fetch job {jid}: {job_exc}", err=True)
                diagnosis["jobs"][label] = {"job_id": jid, "status": "unreachable"}

        # Check reviewer runtime health
        reviewer_id = state.get("reviewer_id")
        if reviewer_id:
            try:
                agents = client.list_agents(limit=200).get("items", [])
                reviewer_agent = next((a for a in agents if a.get("agent_id") == reviewer_id), None)
                if reviewer_agent:
                    diagnosis["reviewer_runtime"] = {
                        "agent_status": reviewer_agent.get("status"),
                    }
                    # Find runtimes bound to this agent via claimed_work
                    rt_page = client.list_runtimes(limit=200)
                    bound_rts = [
                        rt for rt in rt_page.get("items", [])
                        if rt.get("agent_id") == reviewer_id
                    ]
                    diagnosis["reviewer_runtime"]["runtime_ids"] = [rt.get("runtime_id") for rt in bound_rts]
                    if bound_rts:
                        rt = bound_rts[0]
                        hb_age = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
                        if hb_age is not None:
                            diagnosis["reviewer_runtime"]["heartbeat_age"] = hb_age
                        diagnosis["reviewer_runtime"]["runtime_status"] = rt.get("status")
            except Exception as rt_exc:
                typer.echo(f"[warn] Failed to fetch reviewer runtime health: {rt_exc}", err=True)

        if output_json:
            typer.echo(json.dumps(diagnosis, indent=2, default=str))
            return

        # Pretty print
        typer.echo(f"Review Diagnosis: {state.get('review_session_id', '?')}")
        typer.echo(f"  phase:        {state.get('phase', '?')}")
        typer.echo(f"  round:        {state.get('current_round', '?')}/{state.get('max_rounds', '?')}")
        typer.echo(f"  last_verdict: {state.get('last_verdict', 'none')}")

        for label, jdata in diagnosis.get("jobs", {}).items():
            typer.echo(f"\n  {label.title()} Job ({jdata.get('job_id', '?')}):")
            typer.echo(f"    status:  {jdata.get('status', '?')}")
            if jdata.get("latest_run_id"):
                typer.echo(f"    run:     {jdata['latest_run_id']}")

        rt_info = diagnosis.get("reviewer_runtime", {})
        if rt_info:
            typer.echo(f"\n  Reviewer Runtime:")
            typer.echo(f"    agent_status:   {rt_info.get('agent_status', '?')}")
            hb = rt_info.get("heartbeat_age")
            typer.echo(f"    heartbeat:      {f'{hb:.0f}s ago' if hb is not None else 'never'}")
            typer.echo(f"    runtime_status: {rt_info.get('runtime_status', '?')}")

        for label, ediag in diagnosis.get("extraction_diagnostics", {}).items():
            typer.echo(f"\n  Extraction Diagnostics ({label}):")
            typer.echo(f"    source:           {ediag.get('selected_source', '?')}")
            typer.echo(f"    file_present:     {ediag.get('file_result_present', '?')}")
            typer.echo(f"    file_valid:       {ediag.get('file_result_valid', '?')}")
            typer.echo(f"    terminal_found:   {ediag.get('terminal_candidates_found', 0)}")
            typer.echo(f"    failure_category: {ediag.get('failure_category', 'none')}")
            for w in ediag.get("warnings", []):
                typer.echo(f"    warning: {w}")


# ── 1d. diagnose ────────────────────────────────────────────────────


def _diagnose_agent(client, agent_id: str, *, output_json: bool = False) -> None:
    """Diagnose an agent — show detail, runtime binding, heartbeat, and recent jobs."""
    import httpx as _httpx

    try:
        agent = client.get_agent(agent_id)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            typer.echo(f"Agent '{agent_id}' not found.", err=True)
        else:
            typer.echo(_format_http_error(exc), err=True)
        raise typer.Exit(1)

    diagnosis: dict = {"agent": agent, "runtime": None, "recent_jobs": []}

    # Runtime binding — query all runtimes and find the one bound to this agent.
    try:
        rt_page = client.ops_list_runtimes(limit=200)
        bound_rts = [
            rt for rt in rt_page.get("items", [])
            if rt.get("agent_id") == agent_id
        ]
        if bound_rts:
            diagnosis["runtime"] = bound_rts[0]
    except Exception:  # noqa: BLE001
        pass

    # Recent jobs targeting this agent
    try:
        jobs_data = client.list_jobs(target_agent_id=agent_id, limit=10)
        diagnosis["recent_jobs"] = jobs_data.get("items", [])
    except Exception:  # noqa: BLE001
        pass

    if output_json:
        typer.echo(json.dumps(diagnosis, indent=2, default=str))
        return

    # Pretty print
    typer.echo(f"Agent: {agent_id}")
    typer.echo(f"  status:       {agent.get('status', '?')}")
    typer.echo(f"  capabilities: {', '.join(agent.get('capabilities', [])) or 'none'}")
    typer.echo(f"  workspace:    {agent.get('workspace_ref') or '(none)'}")
    typer.echo(f"  registered:   {agent.get('created_at', '?')}")

    # Heartbeat
    hb = _heartbeat_age_seconds(agent.get("last_heartbeat_at"))
    typer.echo(f"  heartbeat:    {f'{hb:.0f}s ago' if hb is not None else 'never'}")

    # Queue depth (if available from get_agent)
    qd = agent.get("queue_depth")
    if qd is not None:
        typer.echo(f"  queue_depth:  {qd}")

    # Runtime binding
    rt = diagnosis["runtime"]
    if rt:
        typer.echo(f"\n  Runtime Binding:")
        typer.echo(f"    runtime_id: {rt.get('runtime_id', '?')}")
        typer.echo(f"    status:     {rt.get('status', '?')}")
        typer.echo(f"    host:       {rt.get('hostname', '?')}")
    else:
        typer.echo(f"\n  Runtime Binding: none")

    # Recent jobs
    jobs = diagnosis["recent_jobs"]
    if jobs:
        typer.echo(f"\n  Recent Jobs ({len(jobs)}):")
        for j in jobs:
            status = j.get("status", "?")
            created = j.get("created_at", "?")
            job_id = j.get("job_id", "?")
            typer.echo(f"    {job_id}  status={status}  created={created}")
    else:
        typer.echo(f"\n  Recent Jobs: none")


@app.command(name="diagnose")
def diagnose_cmd(
    entity_type: str = typer.Argument(..., help="Entity type: 'runtime' or 'agent'"),
    entity_id: str = typer.Argument(..., help="Runtime or agent ID to diagnose."),
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Diagnose a runtime or agent — show registration, heartbeat, jobs, and logs."""
    if entity_type not in ("runtime", "agent"):
        typer.echo(f"Unknown entity type '{entity_type}'. Supported: runtime, agent", err=True)
        raise typer.Exit(1)

    with _cli_client(server_url) as client:
        if entity_type == "agent":
            _diagnose_agent(client, entity_id, output_json=output_json)
            return

        rt = client.ops_get_runtime(entity_id)
        if rt is None:
            typer.echo(f"Runtime '{entity_id}' not found.", err=True)
            raise typer.Exit(1)

        diagnosis: dict = {
            "runtime": rt,
            "agents": [],
            "recent_logs": [],
        }

        # Use agents already returned by the runtime detail API
        diagnosis["agents"] = rt.get("agents", [])

        # Recent runtime logs
        try:
            logs = client.logs_runtime(entity_id, limit=20)
            diagnosis["recent_logs"] = logs.get("entries", logs) if isinstance(logs, dict) else logs
        except Exception as logs_exc:
            typer.echo(f"[warn] Failed to fetch runtime logs: {logs_exc}", err=True)

        if output_json:
            typer.echo(json.dumps(diagnosis, indent=2, default=str))
            return

        # Pretty print
        typer.echo(f"Runtime: {entity_id}")
        typer.echo(f"  status:     {rt.get('status', '?')}")
        hb = rt.get("heartbeat_age_seconds")
        if hb is None:
            hb_raw = rt.get("last_heartbeat_at")
            if hb_raw:
                from datetime import datetime, timezone
                try:
                    hb_dt = datetime.fromisoformat(str(hb_raw).replace("Z", "+00:00"))
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                    hb = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                except (ValueError, TypeError):
                    pass
        typer.echo(f"  heartbeat:  {f'{hb:.0f}s ago' if hb is not None else 'never'}")
        typer.echo(f"  host:       {rt.get('hostname', '?')}")
        typer.echo(f"  registered: {rt.get('created_at', '?')}")

        if diagnosis["agents"]:
            typer.echo(f"\n  Bound Agents:")
            for a in diagnosis["agents"]:
                caps = ", ".join(a.get("capabilities", []))
                typer.echo(f"    {a['agent_id']}  status={a['status']}  caps=[{caps}]")
        else:
            typer.echo(f"\n  Bound Agents: none")

        logs = diagnosis.get("recent_logs", [])
        if logs:
            entries = logs[-10:] if isinstance(logs, list) else []
            if entries:
                typer.echo(f"\n  Recent Logs (last {len(entries)}):")
                for entry in entries:
                    if isinstance(entry, dict):
                        ts = entry.get("created_at", "?")
                        action = entry.get("action", entry.get("kind", "?"))
                        typer.echo(f"    [{ts}] {action}")
                    else:
                        typer.echo(f"    {str(entry)[:120]}")


# ── 2. wait ──────────────────────────────────────────────────────────


@app.command(name="wait")
def wait_cmd(
    job_id: str = typer.Argument(..., help="Job ID to re-attach to."),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout: int = typer.Option(300, help="Wait timeout in seconds (default: 300)."),
) -> None:
    """Re-attach to a running job and wait for its result."""
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Quick check — maybe it already finished
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        if job["status"] in ("completed", "failed", "cancelled"):
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        agent_id = job.get("target_agent_id", "?")
        typer.echo(f"[..] Re-attaching to {job_id} (agent={agent_id})...")
        _print_peek_tip(agent_id)
        try:
            job, timed_out = _poll_until_done(client, job_id, timeout)
        except KeyboardInterrupt:
            _print_detached(job_id, agent_id)
            raise typer.Exit(0)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        if not timed_out:
            _print_job_result(job, client)
            if job["status"] == "failed":
                raise typer.Exit(1)
            return

        typer.echo("timeout — job still running", err=True)
        typer.echo(f"Check again with: agp status {job_id}")
        raise typer.Exit(1)


# ── 2b. health ──────────────────────────────────────────────────────


@app.command()
def health(
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show control-plane, runtime, and agent health at a glance."""
    import httpx as _httpx

    with _make_client(server_url) as client:
        try:
            cp_health = client.health()
        except (_httpx.RequestError, _httpx.HTTPStatusError) as exc:
            typer.echo(f"Control plane unreachable: {exc}", err=True)
            raise typer.Exit(1)

        # Ops health (runtimes, agents, jobs)
        ops: dict | None = None
        agents: list[dict] = []
        runtimes: list[dict] = []
        try:
            ops = client.ops_health()
        except (_httpx.HTTPStatusError, _httpx.RequestError, RuntimeError):
            pass
        try:
            page = client.list_agents(limit=200)
            agents = page.get("items", [])
        except Exception:
            pass
        try:
            rt_page = client.list_runtimes(limit=200)
            runtimes = rt_page.get("items", [])
        except Exception:
            pass

        # Filter synthetic rtm_ runtimes (created by agent_up, no backing process)
        runtimes = [rt for rt in runtimes if not rt.get("runtime_id", "").startswith("rtm_")]

        if output_json:
            typer.echo(json.dumps({
                "control_plane": cp_health,
                "ops": ops,
                "agents": agents,
                "runtimes": runtimes,
            }, indent=2, default=str))
            return

        # Control plane — unwrap {"ok": ..., "data": {...}} envelope
        cp_data = cp_health.get("data", cp_health)
        cp_status = cp_data.get("status", "unknown")
        typer.echo(f"Control Plane: {cp_status}")
        for k, v in cp_data.get("components", {}).items():
            typer.echo(f"  {k}: {v}")

        # Runtimes
        typer.echo(f"\nRuntimes: {len(runtimes)}")
        for rt in runtimes:
            rid = rt.get("runtime_id", "?")
            hb_age = rt.get("heartbeat_age_seconds")
            if hb_age is None:
                hb_age = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
            hb_str = f"{hb_age:.0f}s ago" if hb_age is not None else "never"
            # Show bound agent from agent_id field, fall back to claimed_work leases
            bound_aid = rt.get("agent_id")
            if bound_aid:
                agents_bound = bound_aid
            else:
                agents_bound = ", ".join(
                    sorted({w.get("agent_id", "?") for w in rt.get("claimed_work", [])})
                ) or "none"
            typer.echo(f"  {rid}  heartbeat={hb_str}  agents=[{agents_bound}]")

        # Agents
        typer.echo(f"\nAgents: {len(agents)}")
        for agent in agents:
            aid = agent.get("agent_id", "?")
            state = agent.get("status", "unknown")
            caps = ", ".join(agent.get("capabilities", []))
            qdepth = int(agent.get("queue_depth", 0) or 0)
            parts = [f"  {aid}  status={state}"]
            if caps:
                parts.append(f"caps=[{caps}]")
            if qdepth > 0:
                parts.append(f"queue={qdepth}")
            typer.echo("  ".join(parts))

        # Queue summary
        if ops:
            queue = ops.get("queue") or {}
            depth = int(queue.get("depth") or 0)
            if depth > 0:
                typer.echo(f"\nQueue depth: {depth}")


# ── 3. status ────────────────────────────────────────────────────────


@app.command()
def status(
    target: str = typer.Argument(None, help="Job ID or agent ID (optional)."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Check job or agent status.

    With a job ID: shows full job details + artifacts.
    With an agent ID: shows agent status, heartbeat, and current job.
    With no arguments: quick reachability check (use ``agp health`` for full details).
    """
    if target is None:
        _status_ping(server_url)
        return
    # Try as job first; on 404, try as agent before giving up
    import httpx as _httpx
    with _cli_client(server_url) as client:
        try:
            job = client.get_job(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)
            # Not a job — try as agent
            try:
                agent = client.get_agent(target)
            except _httpx.HTTPStatusError as exc2:
                if exc2.response.status_code == 404:
                    typer.echo(f"Not found: '{target}' is neither a job ID nor an agent ID.", err=True)
                else:
                    typer.echo(_format_http_error(exc2), err=True)
                raise typer.Exit(1)
            _status_agent(agent, client)
            return
        _status_job_from_data(job, client)


def _status_agent(agent: dict, client) -> None:
    """Show agent status summary."""
    aid = agent["agent_id"]
    typer.echo(f"AGENT:        {aid}")
    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"CAPABILITIES: {', '.join(agent.get('capabilities', [])) or '-'}")

    # Heartbeat
    hb_age = _heartbeat_age_seconds(agent.get("last_heartbeat_at"))
    if hb_age is not None:
        typer.echo(f"HEARTBEAT:    {hb_age:.0f}s ago")

    qdepth = int(agent.get("queue_depth", 0) or 0)
    if qdepth:
        typer.echo(f"QUEUE_DEPTH:  {qdepth}")

    workspace = agent.get("workspace_ref")
    if workspace:
        typer.echo(f"WORKSPACE:    {workspace}")

    # Show current job if busy
    if agent.get("status") == "busy":
        try:
            running = client.list_jobs(status="running", target_agent_id=aid, limit=1)
            items = running.get("items", [])
            if items:
                typer.echo(f"CURRENT_JOB:  {items[0]['job_id']}")
                _print_peek_tip(aid)
        except Exception:
            pass


def _status_ping(server_url: str | None) -> None:
    """Quick CP reachability check."""
    try:
        with _make_client(server_url) as client:
            data = client.health()
        cp_data = data.get("data", data)
        components = cp_data.get("components", {})
        parts = [f"{k}={v}" for k, v in components.items()]
        typer.echo(f"CP reachable ({', '.join(parts) or 'ok'})")
        typer.echo("Run `agp health` for full system status.")
    except Exception as e:
        typer.echo(f"CP unreachable: {e}", err=True)
        raise typer.Exit(1)


def _status_job(job_id: str, server_url: str | None) -> None:
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        _status_job_from_data(job, client)


def _status_job_from_data(job: dict, client) -> None:
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)
    job_id = job["job_id"]

    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {job.get('target_agent_id', 'unknown')}")
    typer.echo(f"STATUS:       {job['status'].upper()}")
    if retry_count > 0:
        typer.echo(f"RETRIES:      {retry_count}/{max_retries}")
    if job.get("latest_run_id"):
        typer.echo(f"RUN:          {job['latest_run_id']}")
    typer.echo(f"CREATED:      {job.get('created_at', '?')}")
    typer.echo(f"UPDATED:      {job.get('updated_at', '?')}")

    if job["status"] in ("queued", "accepted", "leased", "running"):
        _print_peek_tip(job.get("target_agent_id", ""))

    # Show result/failure artifact if terminal
    if job["status"] in ("completed", "failed") and job.get("result_artifact_id"):
        try:
            art = client.fetch_artifact(job["result_artifact_id"], content=True)
            typer.echo("---")
            typer.echo(art.get("content", "(no content)"))
        except Exception:
            pass
    elif job["status"] == "failed":
        try:
            artifacts = client.list_job_artifacts(job_id, role="failure_evidence")
            items = artifacts.get("items", [])
            if items:
                art = client.fetch_artifact(items[0]["artifact_id"], content=True)
                typer.echo("---")
                typer.echo(art.get("content", "(no content)"))
        except Exception:
            pass


# ── 4. jobs ──────────────────────────────────────────────────────────


@app.command()
def jobs(
    server_url: str = typer.Option(None, help="CP URL."),
    limit: int = typer.Option(10, help="Max jobs to show."),
    agent: str = typer.Option(None, "--agent", help="Filter by agent ID."),
    filter_status: str = typer.Option(None, "--status", help="Filter by status (queued, running, completed, failed)."),
) -> None:
    """List recent jobs."""
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            data = client.list_jobs(
                limit=limit,
                target_agent_id=agent,
                status=filter_status,
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        items = data.get("items", [])
        if not items:
            typer.echo("(no jobs)")
            return
        for j in items:
            retry = f" retry={j['retry_count']}/{j['max_retries']}" if j.get("retry_count", 0) > 0 else ""
            typer.echo(
                f"  {j['job_id']}  {j['status']:10s}  agent={j.get('target_agent_id', '?')}{retry}"
            )


# ── 5. ls ────────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Format seconds into Xm:XXs or Xh:XXm."""
    if seconds < 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h:{minutes:02d}m"
    return f"{minutes:02d}m:{secs:02d}s"


@app.command()
def result(
    job_id: str = typer.Argument(..., help="Job ID to fetch output for."),
    server_url: str = typer.Option(None, help="CP URL."),
    role: str = typer.Option(None, "--role", help="Artifact role to fetch (default: transcript_log, falls back to result)."),
) -> None:
    """Dump the clean output of a completed job.

    Fetches the transcript (or result artifact) and prints it to stdout
    with no envelope or plumbing.  Useful for piping agent output into
    other tools.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            arts = client.list_job_artifacts(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        items = arts.get("items", [])
        # Preference order: explicit role > transcript_log > result > exec_log
        if role:
            candidates = [a for a in items if a.get("role") == role]
        else:
            candidates = (
                [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        if not candidates:
            typer.echo(f"No output artifact found for job {job_id}", err=True)
            available = [a.get("role") for a in items]
            if available:
                typer.echo(f"Available roles: {', '.join(available)}", err=True)
            raise typer.Exit(1)
        art = candidates[-1]  # latest
        try:
            data = client.fetch_artifact(art["artifact_id"], content=True)
            typer.echo(data.get("content") or "(no content)")
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)


@app.command()
def ls(
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """List logical agents and available capabilities."""
    from datetime import datetime, timezone
    import httpx as _httpx

    warning_items: list[str] = []

    with _cli_client(server_url) as client:
        try:
            agents: list[dict] = []
            cursor: str | None = None
            _MAX_PAGES = 10
            for _page_num in range(_MAX_PAGES):
                page = client.list_agents(limit=200, cursor=cursor)
                agents.extend(page.get("items", []))
                cursor = (page.get("page") or {}).get("next_cursor")
                if not cursor:
                    break
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        try:
            caps_data = client.list_capabilities()
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        caps = caps_data.get("items", [])

        # Build agent → runtime lookup (1:1 binding)
        agent_runtime: dict[str, str] = {}
        runtime_health: dict[str, tuple[str, str]] = {}
        try:
            runtimes_data = client.ops_list_runtimes(limit=200)
            for rt in runtimes_data.get("items", []):
                runtime_id = rt["runtime_id"]
                runtime_status = str(rt.get("status") or "-").lower()
                health_status = str(rt.get("health_status") or "-").lower()
                runtime_health[runtime_id] = (runtime_status, health_status)
                aid = rt.get("agent_id")
                if aid:
                    agent_runtime[aid] = runtime_id
        except Exception:
            pass  # ops endpoint may not be available

        # For busy agents, fetch their running job
        agent_jobs: dict[str, dict] = {}
        busy_agents = [a for a in agents if a.get("status") == "busy"]
        if busy_agents:
            try:
                running_jobs = client.list_jobs(status="running", limit=100)
                for j in running_jobs.get("items", []):
                    tid = j.get("target_agent_id")
                    if tid:
                        agent_jobs[tid] = j
            except _httpx.HTTPStatusError:
                pass  # job listing may fail; proceed without job details

        # ── Header
        typer.echo(_SEPARATOR)
        typer.echo("      AGP SERVICE DISCOVERY (agp ls)")
        typer.echo(_SEPARATOR)
        typer.echo("Logical agent view only. Use `agp health` or `agp diagnose runtime <id>` for runtime health.")
        typer.echo("")

        # ── Active Agents section
        active = list(agents)  # All agents in DB are live

        typer.echo("[ACTIVE AGENTS]")
        if not active:
            typer.echo("(none)")
        else:
            # Column headers
            typer.echo(
                f"{'ID':<20s} {'ROLE':<18s} {'STATUS':<8s} {'RUNTIME':<16s} "
                f"{'JOB_ID':<14s} {'TIME_ON_JOB':<12s} {'PENDING':<7s} {'QUEUE_AGE':<10s} {'WORKSPACE'}"
            )
            typer.echo("-" * 142)

            now = datetime.now(timezone.utc)
            for a in active:
                agent_id = a["agent_id"]
                role = ", ".join(a.get("capabilities", [])) or "-"
                agent_status = a.get("status", "?").upper()
                runtime_id = agent_runtime.get(agent_id, "-")
                workspace = a.get("workspace_ref") or "-"
                pending = str(a.get("queue_depth", 0))
                queue_depth = int(a.get("queue_depth", 0) or 0)
                queue_age_seconds = a.get("oldest_queue_age_seconds")
                queue_age = _format_duration(queue_age_seconds) if isinstance(queue_age_seconds, (int, float)) else "-"
                runtime_status, health_status = runtime_health.get(runtime_id, ("-", "-"))

                job = agent_jobs.get(agent_id)
                if job:
                    job_id = job["job_id"]
                    # Compute time on job
                    try:
                        created = datetime.fromisoformat(job["created_at"])
                        elapsed = (now - created).total_seconds()
                        time_on_job = _format_duration(elapsed)
                    except Exception:
                        time_on_job = "-"
                else:
                    job_id = "-"
                    time_on_job = "-"

                typer.echo(
                    f"{agent_id:<20s} {role:<18s} {agent_status:<8s} {runtime_id:<16s} "
                    f"{job_id:<14s} {time_on_job:<12s} {pending:<7s} {queue_age:<10s} {workspace}"
                )

                if queue_depth <= 0:
                    continue
                if runtime_id == "-":
                    warning_items.append(
                        f"- {agent_id}: {queue_depth} queued, no runtime bound. Start or re-register its runtime."
                    )
                    continue
                if runtime_status in {"degraded", "offline"} or health_status in {"degraded", "unreachable"}:
                    warning_items.append(
                        f"- {agent_id}: {queue_depth} queued, runtime {runtime_id} heartbeat stale ({health_status if health_status != '-' else runtime_status}). Restart that runtime."
                    )

        typer.echo("")

        if warning_items:
            typer.echo("[WARNINGS]")
            for item in warning_items:
                typer.echo(item)
            typer.echo("Action: run `make local-restart` to recover state, or `make local-up` for a clean start.")
            typer.echo("")

        # ── Available Capabilities section
        typer.echo("[AVAILABLE CAPABILITIES (On-Demand)]")
        if not caps:
            typer.echo("(none)")
        else:
            typer.echo(
                f"{'CAPABILITY':<20s} {'MODEL':<20s} {'TIER':<10s} {'VERSION'}"
            )
            typer.echo("-" * 70)

            for c in caps:
                cap_name = c.get("name", c["capability_id"])
                model = c.get("model_ref", "-") or "-"
                tier = c.get("resource_tier", "-") or "-"
                version = c.get("version", "-") or "-"
                typer.echo(
                    f"{cap_name:<20s} {model:<20s} {tier:<10s} {version}"
                )


# ── 6. info ──────────────────────────────────────────────────────────


def _print_capability_blueprint(cap: dict, *, indent: str = "") -> None:
    """Print capability blueprint fields."""
    typer.echo(f"{indent}MODEL:        {cap.get('model_ref') or '-'}")
    tier = cap.get("resource_tier") or "-"
    typer.echo(f"{indent}TIER:         {tier}")
    typer.echo(f"{indent}PERMISSION:   {cap.get('permission_profile') or 'default'}")

    reqs = cap.get("runtime_requirements_json") or {}

    network = reqs.get("network")
    filesystem = reqs.get("filesystem")
    if network or filesystem:
        typer.echo(f"{indent}ACCESS:")
        if network:
            typer.echo(f"{indent}  Network:    {network}")
        if filesystem:
            typer.echo(f"{indent}  Filesystem: {filesystem}")

    tools = reqs.get("tools")
    if tools and isinstance(tools, list):
        typer.echo(f"{indent}PRE-INSTALLED TOOLS:")
        for t in tools:
            typer.echo(f"{indent}  - {t}")

    restrictions = reqs.get("restrictions")
    if restrictions and isinstance(restrictions, list):
        typer.echo(f"{indent}RESTRICTIONS:")
        for r in restrictions:
            typer.echo(f"{indent}  - {r}")


@app.command()
def info(
    target: str = typer.Argument(..., help="Agent ID or capability name/ID."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Deep-dive context for an agent or capability.

    Accepts an agent ID (e.g. agt_local) or capability ID/name (e.g. cap_python).
    """
    from datetime import datetime, timezone
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Try agent first, fall back to capability
        agent = None
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if agent is not None:
            _info_agent(agent, client)
        else:
            _info_capability(target, client)


def _info_agent(agent: dict, client) -> None:
    from datetime import datetime, timezone

    agent_id = agent["agent_id"]

    typer.echo(_SEPARATOR)
    typer.echo(f"      AGENT INFO: {agent_id}")
    typer.echo(_SEPARATOR)

    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"CAPABILITIES: {', '.join(agent.get('capabilities', [])) or '-'}")

    # Heartbeat
    now = datetime.now(timezone.utc)
    hb_age = _heartbeat_age_seconds(agent.get("last_heartbeat_at"))
    if hb_age is not None:
        typer.echo(f"HEARTBEAT:    {hb_age:.0f}s ago")

    # Queue depth
    qdepth = int(agent.get("queue_depth", 0) or 0)
    typer.echo(f"QUEUE_DEPTH:  {qdepth}")

    # Current job for busy agents
    if agent.get("status") == "busy":
        try:
            running = client.list_jobs(status="running", target_agent_id=agent_id, limit=1)
            items = running.get("items", [])
            if items:
                job = items[0]
                try:
                    created = datetime.fromisoformat(job["created_at"])
                    elapsed = (now - created).total_seconds()
                    duration = _format_duration(elapsed)
                except Exception:
                    duration = "?"
                typer.echo(f"CURRENT_JOB:  {job['job_id']} (Running for {duration})")
        except Exception:
            pass

    # Uptime
    created_at = agent.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            uptime = (now - created).total_seconds()
            typer.echo(f"UPTIME:       {_format_duration(uptime)}")
        except Exception:
            pass

    workspace = agent.get("workspace_ref") or "-"
    typer.echo(f"WORKSPACE:    {workspace}")

    # Runtime binding — query by agent_id instead of guessing runtime ID prefixes
    try:
        rt_page = client.ops_list_runtimes(limit=200)
        bound_rts = [
            rt for rt in rt_page.get("items", [])
            if rt.get("agent_id") == agent_id
        ]
        if bound_rts:
            rt = bound_rts[0]
            typer.echo(f"RUNTIME:      {rt.get('runtime_id', '?')} ({rt.get('hostname', '?')})")
        else:
            typer.echo("RUNTIME:      (unbound)")
    except Exception:
        typer.echo("RUNTIME:      (unbound)")

    # Recent jobs
    try:
        jobs_data = client.list_jobs(target_agent_id=agent_id, limit=5)
        recent = jobs_data.get("items", [])
        if recent:
            typer.echo(f"\nRECENT JOBS ({len(recent)}):")
            for j in recent:
                typer.echo(f"  {j.get('job_id', '?')}  {j.get('status', '?')}")
    except Exception:
        pass


def _info_capability(target: str, client) -> None:
    import httpx as _httpx

    # Try by ID first, then search by name
    cap = None
    try:
        cap = client.get_capability(target)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
    except _httpx.RequestError as exc:
        typer.echo(f"unreachable: {exc}", err=True)
        raise typer.Exit(1)

    if cap is None:
        try:
            results = client.list_capabilities(name=target)
            items = results.get("items", [])
            if items:
                cap = items[0]
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        except _httpx.RequestError as exc:
            typer.echo(f"unreachable: {exc}", err=True)
            raise typer.Exit(1)

    if cap is None:
        typer.echo(f"Not found: {target} (not an agent ID or capability name)", err=True)
        raise typer.Exit(1)

    cap_name = cap.get("name", cap.get("capability_id", target))
    typer.echo(_SEPARATOR)
    typer.echo(f"      CAPABILITY INFO: {cap_name}")
    typer.echo(_SEPARATOR)
    _print_capability_blueprint(cap)


# ── 7. nudge ─────────────────────────────────────────────────────────


def _format_human_nudge(message: str) -> str:
    return (
        f"{_SEPARATOR}\n"
        f"[SYSTEM NUDGE] Human Co-Pilot Override\n"
        f"{_SEPARATOR}\n"
        f"SOURCE:       User / Lead Developer\n"
        f"PRIORITY:     CRITICAL OVERRIDE\n"
        f"\n"
        f'HUMAN MESSAGE: "{message}"\n'
        f"\n"
        f"ACTION REQUIRED: Acknowledge this pivot immediately. "
        f"Pause your current goals, use `agp ls` to find an available worker, "
        f"and execute the human's exact request."
    )


@app.command()
def nudge(
    target: str = typer.Argument(..., help="Target orchestrator agent ID."),
    message: str = typer.Argument(..., help="Message to inject."),
    server_url: str = typer.Option(None, help="CP URL."),
    priority: int = typer.Option(1, help="Priority (1=human, 2=job, 3=agenda, 4=system)."),
    source: str = typer.Option("human", help="Nudge source label."),
) -> None:
    """Send a nudge to an orchestrator's terminal.

    The nudge is queued and delivered by the nudge-loop daemon when
    the orchestrator's shell is idle.
    """
    if source == "human" and priority == 1:
        payload = _format_human_nudge(message)
    else:
        payload = (
            f"{_SEPARATOR}\n"
            f"[SYSTEM NUDGE] {source.replace('_', ' ').title()}\n"
            f"{_SEPARATOR}\n"
            f"{message}"
        )

    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            result = client.create_nudge(target, payload, priority=priority, source=source)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        typer.echo(f"nudge queued: {result['nudge_id']} (priority={priority}, target={target})")


# ── 8. nudge-loop ────────────────────────────────────────────────────


@app.command(hidden=True)
def nudge_loop(
    target: str = typer.Argument(..., help="Orchestrator agent ID to deliver nudges to."),
    session: str = typer.Option(None, help="Tmux session name (default: agp-<target>)."),
    server_url: str = typer.Option(None, help="CP URL."),
    poll_seconds: float = typer.Option(2.0, help="Poll interval for new nudges."),
    idle_polls: int = typer.Option(3, help="Consecutive stable polls before injecting."),
    max_iterations: int | None = typer.Option(None, help="Stop after N deliveries (for testing)."),
) -> None:
    """Daemon: deliver queued nudges into an orchestrator's tmux session.

    Monitors the nudge queue and the tmux session.  Only injects when
    the session output has stabilised (shell is idle).
    """
    _require_server_extra()

    import subprocess
    import time

    session_name = session or f"agp-{target}"
    delivered = 0

    typer.echo(f"nudge-loop: target={target}  session={session_name}  poll={poll_seconds}s")

    with _make_client(server_url) as client:
        while True:
            # Check for pending nudges
            nudge = client.next_nudge(target)
            if nudge is None:
                time.sleep(poll_seconds)
                continue

            # Wait for tmux session to be idle
            typer.echo(f"[nudge] pending: {nudge['nudge_id']} (priority={nudge['priority']}, source={nudge['source']})")
            idle = _wait_for_tmux_idle(session_name, poll_seconds=poll_seconds, idle_after=idle_polls)
            if not idle:
                typer.echo(f"[nudge] session {session_name} not idle, delivering anyway")

            # Inject into tmux
            payload = nudge["payload"]
            try:
                # Use tmux load-buffer + paste-buffer for clean multi-line injection
                subprocess.run(
                    ["tmux", "set-buffer", "-b", "agp-nudge", payload],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "paste-buffer", "-b", "agp-nudge", "-t", session_name],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "", "Enter"],
                    check=True, capture_output=True,
                )
                typer.echo(f"[nudge] delivered: {nudge['nudge_id']}")
            except subprocess.CalledProcessError as e:
                typer.echo(f"[nudge] delivery failed: {e}", err=True)

            delivered += 1
            if max_iterations is not None and delivered >= max_iterations:
                typer.echo(f"[nudge] reached max_iterations={max_iterations}, stopping")
                return

            time.sleep(poll_seconds)


def _wait_for_tmux_idle(
    session_name: str,
    *,
    poll_seconds: float = 2.0,
    idle_after: int = 3,
    timeout_seconds: float = 30.0,
) -> bool:
    """Wait until tmux session output stabilises.  Returns True if idle detected."""
    import subprocess
    import time

    last_output = ""
    stable_count = 0
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, text=True, timeout=5,
            )
            current = result.stdout.rstrip()
        except Exception:
            time.sleep(poll_seconds)
            continue

        if current == last_output:
            stable_count += 1
            if stable_count >= idle_after:
                return True
        else:
            stable_count = 0
            last_output = current

        time.sleep(poll_seconds)

    return False


# ── cleanup ──────────────────────────────────────────────────────────


@app.command()
def cleanup(
    workspace: str = typer.Argument(None, help="Workspace directory (default: cwd)."),
    keep_temp_artifacts: bool = typer.Option(False, "--keep-temp-artifacts", help="Skip temp artifact cleanup."),
) -> None:
    """Remove AGP temp artifacts and stale result files."""
    from agp.runtime._attachments import cleanup_temp_artifacts, cleanup_stale_result_files

    ws = Path(workspace) if workspace else None
    total = 0
    if not keep_temp_artifacts:
        n = cleanup_temp_artifacts(ws)
        if n:
            typer.echo(f"Cleaned {n} temp artifact director{'ies' if n != 1 else 'y'}.")
        total += n
    n = cleanup_stale_result_files()
    if n:
        typer.echo(f"Cleaned {n} stale result file{'s' if n != 1 else ''}.")
    total += n
    if total == 0:
        typer.echo("Nothing to clean.")


if __name__ == "__main__":
    app()
