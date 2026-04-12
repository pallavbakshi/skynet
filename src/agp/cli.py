"""CLI entrypoint for the AGP scaffold.

Primarily exposes the agent-facing client surface (send, wait, status,
ls, info, nudge, etc.) that talks to a running control plane over HTTP.
Operational commands still exist here as hidden compatibility shims so
older scripts keep working, but the intended operator entrypoint is the
``skyops`` CLI.

"""

import json
import logging
import os
import re as _re
import sys
import time
import uuid
from difflib import get_close_matches
from pathlib import Path

import typer

app = typer.Typer(help="AGP agent CLI.")

_SEND_REPLY_OPTION_NAMES = {
    "--attach",
    "--fire-and-forget",
    "--nudge",
    "--output-contract",
    "--poll-timeout",
    "--reply-to",
    "--server-url",
    "--timeout",
}



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


def _validate_send_reply_target(name: str, value: str) -> None:
    if value.startswith("-"):
        raise typer.BadParameter(
            f"{name} looks like an option: {value}. "
            "If your task text starts with option-like tokens, insert -- before the task."
        )


def _reject_suspicious_task_options(ctx: typer.Context, *, task: str | None) -> None:
    task_prefix = (task or "").split()
    candidate_tokens = [*task_prefix[:1], *ctx.args]
    for token in candidate_tokens:
        if not token.startswith("-"):
            continue
        if token == "--":
            continue
        if token in _SEND_REPLY_OPTION_NAMES:
            continue
        match = get_close_matches(token, sorted(_SEND_REPLY_OPTION_NAMES), n=1, cutoff=0.75)
        if not match:
            continue
        raise typer.BadParameter(
            f"unrecognized option-like token {token!r}; did you mean {match[0]!r}? "
            "If this is literal task text, insert -- before the task."
        )


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


    from agp.db import init_db

    init_db()
    typer.echo("Initialized database schema.")


@app.command(name="db-status", hidden=True)
def db_status() -> None:
    """Show current schema version and pending migrations."""


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
    workspace: str | None = typer.Option(None, help="Working directory for the agent's terminal session (default: runtime's cwd)."),
    log_level: str = typer.Option("WARNING", help="Python log level (DEBUG, INFO, WARNING, ERROR)."),
) -> None:
    """Continuously claim and execute jobs until stopped or iteration bound is hit."""

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
            workspace_ref=workspace,
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

    # Print meta from summary if available
    summary = job.get("summary_json") or job.get("summary") or {}
    if isinstance(summary, dict) and summary.get("model"):
        meta_parts = [summary["model"]]
        if summary.get("effort"):
            meta_parts.append(summary["effort"])
        if summary.get("tokens"):
            meta_parts.append(f"{summary['tokens']:,} tokens")
        if summary.get("context_pct"):
            meta_parts.append(f"{summary['context_pct']}% context")
        if summary.get("elapsed"):
            meta_parts.append(f"{summary['elapsed']:.1f}s")
        typer.echo(f"META:         {' · '.join(meta_parts)}")

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

    if job_status == "failed":
        agent_id = job.get("target_agent_id", "")
        if retry_count > 0:
            typer.echo("")
            typer.echo(
                f"Notice: System exhausted best-effort retries ({retry_count} attempts). "
                "Review the error log and pivot your strategy."
            )
        if agent_id:
            typer.echo(f"Tip: inspect the agent's terminal:  agp peek {agent_id}")


def _peek_tip(agent_id: str) -> str:
    """Return a tip for peeking at an agent's live terminal output."""
    return f"Tip: peek at live output with:  agp peek {agent_id}"


def _print_peek_tip(agent_id: str) -> None:
    """Print the peek tip."""
    typer.echo(_peek_tip(agent_id))


def _print_detached(job_id: str, agent_id: str) -> None:
    _print_banner("ACCEPTED", "Task Detached — Still Running")
    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {agent_id}")
    typer.echo(f"STATUS:       IN_PROGRESS")
    typer.echo("")
    typer.echo("The CLI stopped waiting — the job IS STILL RUNNING on the server.")
    typer.echo("The control plane will let it run up to 60 minutes before failing it.")
    typer.echo("")
    typer.echo("DO NOT resend the task. DO NOT assume it is stuck. Be patient.")
    typer.echo("")
    typer.echo("What to do next:")
    typer.echo(f"  agp peek {agent_id}                    # see what the agent is doing RIGHT NOW")
    typer.echo(f"  agp wait {job_id} --poll-timeout 3600  # block until done (up to CP limit)")
    typer.echo(f"  agp result {job_id}                    # fetch output once complete")
    typer.echo("")
    typer.echo("Tip: next time, pass --poll-timeout 1800 on `agp send` to avoid detaching.")


def _poll_until_done(client, job_id: str, timeout: float, heartbeat_interval: float = 10.0, *, job_created_at: float | None = None):
    """Poll job until terminal or timeout.  Returns (job_dict, timed_out).

    If *job_created_at* is given (monotonic-equivalent offset in seconds
    since job start), the elapsed counter shows total time since job
    creation rather than time since this call.
    """
    import time
    from datetime import datetime, timezone

    start = time.monotonic()
    elapsed_offset = job_created_at or 0.0
    deadline = start + timeout
    last_heartbeat = start
    next_peek_tip_at = 30  # first tip at 30s, then decaying frequency

    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return job, False

        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start + elapsed_offset)
            # Show queued status when agent hasn't claimed the job yet
            if job["status"] in ("queued", "accepted"):
                typer.echo(f"[..] Queued, waiting for agent... ({elapsed}s elapsed)")
                last_heartbeat = now
                time.sleep(2)
                continue
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
                    tui_state = (details.get("tui_state") or "").strip()
                    last_line = (details.get("last_line") or "").strip()
                    output_chars = details.get("output_chars")
                    activity = _heartbeat_activity_hint(
                        tui_state=tui_state,
                        last_line=last_line,
                        output_chars=output_chars,
                    )
                    if activity:
                        hint = f" \u2014 {activity}"
                    created_at = progress_ev.get("created_at", "")
                    if created_at:
                        ev_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - ev_time).total_seconds() > 30:
                            hint += " (stalled)"
            except Exception:
                pass
            typer.echo(f"[..] Agent working... ({elapsed}s elapsed){hint}")
            # Surface peek tip periodically (30s, then 90s, then every 120s)
            if elapsed >= next_peek_tip_at:
                agent_id = job.get("target_agent_id", "")
                if agent_id:
                    typer.echo(f"     Tip: agp peek {agent_id}")
                next_peek_tip_at = elapsed + (60 if next_peek_tip_at < 120 else 120)
            last_heartbeat = now

        time.sleep(2)

    return client.get_job(job_id), True  # last check before giving up


def _poll_jobs_until_done(
    client,
    job_ids: list[str],
    timeout: float,
    *,
    on_complete=None,
    on_error=None,
    heartbeat_interval: float = 10.0,
) -> tuple[dict[str, dict], set[str]]:
    """Poll multiple jobs concurrently until each is terminal or timeout.

    Visits every pending job on each iteration, so completions are surfaced
    as they happen — not in dispatch order. Calls ``on_complete(job_id, job)``
    the moment a job becomes terminal, then keeps polling the rest.  Permanent
    lookup errors (404) discard the job and call ``on_error(job_id, exc)``.

    ``job_ids`` may contain duplicates; they are deduped internally.

    Returns ``(results_by_id, still_pending)``.  ``still_pending`` contains
    any jobs that hadn't reached a terminal state when the timeout expired —
    jobs that completed during the final snapshot are NOT in this set.
    """
    import time
    import httpx as _httpx

    _TERMINAL = ("completed", "failed", "cancelled")
    pending = set(job_ids)
    dedup_total = len(pending)
    results: dict[str, dict] = {}
    start = time.monotonic()
    deadline = start + timeout
    last_heartbeat = start

    def _maybe_complete(jid: str, job: dict) -> bool:
        """Record a terminal-state job and notify the caller. Returns True if terminal."""
        if job.get("status") in _TERMINAL:
            results[jid] = job
            pending.discard(jid)
            if on_complete is not None:
                on_complete(jid, job)
            return True
        return False

    while pending and time.monotonic() < deadline:
        for jid in list(pending):
            try:
                job = client.get_job(jid)
            except _httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Job deleted/purged — permanent, stop polling it
                    pending.discard(jid)
                    results[jid] = {"job_id": jid, "status": "not_found"}
                    if on_error is not None:
                        on_error(jid, exc)
                    continue
                # Other HTTP errors (5xx, auth) — warn and retry next iter
                typer.echo(f"[warn] get_job({jid}): HTTP {exc.response.status_code}", err=True)
                continue
            except _httpx.RequestError as exc:
                typer.echo(f"[warn] get_job({jid}): {exc}", err=True)
                continue
            _maybe_complete(jid, job)
        if not pending:
            break
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            elapsed = int(now - start)
            done = dedup_total - len(pending)
            pending_list = ", ".join(sorted(pending))
            typer.echo(f"[..] {done}/{dedup_total} done — waiting on {len(pending)}: {pending_list} ({elapsed}s elapsed)")
            last_heartbeat = now
        time.sleep(2)

    # Final snapshot for anything still pending (timeout case).
    # A job may have transitioned between the last poll and this snapshot;
    # if so, treat it as a normal completion so the caller is NOT told it
    # "timed out" when it actually finished.
    for jid in list(pending):
        try:
            job = client.get_job(jid)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                pending.discard(jid)
                results[jid] = {"job_id": jid, "status": "not_found"}
                if on_error is not None:
                    on_error(jid, exc)
                continue
            results[jid] = {"job_id": jid, "status": "unknown"}
            continue
        except _httpx.RequestError:
            results[jid] = {"job_id": jid, "status": "unknown"}
            continue
        if not _maybe_complete(jid, job):
            results[jid] = job  # non-terminal — left in pending
    return results, pending


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

        # Force-delete in both cases: busy agents had --force, idle agents have no work to drain.
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


@app.command(
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": True,
        "ignore_unknown_options": True,
    }
)
def send(
    ctx: typer.Context,
    agent_id: str = typer.Argument(..., help="Target agent ID."),
    task: str | None = typer.Argument(None, help="Task text to send (reads from stdin when omitted or '-')."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    fire_and_forget: bool = typer.Option(False, "--fire-and-forget", help="Return immediately after dispatch — no sync window."),
    timeout: int = typer.Option(300, "--poll-timeout", "--timeout", help="Seconds the CLI waits for completion before detaching. Agent keeps running in the background up to the CP's execution deadline (default 60m)."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (useful with --fire-and-forget)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
    review: bool = typer.Option(False, "--review", help=(
        'Apply the standard review output contract. '
        'The agent returns JSON: {"verdict": "approved"|"changes_requested", '
        '"summary": "...", "findings": [{"severity": "high"|"medium"|"low", '
        '"description": "...", "file": "...", "line": N}]}. '
        'Filter with jq: agp result <id> | jq \'.findings[] | select(.severity == "high")\'.'
    )),
    reply_to: str | None = typer.Option(None, "--reply-to", help="Parent message ID for a multi-turn reply."),
    attach: list[str] = typer.Option(None, "--attach", help="Attach a text file as <path>:<role>. Repeatable."),
    via_file: str | None = typer.Option(None, "--via-file", help="Read task text from a file. Avoids shell quoting issues with complex prompts."),
    context_from: str | None = typer.Option(None, "--context-from", help="Prepend the result of a previous job as context. Pass a job ID."),
) -> None:
    """Send a task to an agent.

    Two independent timers to keep straight:

    - CLI poll window (--poll-timeout, default 300s): how long THIS terminal
      blocks waiting for the result. When it expires, the CLI detaches and
      returns — the job keeps running.
    - Server execution deadline (60 minutes, CP-enforced): how long the job
      is allowed to run before the control plane fails it. Not configurable
      from the CLI — this is server policy.

    When the CLI detaches, DO NOT assume the job failed. It is still running
    on the server. Use ``agp peek <agent>`` to see live progress, or
    ``agp wait <job_id> --poll-timeout 3600`` to block until the CP limit.
    Never resend the same task — you will get duplicate work.

    Use --fire-and-forget to return immediately after dispatch.
    Use --poll-timeout to change how long the CLI waits (e.g. --poll-timeout 1800).
    Use --nudge <orc_id> to get a push notification when the task finishes.

    Task input (in priority order):

      --via-file PATH   Read the task from a file (best for complex prompts
                        with code, shell metacharacters, or multiple lines).
      <task> argument   Inline text after the agent ID (supports unquoted words).
      stdin             Pipe or redirect: echo "..." | agp send agent
                        or: agp send agent - < prompt.md

    While waiting, use ``agp peek <agent_id>`` in another terminal to see
    what the agent is doing in real time.

    Examples:

      agp send claude-dev "fix the bug in cli.py"
      agp send claude-dev --via-file /tmp/task.md --fire-and-forget
      agp send claude-reviewer --review --via-file task.md --poll-timeout 300
      echo "what is 2+2?" | agp send claude-dev

      # Fan-out pattern: dispatch to multiple agents, then collect results
      agp send claude-dev --fire-and-forget "review src/cli.py"
      agp send codex-dev  --fire-and-forget "review src/cli.py"
      agp wait <job_id_1> <job_id_2>

    Unknown flags in the task (e.g. --resume, --no-cache) are passed through.
    If the task text must contain this command's own option names
    (--fire-and-forget, --timeout, etc.), insert ``--`` before the task
    to stop option parsing:
    ``agp send myagent -- fix the --timeout bug``
    """
    _validate_send_reply_target("agent_id", agent_id)
    _reject_suspicious_task_options(ctx, task=task)
    # Absorb extra positional tokens into task (unquoted multi-word support)
    if ctx.args:
        parts = [task] if task else []
        parts.extend(ctx.args)
        task = " ".join(parts)
    # Normalize: strip whitespace so "   " is treated as empty (stdin and
    # --via-file paths already strip).
    if task is not None and task != "-":
        task = task.strip() or None
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    conversation_id: str | None = None
    attachments: list[dict[str, str]] = []
    if review and output_contract is not None:
        raise typer.BadParameter("--review and --output-contract are mutually exclusive")
    if review:
        parsed_output_contract = _REVIEW_OUTPUT_CONTRACT
    elif output_contract is not None:
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
    # --via-file takes priority over inline task and stdin.
    if via_file is not None:
        if task and task != "-":
            raise typer.BadParameter("cannot combine --via-file with an inline task argument")
        fpath = Path(via_file).resolve()
        if not fpath.is_file():
            raise typer.BadParameter(f"--via-file: file not found: {via_file}")
        try:
            task = fpath.read_text(encoding="utf-8").strip()
        except (PermissionError, OSError) as exc:
            raise typer.BadParameter(f"--via-file: cannot read {via_file}: {exc}") from None
        except UnicodeDecodeError:
            raise typer.BadParameter(f"--via-file: file is not valid UTF-8: {via_file}") from None
        if not task:
            raise typer.BadParameter(f"--via-file: file is empty: {via_file}")
        # Store resolved path so the adapter can pass it to so.send(file=...)
        # instead of writing a second copy.
        metadata["via_file"] = str(fpath)
    elif task is None or task == "-":
        if sys.stdin.isatty():
            typer.echo("[..] Reading task from stdin (Ctrl-D to end, Ctrl-C to cancel)...")
        task = sys.stdin.read().strip()
    if not task:
        raise typer.BadParameter("task is required (pass as argument, --via-file, or pipe via stdin)")

    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Prepend context from a previous job result if requested
        if context_from:
            try:
                prev_job = client.get_job(context_from)
                art_id = prev_job.get("result_artifact_id")
                if art_id:
                    art_data = client.fetch_artifact(art_id, content=True)
                    context_text = art_data.get("content", "")
                    if context_text:
                        task = f"<context>\n{context_text}\n</context>\n\n{task}"
                else:
                    typer.echo(f"[warn] job {context_from} has no result artifact", err=True)
            except _httpx.HTTPStatusError as exc:
                typer.echo(f"[warn] --context-from: {_format_http_error(exc)}", err=True)

        typer.echo(f"[..] Dispatching to {agent_id}...")
        try:
            result = client.send(
                "agent", agent_id, task,
                metadata=metadata,
                output_contract=parsed_output_contract,
                conversation_id=conversation_id,
                reply_to_message_id=reply_to,
                attachments=attachments,
                idempotency_key=_cli_idempotency_key("cli"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        job_id = result["job_id"]

        # --fire-and-forget: return immediately after dispatch
        if fire_and_forget:
            _print_detached(job_id, agent_id)
            return

        # Sync path: print job ID early so the caller can poll independently
        typer.echo(f"JOB_ID:       {job_id}")
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


# `run` is an alias for `send` — many users reach for `agp run` first.
app.command(
    name="run",
    hidden=True,
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": True,
        "ignore_unknown_options": True,
    },
)(send)


@app.command(
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": True,
        "ignore_unknown_options": True,
    }
)
def reply(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="Source job ID to reply to."),
    task: str | None = typer.Argument(None, help="Reply text to send; if omitted, read from stdin."),
    server_url: str = typer.Option(None, help="CP URL (default: AGP_SERVER_URL or localhost:7860)."),
    fire_and_forget: bool = typer.Option(False, "--fire-and-forget", help="Return immediately after dispatch — no sync window."),
    timeout: int = typer.Option(300, "--poll-timeout", "--timeout", help="Seconds the CLI waits for completion before detaching. Agent keeps running in the background up to the CP's execution deadline (default 60m)."),
    nudge_target: str = typer.Option(None, "--nudge", help="Agent ID to nudge when job completes (useful with --fire-and-forget)."),
    output_contract: str | None = typer.Option(None, "--output-contract", help="JSON string describing the structured output contract."),
    review: bool = typer.Option(False, "--review", help=(
        'Apply the standard review output contract. '
        'The agent returns JSON: {"verdict": "approved"|"changes_requested", '
        '"summary": "...", "findings": [{"severity": "high"|"medium"|"low", '
        '"description": "...", "file": "...", "line": N}]}. '
        'Filter with jq: agp result <id> | jq \'.findings[] | select(.severity == "high")\'.'
    )),
    attach: list[str] = typer.Option(None, "--attach", help="Attach a text file as <path>:<role>. Repeatable."),
    via_file: str | None = typer.Option(None, "--via-file", help="Read reply text from a file. Avoids shell quoting issues with complex prompts."),
) -> None:
    """Reply to an existing job, preserving its conversation context.

    Continues the agent's conversation from where the previous job left off.
    Reply text can be passed as unquoted words after the job ID, via --via-file,
    or piped through stdin.

    Like ``send``, the CLI waits up to ``--poll-timeout`` seconds (default 300)
    for completion, then detaches. The agent keeps running in the background
    up to the CP's 60-minute execution deadline. Use --fire-and-forget to
    return immediately.

    When the CLI detaches, DO NOT resend — use ``agp peek`` to check progress
    or ``agp wait <job_id> --poll-timeout 3600`` to block until done.

    Examples:

      agp reply job_abc123 "now refactor the error handling too"
      agp reply job_abc123 --via-file /tmp/followup.md
      agp reply job_abc123 --fire-and-forget "apply the suggested fixes"

    Unknown flags in the reply (e.g. --resume, --no-cache) are passed through.
    If the reply text must contain this command's own option names
    (--fire-and-forget, --timeout, etc.), insert ``--`` before the reply
    to stop option parsing:
    ``agp reply job_x -- fix the --timeout bug``
    """
    _validate_send_reply_target("job_id", job_id)
    _reject_suspicious_task_options(ctx, task=task)
    # Absorb extra positional tokens into task (unquoted multi-word support)
    if ctx.args:
        parts = [task] if task else []
        parts.extend(ctx.args)
        task = " ".join(parts)
    # Normalize: strip whitespace so "   " is treated as empty.
    if task is not None and task != "-":
        task = task.strip() or None
    metadata: dict = {"kind": "cli"}
    if nudge_target:
        metadata["nudge_target"] = nudge_target
    parsed_output_contract: dict | None = None
    attachments: list[dict[str, str]] = []
    if review and output_contract is not None:
        raise typer.BadParameter("--review and --output-contract are mutually exclusive")
    if review:
        parsed_output_contract = _REVIEW_OUTPUT_CONTRACT
    elif output_contract is not None:
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
    # --via-file takes priority over inline task and stdin.
    if via_file is not None:
        if task and task != "-":
            raise typer.BadParameter("cannot combine --via-file with an inline task argument")
        fpath = Path(via_file).resolve()
        if not fpath.is_file():
            raise typer.BadParameter(f"--via-file: file not found: {via_file}")
        try:
            task = fpath.read_text(encoding="utf-8").strip()
        except (PermissionError, OSError) as exc:
            raise typer.BadParameter(f"--via-file: cannot read {via_file}: {exc}") from None
        except UnicodeDecodeError:
            raise typer.BadParameter(f"--via-file: file is not valid UTF-8: {via_file}") from None
        if not task:
            raise typer.BadParameter(f"--via-file: file is empty: {via_file}")
        metadata["via_file"] = str(fpath)
    elif task is None or task == "-":
        if sys.stdin.isatty():
            typer.echo("[..] Reading reply from stdin (Ctrl-D to end, Ctrl-C to cancel)...")
        task = sys.stdin.read().strip()
    if not task:
        raise typer.BadParameter("task is required (pass as argument, --via-file, or pipe via stdin)")

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
                attachments=attachments,
                idempotency_key=_cli_idempotency_key("cli-reply"),
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        new_job_id = result["job_id"]

        # --fire-and-forget: return immediately after dispatch
        if fire_and_forget:
            _print_detached(new_job_id, agent_id)
            return

        typer.echo(f"JOB_ID:       {new_job_id}")
        _print_peek_tip(agent_id)
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
    job_id: str = typer.Argument(None, help="Source job ID whose result should be reviewed."),
    reviewer_id: str = typer.Argument(None, help="Agent ID of the reviewer."),
    max_rounds: int = typer.Option(3, "--max-rounds", help="Maximum review rounds."),
    dev_id: str = typer.Option(None, "--dev", help="Agent ID of the developer (defaults to the source job's agent)."),
    prompt: str = typer.Option(
        "Review the attached output artifact for correctness, edge cases, and security. "
        "The artifact is the primary subject of review. "
        "If a git diff is also attached, it is supplementary context only — use it to verify or clarify claims in the artifact, but do not review unrelated files in the diff. "
        "Respond with a JSON object: {\"verdict\": \"approved\" or \"changes_requested\", \"summary\": \"...\", \"findings\": [{\"severity\": \"high|medium|low\", \"description\": \"...\", \"file\": \"path/to/file or null\", \"line\": 42 or null}]}. "
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

    if not resume:
        if not job_id:
            raise typer.BadParameter("JOB_ID is required (use --resume to resume a session)")
        if not reviewer_id:
            raise typer.BadParameter("REVIEWER_ID is required (use --resume to resume a session)")

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

            output_contract = _REVIEW_OUTPUT_CONTRACT

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


# ── 2. wait ──────────────────────────────────────────────────────────


@app.command(name="wait")
def wait_cmd(
    job_ids: list[str] = typer.Argument(..., help="One or more job IDs to wait on."),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout: int = typer.Option(300, "--poll-timeout", "--timeout", help="Wait timeout in seconds (default: 300)."),
) -> None:
    """Re-attach to running jobs and wait for their results.

    Accepts one or more job IDs. When multiple IDs are given, polls all of
    them concurrently and prints each result as soon as it is ready — you
    don't have to wait for the slowest job before seeing faster ones.

    The server lets jobs run up to 60 minutes before the CP fails them, but
    the CLI's default ``--poll-timeout`` is 300s. For long tasks (reviews,
    full-codebase scans), pass a larger window up front:

      agp wait job_abc123 --poll-timeout 3600   # block up to the CP limit

    If ``wait`` times out, the job IS STILL RUNNING — do not panic, do not
    resend. Re-run ``agp wait`` with a larger ``--poll-timeout``, or use
    ``agp peek <agent>`` to see live terminal state.

    Examples:

      agp wait job_abc123
      agp wait job_abc123 job_def456 job_ghi789
      agp wait job_abc123 --poll-timeout 3600
    """
    import httpx as _httpx

    had_failure = False

    def _print_timeout_hint(jid: str, agent: str) -> None:
        typer.echo(f"wait timeout — {jid} IS STILL RUNNING (not failed)", err=True)
        typer.echo("The CLI stopped polling. Server lets the job run up to 60 minutes total.", err=True)
        typer.echo("DO NOT resend. Be patient. What to do next:", err=True)
        if agent and agent != "?":
            typer.echo(f"  agp peek {agent}                    # see live terminal", err=True)
        typer.echo(f"  agp wait {jid} --poll-timeout 3600  # block until done", err=True)
        typer.echo(f"  agp result {jid}                    # fetch output once complete", err=True)

    # Dedupe job_ids while preserving order so `agp wait job_a job_a` doesn't
    # double-print or skew the N/M heartbeat denominator.
    job_ids = list(dict.fromkeys(job_ids))

    with _cli_client(server_url) as client:
        # Phase 1: triage — print already-done results, collect pending jobs
        pending: list[str] = []
        pending_agents: dict[str, str] = {}
        for jid in job_ids:
            try:
                job = client.get_job(jid)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                had_failure = True
                continue
            if job["status"] in ("completed", "failed", "cancelled"):
                _print_job_result(job, client)
                if job["status"] == "failed":
                    had_failure = True
            else:
                pending.append(jid)
                pending_agents[jid] = job.get("target_agent_id", "?")

        if not pending:
            if had_failure:
                raise typer.Exit(1)
            return

        # Phase 2a: single pending job — use rich per-job progress
        if len(pending) == 1:
            jid = pending[0]
            agent_id = pending_agents[jid]
            typer.echo(f"[..] Re-attaching to {jid} (agent={agent_id})...")
            _print_peek_tip(agent_id)
            job_age: float = 0.0
            try:
                from datetime import datetime, timezone
                src_job = client.get_job(jid)
                created_raw = src_job.get("created_at")
                if created_raw:
                    created = created_raw if isinstance(created_raw, datetime) else datetime.fromisoformat(str(created_raw))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    job_age = (datetime.now(timezone.utc) - created).total_seconds()
            except Exception:
                pass
            try:
                job, timed_out = _poll_until_done(client, jid, timeout, job_created_at=job_age)
            except KeyboardInterrupt:
                import time
                typer.echo("", err=True)
                typer.echo(f"Detached 1 job (still running in background):", err=True)
                typer.echo(f"  agp wait {jid}", err=True)
                typer.echo(f"\nCtrl+C again within 2s to stop it.", err=True)
                try:
                    time.sleep(2)
                except KeyboardInterrupt:
                    typer.echo(f"\nStopping {jid}...", err=True)
                    try:
                        client.interrupt(jid)
                        typer.echo(f"  {jid} — interrupted", err=True)
                    except Exception:
                        typer.echo(f"  {jid} — could not interrupt", err=True)
                raise typer.Exit(0)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                had_failure = True
            else:
                if not timed_out:
                    _print_job_result(job, client)
                    if job["status"] == "failed":
                        had_failure = True
                else:
                    _print_timeout_hint(jid, agent_id)
                    had_failure = True
            if had_failure:
                raise typer.Exit(1)
            return

        # Phase 2b: multiple pending jobs — concurrent poll, stream completions
        typer.echo(f"[..] Waiting on {len(pending)} job(s):")
        for jid in pending:
            typer.echo(f"     {jid}  agent={pending_agents[jid]}  (agp peek {pending_agents[jid]})")

        # Caller-side pending set stays in sync via on_complete so Ctrl+C
        # knows exactly which jobs are still unfinished. Discard happens
        # BEFORE any network-blocking print — otherwise a Ctrl+C during
        # _print_job_result (which fetches artifacts) would leak a completed
        # job back into pending_set.
        pending_set = set(pending)

        def _on_complete(jid: str, job: dict) -> None:
            nonlocal had_failure
            pending_set.discard(jid)
            _print_job_result(job, client)
            if job["status"] == "failed":
                had_failure = True

        def _on_error(jid: str, exc: Exception) -> None:
            nonlocal had_failure
            pending_set.discard(jid)
            typer.echo(f"error: {jid} — {exc}", err=True)
            typer.echo(f"  (job may have been deleted or purged)", err=True)
            had_failure = True

        try:
            _, still_pending = _poll_jobs_until_done(
                client, pending, timeout,
                on_complete=_on_complete, on_error=_on_error,
            )
        except KeyboardInterrupt:
            import time
            remaining = sorted(pending_set)
            typer.echo("", err=True)
            typer.echo(f"Detached {len(remaining)} job(s) (still running in background):", err=True)
            for jid in remaining:
                typer.echo(f"  agp wait {jid}", err=True)
            typer.echo(f"\nCtrl+C again within 2s to stop all jobs.", err=True)
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                typer.echo(f"\nStopping {len(remaining)} job(s)...", err=True)
                for jid in remaining:
                    try:
                        client.interrupt(jid)
                        typer.echo(f"  {jid} — interrupted", err=True)
                    except Exception:
                        typer.echo(f"  {jid} — could not interrupt", err=True)
            raise typer.Exit(0)

        for jid in sorted(still_pending):
            _print_timeout_hint(jid, pending_agents.get(jid, "?"))
            had_failure = True

    if had_failure:
        raise typer.Exit(1)


# ── 2b. peek ───────────────────────────────────────────────────────


def _peek_agent_status(agent_id: str, server_url: str | None) -> str | None:
    """Return a short status string for the peek header, or None on failure."""
    try:
        with _cli_client(server_url) as client:
            info = client.get_agent(agent_id)
            status = (info.get("status") or "unknown").upper()
            job_id = info.get("current_job_id") or ""
            if status == "BUSY" and job_id:
                return f"BUSY on {job_id}"
            return status
    except Exception:
        return None


def _try_local_peek(agent_id: str, *, lines: int = 0) -> str | None:
    """Try to capture terminal content locally (fast path)."""
    import shutil
    import subprocess

    # Try tmux first (most common host kind)
    if shutil.which("tmux"):
        session_name = f"agp-{agent_id}"
        try:
            check = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, timeout=3,
            )
            if check.returncode == 0:
                args = ["tmux", "capture-pane", "-t", session_name, "-p"]
                if lines and lines > 0:
                    args.extend(["-S", str(-lines)])
                result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    return result.stdout
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
                    if f"agp-{agent_id}" in title or title == agent_id:
                        pane_id = pane.get("pane_id")
                        args = ["wezterm", "cli", "get-text", "--pane-id", str(pane_id)]
                        if lines and lines > 0:
                            args.extend(["--start-line", str(-lines)])
                        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            return result.stdout
        except Exception:
            pass

    return None


@app.command()
def attach(
    agent_id: str = typer.Argument(..., help="Agent ID to attach to."),
) -> None:
    """Attach to an agent's live terminal session.

    Opens an interactive view of the agent's tmux or wezterm pane.
    Use Ctrl+B D (tmux) to detach without stopping the agent.

    Examples:

      agp attach claude-dev
      agp attach codex-dev
    """
    import os
    import subprocess

    session_name = f"agp-{agent_id}"

    # Try tmux
    try:
        check = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if check.returncode == 0:
            os.execvp("tmux", ["tmux", "attach", "-t", session_name])
    except FileNotFoundError:
        pass

    # Try wezterm — smallops marks panes with title "SMALLOPS:{agent_id}"
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            marker = f"SMALLOPS:{agent_id}"
            for pane in _json.loads(result.stdout):
                if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                    pane_id = str(pane.get("pane_id"))
                    subprocess.run(["wezterm", "cli", "activate-pane", "--pane-id", pane_id], check=False)
                    typer.echo(f"Activated wezterm pane {pane_id} for {agent_id}")
                    return
    except FileNotFoundError:
        pass

    typer.echo(f"No local session found for {agent_id} (looked for '{session_name}').", err=True)
    typer.echo(f"Use 'agp peek {agent_id}' for remote agents.", err=True)
    raise typer.Exit(1)


@app.command()
def peek(
    agent_id: str = typer.Argument(..., help="Agent ID to peek at."),
    lines: int = typer.Option(0, "--lines", "-n", help="Scrollback lines to capture (0 = visible screen only)."),
    timeout: float = typer.Option(45.0, "--timeout", help="Max seconds to wait for remote peek result."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Show live terminal content of an agent's runtime.

    Peek is the universal way to inspect what any agent is doing right now.
    It works regardless of terminal host (tmux, wezterm) and regardless of
    whether the agent is local or on a remote server.

    By default captures the visible screen. Use --lines N to include
    scrollback history (useful for seeing earlier output or error traces).

    Local agents:   captured directly from tmux/wezterm (sub-second).
    Remote agents:  captured via the control plane on the next heartbeat (~5-15s).

    Common use cases:

      agp peek claude-dev              # what is it doing right now?
      agp peek claude-dev -n 200       # show last 200 lines of scrollback
      agp peek codex-reviewer          # inspect a remote agent on another server
      agp peek claude-dev --timeout 5  # fail fast if agent is slow to respond

    Use peek when:
      - A job is running long and you want to see progress
      - A job failed and you want to see the agent's terminal state
      - You want to verify an agent is actually working, not stuck
      - You need to debug a remote agent without SSH access
    """
    import time as _time
    import httpx as _httpx

    # Fast path: try local capture first
    local_text = _try_local_peek(agent_id, lines=lines)
    if local_text is not None:
        # Show agent status header so users know if output is live or stale
        agent_status = _peek_agent_status(agent_id, server_url)
        if agent_status:
            typer.echo(f"[{agent_id} — {agent_status}]", err=True)
        typer.echo(local_text, nl=False)
        return

    # Remote path: request via CP
    with _cli_client(server_url) as client:
        try:
            req = client.request_peek(agent_id, lines=lines)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        request_id = req["request_id"]
        runtime_id = req.get("runtime_id", "?")
        typer.echo(f"[..] Peek requested for {agent_id} (runtime={runtime_id}). Waiting for heartbeat...", err=True)

        start = _time.monotonic()
        while _time.monotonic() - start < timeout:
            elapsed = int(_time.monotonic() - start)
            try:
                result = client.get_peek_result(agent_id, request_id)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

            if result.get("status") == "ready":
                typer.echo(result["text"], nl=False)
                return

            typer.echo(f"\r[..] Waiting for heartbeat... ({elapsed}s)", err=True, nl=False)
            _time.sleep(1.0)

        typer.echo("", err=True)  # newline after progress
        typer.echo(
            f"Timed out after {int(timeout)}s waiting for peek result. "
            "The runtime may be offline or slow to heartbeat.",
            err=True,
        )
        raise typer.Exit(1)


# ── 3. status ────────────────────────────────────────────────────────


@app.command()
def status(
    target: str = typer.Argument(None, help="Job ID or agent ID (optional)."),
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Minimal output — just agent lines."),
) -> None:
    """System dashboard, or job/agent status.

    With no arguments: combined health + agent overview (replaces ``health`` and ``ls``).
    With a job ID: shows full job details + artifacts.
    With an agent ID: shows agent status, heartbeat, and current job.
    """
    if target is None:
        _status_dashboard(server_url, output_json=output_json, quiet=quiet)
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


def _status_dashboard(server_url: str | None, *, output_json: bool = False, quiet: bool = False) -> None:
    """Combined system dashboard — health, runtimes, agents, queue."""
    import httpx as _httpx
    from datetime import datetime, timezone

    with _make_client(server_url) as client:
        try:
            cp_health = client.health()
        except (_httpx.RequestError, _httpx.HTTPStatusError) as exc:
            typer.echo(f"Control plane unreachable: {exc}", err=True)
            raise typer.Exit(1)

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
            rt_page = client.ops_list_runtimes(limit=200)
            runtimes = rt_page.get("items", [])
        except Exception:
            pass

        # Filter synthetic rtm_ runtimes (created by agent_up, no backing process)
        runtimes = [rt for rt in runtimes if not rt.get("runtime_id", "").startswith("rtm_")]

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
            except Exception:
                pass

        if output_json:
            typer.echo(json.dumps({
                "control_plane": cp_health,
                "ops": ops,
                "agents": agents,
                "runtimes": runtimes,
            }, indent=2, default=str))
            return

        if quiet:
            # Minimal output — just agent lines (matches ls -q)
            if not agents:
                typer.echo("(none)")
                return
            now = datetime.now(timezone.utc)
            for agent in agents:
                aid = agent.get("agent_id", "?")
                agent_status = agent.get("status", "?").upper()
                role = ", ".join(agent.get("capabilities", [])) or "-"
                qdepth = int(agent.get("queue_depth", 0) or 0)
                job = agent_jobs.get(aid)
                parts = [f"{aid:<20s}", agent_status]
                if role != "-":
                    parts.append(f"caps=[{role}]")
                if job:
                    parts.append(f"job={job['job_id']}")
                    try:
                        created = datetime.fromisoformat(job["created_at"])
                        elapsed = (now - created).total_seconds()
                        parts.append(f"({_format_duration(elapsed)})")
                    except Exception:
                        pass
                if qdepth > 0:
                    parts.append(f"queue={qdepth}")
                typer.echo("  ".join(parts))
            return

        # ── Control plane health
        cp_data = cp_health.get("data", cp_health)
        cp_status = cp_data.get("status", "unknown")
        typer.echo(f"Control Plane: {cp_status}")
        for k, v in cp_data.get("components", {}).items():
            typer.echo(f"  {k}: {v}")

        # ── Runtimes
        typer.echo(f"\nRuntimes: {len(runtimes)}")
        for rt in runtimes:
            rid = rt.get("runtime_id", "?")
            hb_age = rt.get("heartbeat_age_seconds")
            if hb_age is None:
                hb_age = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
            hb_str = f"{hb_age:.0f}s ago" if hb_age is not None else "never"
            bound_aid = rt.get("agent_id")
            if bound_aid:
                agents_bound = bound_aid
            else:
                agents_bound = ", ".join(
                    sorted({w.get("agent_id", "?") for w in rt.get("claimed_work", [])})
                ) or "none"
            typer.echo(f"  {rid}  heartbeat={hb_str}  agents=[{agents_bound}]")

        # ── Agents
        now = datetime.now(timezone.utc)
        typer.echo(f"\nAgents: {len(agents)}")
        for agent in agents:
            aid = agent.get("agent_id", "?")
            state = agent.get("status", "unknown")
            caps = ", ".join(agent.get("capabilities", []))
            qdepth = int(agent.get("queue_depth", 0) or 0)
            job = agent_jobs.get(aid)
            parts = [f"  {aid}  status={state}"]
            if caps:
                parts.append(f"caps=[{caps}]")
            if job:
                job_id = job["job_id"]
                try:
                    created = datetime.fromisoformat(job["created_at"])
                    elapsed = (now - created).total_seconds()
                    parts.append(f"job={job_id} ({_format_duration(elapsed)})")
                except Exception:
                    parts.append(f"job={job_id}")
            if qdepth > 0:
                parts.append(f"queue={qdepth}")
            typer.echo("  ".join(parts))

        # ── Queue summary
        if ops:
            queue = ops.get("queue") or {}
            depth = int(queue.get("depth") or 0)
            if depth > 0:
                typer.echo(f"\nQueue depth: {depth}")

        # ── Warnings for agents with queued work but no runtime
        agent_runtime: dict[str, str] = {}
        runtime_health: dict[str, tuple[str, str]] = {}
        for rt in runtimes:
            rid = rt.get("runtime_id", "")
            runtime_health[rid] = (
                str(rt.get("status") or "-").lower(),
                str(rt.get("health_status") or "-").lower(),
            )
            aid = rt.get("agent_id")
            if aid:
                agent_runtime[aid] = rid
        warning_items: list[str] = []
        for agent in agents:
            aid = agent.get("agent_id", "?")
            qdepth = int(agent.get("queue_depth", 0) or 0)
            if qdepth <= 0:
                continue
            bound_rt = agent_runtime.get(aid)
            if not bound_rt:
                warning_items.append(
                    f"- {aid}: {qdepth} queued, no runtime bound. Start or re-register its runtime."
                )
            elif bound_rt in runtime_health:
                rs, hs = runtime_health[bound_rt]
                if rs in {"degraded", "offline"} or hs in {"degraded", "unreachable"}:
                    warning_items.append(
                        f"- {aid}: {qdepth} queued, runtime {bound_rt} heartbeat stale ({hs if hs != '-' else rs}). Restart that runtime."
                    )
        if warning_items:
            typer.echo("\n[WARNINGS]")
            for item in warning_items:
                typer.echo(item)


def _status_job(job_id: str, server_url: str | None) -> None:
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        _status_job_from_data(job, client)


_REVIEW_OUTPUT_CONTRACT: dict = {
    "format": "json",
    "json_schema": {
        "type": "object",
        "required": ["verdict", "summary", "findings"],
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "description", "file", "line"],
                    "additionalProperties": False,
                    "properties": {
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "description": {"type": "string"},
                        "file": {"type": ["string", "null"]},
                        "line": {"type": ["integer", "null"]},
                    },
                },
            },
        },
    },
}

_HEARTBEAT_STATE_LABELS = {
    "gate.auto": "dismissing prompt",
    "gate.fatal": "blocked: requires login",
    "ready": "idle",
    "completed": "finishing up",
    "working": "working",
}


def _heartbeat_activity_hint(*, tui_state: str, last_line: str, output_chars: int | None) -> str:
    """Return the user-facing progress hint for a runtime heartbeat."""
    if last_line:
        return last_line[:60]
    label = _HEARTBEAT_STATE_LABELS.get(tui_state, tui_state) if tui_state else ""
    if label and label != "unknown":
        return label
    if output_chars:
        return f"{output_chars:,} chars output"
    return ""


def _status_show_heartbeat(job_id: str, client) -> None:
    """Show the latest progress heartbeat for a running job."""
    try:
        events_data = client.get_job_events(job_id, limit=200)
        items = events_data.get("items") or []
        for ev in reversed(items):
            body = ev.get("body") or {}
            if body.get("message") != "runtime.progress_heartbeat":
                continue
            details = body.get("details") or {}
            tui_state = (details.get("tui_state") or "").strip()
            last_line = (details.get("last_line") or "").strip()
            output_chars = details.get("output_chars")
            activity = _heartbeat_activity_hint(
                tui_state=tui_state,
                last_line=last_line,
                output_chars=output_chars,
            )
            if activity:
                typer.echo(f"ACTIVITY:     {activity}")
            # Show heartbeat freshness
            created_at = ev.get("created_at", "")
            if created_at:
                ev_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ev_time).total_seconds()
                if age > 30:
                    typer.echo(f"LAST_SEEN:    {int(age)}s ago (possibly stalled)")
                else:
                    typer.echo(f"LAST_SEEN:    {int(age)}s ago")
            return
    except Exception:
        pass


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
        _status_show_heartbeat(job_id, client)
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
        agent_id = job.get("target_agent_id", "")
        if agent_id:
            typer.echo(f"Tip: inspect the agent's terminal:  agp peek {agent_id}")


# ── 4. jobs ──────────────────────────────────────────────────────────


@app.command()
def jobs(
    server_url: str = typer.Option(None, help="CP URL."),
    limit: int = typer.Option(10, help="Max jobs to show."),
    agent: str = typer.Option(None, "--agent", help="Filter by agent ID."),
    filter_status: str = typer.Option(None, "--status", help="Filter by status (queued, running, completed, failed)."),
) -> None:
    """List recent jobs."""
    from datetime import datetime, timezone
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
        # For failed jobs, fetch the failure reason from events so
        # operators can distinguish infra failures from agent errors.
        failure_reasons: dict[str, str] = {}
        for j in items:
            if j["status"] != "failed":
                continue
            try:
                events_data = client.get_job_events(j["job_id"], limit=50)
                for ev in reversed(events_data.get("items", [])):
                    body = ev.get("body") or {}
                    if ev.get("event_type") == "run.failed":
                        summary = body.get("summary") or {}
                        exc_type = summary.get("exception_type", "")
                        if exc_type:
                            failure_reasons[j["job_id"]] = exc_type
                        break
            except Exception:  # noqa: BLE001
                pass
        now = datetime.now(timezone.utc)
        for j in items:
            retry = f" retry={j['retry_count']}/{j['max_retries']}" if j.get("retry_count", 0) > 0 else ""
            status_str = j["status"]
            reason = failure_reasons.get(j["job_id"])
            if reason:
                status_str = f"failed:{reason}"
            time_info = ""
            try:
                created_raw = j["created_at"]
                created = (
                    created_raw if isinstance(created_raw, datetime)
                    else datetime.fromisoformat(str(created_raw))
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_str = _format_duration((now - created).total_seconds()) + " ago"
                elapsed_str = ""
                updated_raw = j.get("updated_at")
                if updated_raw and j["status"] in ("completed", "failed"):
                    updated = (
                        updated_raw if isinstance(updated_raw, datetime)
                        else datetime.fromisoformat(str(updated_raw))
                    )
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    elapsed_str = _format_duration((updated - created).total_seconds())
                time_info = age_str
                if elapsed_str:
                    time_info += f"  took {elapsed_str}"
            except Exception:
                pass
            typer.echo(
                f"  {j['job_id']}  {status_str:<20s}  agent={j.get('target_agent_id', '?'):<14s}  {time_info}{retry}"
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
    role: str = typer.Option(None, "--role", help="Artifact role to fetch (default: transcript_log > result > exec_log, or result-first for jobs with output contracts)."),
) -> None:
    """Dump the clean output of a completed job.

    Fetches the transcript (or result artifact) and prints it to stdout
    with no envelope or plumbing.  Useful for piping agent output into
    other tools.

    Jobs with output contracts (e.g. --review) prefer the result artifact
    over transcript, since the result contains the structured output.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            arts = client.list_job_artifacts(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        items = arts.get("items", [])

        # Check job metadata to adjust artifact preference.
        job_data = None
        has_output_contract = False
        job_failed = False
        if not role:
            try:
                job_data = client.get_job(job_id)
                has_output_contract = bool(job_data.get("output_contract_json"))
                job_failed = job_data.get("status") == "failed"
            except Exception:
                pass

        if role:
            candidates = [a for a in items if a.get("role") == role]
        elif job_failed:
            # Failed job: show the failure reason first, fall back to transcript.
            # This applies to both contract and non-contract jobs.
            candidates = (
                [a for a in items if a.get("role") == "failure_evidence"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        elif has_output_contract:
            # Successful contract job: result first (structured), then transcript
            candidates = (
                [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        else:
            # Default: result (clean extracted answer) > transcript_log > exec_log
            candidates = (
                [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        if not candidates:
            job_status = str((job_data or {}).get("status") or "").strip().lower()
            if job_status in {"cancelled", "interrupt_requested"}:
                typer.echo("Job was cancelled/interrupted before a result was captured.", err=True)
            typer.echo(f"No output artifact found for job {job_id}", err=True)
            available = [a.get("role") for a in items]
            if available:
                typer.echo(f"Available roles: {', '.join(available)}", err=True)
            raise typer.Exit(1)
        art = candidates[-1]  # latest
        if job_failed and not role:
            art_role = art.get("role", "artifact")
            if has_output_contract:
                typer.echo(f"WARNING: Job failed (output contract violation). Showing {art_role}.", err=True)
            else:
                typer.echo(f"WARNING: Job failed. Showing {art_role}.", err=True)
            typer.echo("  Tip: use --role to select a specific artifact (e.g. --role transcript_log).", err=True)
            target_agent = (job_data or {}).get("target_agent_id", "")
            if target_agent:
                typer.echo(f"  Tip: inspect the agent's terminal:  agp peek {target_agent}", err=True)
            typer.echo("---", err=True)
        try:
            data = client.fetch_artifact(art["artifact_id"], content=True)
            typer.echo(data.get("content") or "(no content)")
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)


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
    target: str = typer.Argument(..., help="Agent ID, runtime ID, or capability name/ID."),
    server_url: str = typer.Option(None, help="CP URL."),
    diagnose: bool = typer.Option(False, "--diagnose", "-d", help="Include diagnostic details (runtime logs, registration)."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Deep-dive context for an agent, runtime, or capability.

    Accepts an agent ID (e.g. agt_local), runtime ID (e.g. rtm_abc),
    or capability ID/name (e.g. cap_python).

    Use --diagnose to include runtime logs and registration details.
    """
    from datetime import datetime, timezone
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Runtime detection: if target starts with "rtm_" or "rtm-", try runtime first
        if target.startswith("rtm_") or target.startswith("rtm-"):
            _info_runtime(target, client, output_json=output_json, diagnose=diagnose)
            return

        # Try agent first, fall back to capability
        agent = None
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if agent is not None:
            if output_json:
                _info_agent_json(agent, client, diagnose=diagnose)
            else:
                _info_agent(agent, client)
                if diagnose:
                    _info_agent_diagnose(agent, client)
        else:
            _info_capability(target, client, output_json=output_json)


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


def _info_capability(target: str, client, *, output_json: bool = False) -> None:
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

    if output_json:
        typer.echo(json.dumps(cap, indent=2, default=str))
        return

    cap_name = cap.get("name", cap.get("capability_id", target))
    typer.echo(_SEPARATOR)
    typer.echo(f"      CAPABILITY INFO: {cap_name}")
    typer.echo(_SEPARATOR)
    _print_capability_blueprint(cap)


def _info_agent_diagnose(agent: dict, client) -> None:
    """Print diagnostic details for an agent (runtime binding, logs, registration)."""
    agent_id = agent["agent_id"]

    # Runtime binding details
    rt = None
    try:
        rt_page = client.ops_list_runtimes(limit=200)
        bound_rts = [
            r for r in rt_page.get("items", [])
            if r.get("agent_id") == agent_id
        ]
        if bound_rts:
            rt = bound_rts[0]
    except Exception:
        pass

    typer.echo(f"\n--- DIAGNOSTICS ---")
    typer.echo(f"REGISTERED:   {agent.get('created_at', '?')}")

    if rt:
        typer.echo(f"\nRuntime Binding:")
        typer.echo(f"  runtime_id: {rt.get('runtime_id', '?')}")
        typer.echo(f"  status:     {rt.get('status', '?')}")
        typer.echo(f"  host:       {rt.get('hostname', '?')}")

        # Recent runtime logs
        runtime_id = rt.get("runtime_id")
        if runtime_id:
            try:
                logs = client.logs_runtime(runtime_id, limit=20)
                entries = logs.get("entries", logs) if isinstance(logs, dict) else logs
                if isinstance(entries, list) and entries:
                    entries = entries[-10:]
                    typer.echo(f"\n  Recent Logs (last {len(entries)}):")
                    for entry in entries:
                        if isinstance(entry, dict):
                            ts = entry.get("created_at", "?")
                            action = entry.get("action", entry.get("kind", "?"))
                            typer.echo(f"    [{ts}] {action}")
                        else:
                            typer.echo(f"    {str(entry)[:120]}")
            except Exception as logs_exc:
                typer.echo(f"  [warn] Failed to fetch runtime logs: {logs_exc}", err=True)
    else:
        typer.echo("Runtime Binding: none")

    # Extended job history
    try:
        jobs_data = client.list_jobs(target_agent_id=agent_id, limit=10)
        jobs = jobs_data.get("items", [])
        if jobs:
            typer.echo(f"\nJob History ({len(jobs)}):")
            for j in jobs:
                typer.echo(f"  {j.get('job_id', '?')}  status={j.get('status', '?')}  created={j.get('created_at', '?')}")
        else:
            typer.echo(f"\nJob History: none")
    except Exception:
        pass


def _info_agent_json(agent: dict, client, *, diagnose: bool = False) -> None:
    """Output agent info as JSON."""
    agent_id = agent["agent_id"]
    payload: dict = {"agent": agent}

    if diagnose:
        # Runtime binding
        try:
            rt_page = client.ops_list_runtimes(limit=200)
            bound_rts = [
                r for r in rt_page.get("items", [])
                if r.get("agent_id") == agent_id
            ]
            payload["runtime"] = bound_rts[0] if bound_rts else None
        except Exception:
            payload["runtime"] = None

        # Recent jobs
        try:
            jobs_data = client.list_jobs(target_agent_id=agent_id, limit=10)
            payload["recent_jobs"] = jobs_data.get("items", [])
        except Exception:
            payload["recent_jobs"] = []

    typer.echo(json.dumps(payload, indent=2, default=str))


def _info_runtime(runtime_id: str, client, *, output_json: bool = False, diagnose: bool = False) -> None:
    """Show runtime info — reuses diagnose runtime rendering."""
    import httpx as _httpx
    try:
        rt = client.ops_get_runtime(runtime_id)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            typer.echo(f"Runtime '{runtime_id}' not found.", err=True)
            raise typer.Exit(1)
        raise

    payload: dict = {
        "runtime": rt,
        "agents": rt.get("agents", []),
    }

    if diagnose:
        payload["recent_logs"] = []
        try:
            logs = client.logs_runtime(runtime_id, limit=20)
            payload["recent_logs"] = logs.get("entries", logs) if isinstance(logs, dict) else logs
        except Exception as logs_exc:
            typer.echo(f"[warn] Failed to fetch runtime logs: {logs_exc}", err=True)

    if output_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"Runtime: {runtime_id}")
    typer.echo(f"  status:     {rt.get('status', '?')}")
    hb = rt.get("heartbeat_age_seconds")
    if hb is None:
        hb = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
    typer.echo(f"  heartbeat:  {f'{hb:.0f}s ago' if hb is not None else 'never'}")
    typer.echo(f"  host:       {rt.get('hostname', '?')}")
    typer.echo(f"  registered: {rt.get('created_at', '?')}")

    if payload["agents"]:
        typer.echo(f"\n  Bound Agents:")
        for a in payload["agents"]:
            caps = ", ".join(a.get("capabilities", []))
            typer.echo(f"    {a['agent_id']}  status={a['status']}  caps=[{caps}]")
    else:
        typer.echo(f"\n  Bound Agents: none")

    if diagnose:
        logs = payload.get("recent_logs", [])
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


# ── 7. nudge ─────────────────────────────────────────────────────────


def _format_human_nudge(message: str) -> str:
    return message


@app.command()
def nudge(
    target: str = typer.Argument(..., help="Target agent ID."),
    message: str = typer.Argument(..., help="Message to inject into the agent's terminal."),
) -> None:
    """Inject a message directly into an agent's terminal — instantly.

    Writes the message to a via-file and types the reference string
    into the agent's terminal pane.  No queue, no heartbeat, no wait.
    The agent sees the message on its next input read.

    Works with both tmux and wezterm sessions (local only).

    Examples:

      agp nudge claude-dev "stop and focus on the login bug instead"
      agp nudge orchestrator "the deadline moved to Friday"
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    payload = _format_human_nudge(message)

    # Write nudge to a plain file — NOT write_via_file (which adds
    # BEGIN TASK / END TASK markers and a task-style reference string).
    # Nudges are system reminders, not tasks.  The original so.send()
    # poll must not see nudge markers in the terminal output.
    nudge_dir = Path("/tmp/smallops")
    nudge_dir.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="nudge-", suffix=".md", dir=str(nudge_dir))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    ref = f"Read the file {tmp} and follow the instructions inside."
    session_name = f"agp-{target}"

    # Try tmux
    try:
        check = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=3,
        )
        if check.returncode == 0:
            # Match smallops send_text: -l (literal) text, brief pause, then Enter
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", ref],
                check=True, capture_output=True, timeout=3,
            )
            time.sleep(0.05)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                check=True, capture_output=True, timeout=3,
            )
            typer.echo(f"nudge sent to {target}")
            return
    except FileNotFoundError:
        pass

    # Try wezterm
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            marker = f"SMALLOPS:{target}"
            for pane in _json.loads(result.stdout):
                if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                    pane_id = str(pane.get("pane_id"))
                    subprocess.run(
                        ["wezterm", "cli", "send-text", "--pane-id", pane_id, "--no-paste", ref + "\n"],
                        check=True, capture_output=True, timeout=3,
                    )
                    typer.echo(f"nudge sent to {target}")
                    return
    except FileNotFoundError:
        pass

    typer.echo(f"No local session found for {target} (looked for '{session_name}').", err=True)
    raise typer.Exit(1)



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
