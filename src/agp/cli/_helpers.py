"""Shared CLI helpers — formatting, client setup, polling, output."""

from __future__ import annotations

import json
import os
import re as _re
import time
import uuid
from datetime import UTC, datetime
from difflib import get_close_matches
from pathlib import Path

import typer

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

_SEPARATOR = "========================================="

_REVIEW_OUTPUT_CONTRACT: dict = {
    "format": "json",
    "json_schema": {
        "type": "object",
        "required": ["verdict", "summary", "findings"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "description"],
                    "properties": {
                        "severity": {"type": "string"},
                        "description": {"type": "string"},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
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
    from datetime import datetime
    try:
        hb_dt = datetime.fromisoformat(str(iso_timestamp).replace("Z", "+00:00"))
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - hb_dt).total_seconds()
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
    typer.echo("STATUS:       IN_PROGRESS")
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
    from datetime import datetime

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
                        if (datetime.now(UTC) - ev_time).total_seconds() > 30:
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
                age = (datetime.now(UTC) - ev_time).total_seconds()
                if age > 30:
                    typer.echo(f"LAST_SEEN:    {int(age)}s ago (possibly stalled)")
                else:
                    typer.echo(f"LAST_SEEN:    {int(age)}s ago")
            return
    except Exception:
        pass


def _format_duration(seconds: float) -> str:
    """Format seconds into Xm:XXs or Xh:XXm."""
    if seconds < 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h:{minutes:02d}m"
    return f"{minutes:02d}m:{secs:02d}s"

