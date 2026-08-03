"""Review loop commands — review, review-status, review-diagnose."""

from __future__ import annotations

import json
import uuid
from datetime import UTC

import typer

from agp.cli import app
from agp.cli._helpers import (
    _REVIEW_OUTPUT_CONTRACT,
    _cli_client,
    _extract_trailing_json_payload,
    _format_http_error,
    _heartbeat_age_seconds,
    _poll_until_done,
    _print_banner,
    _print_job_result,
    _print_peek_tip,
    _strip_tui_action_traces,
)


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
    from datetime import datetime

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
        "updated_at": datetime.now(UTC).isoformat(),
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
                        from datetime import datetime
                        _save_review_state(client, {**state, "updated_at": datetime.now(UTC).isoformat()})
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
            typer.echo("\n  Reviewer Runtime:")
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


