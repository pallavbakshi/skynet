"""Send and reply commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from agp.cli import app
from agp.cli._helpers import (
    _REVIEW_OUTPUT_CONTRACT,
    _cli_client,
    _cli_idempotency_key,
    _format_http_error,
    _parse_attachment_option,
    _poll_until_done,
    _print_detached,
    _print_job_result,
    _print_peek_tip,
    _reject_suspicious_task_options,
    _validate_send_reply_target,
)


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


