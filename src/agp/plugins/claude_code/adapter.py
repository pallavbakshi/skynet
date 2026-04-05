"""Claude Code CLI agent adapter — orchestration (poll loop, bootstrap, recovery).

All TUI parsing and classification is delegated to sibling modules
(_classify, _parse, _gates, _normalize, _json_extract, _metadata).
"""
from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from agp.plugins.claude_code._classify import (
    ends_with_prompt as _ends_with_prompt,
    is_completed_turn as _is_completed_turn,
    is_ready as _is_ready,
    is_shell_returned as _is_shell_returned,
    is_working as _is_working,
)
from agp.plugins.claude_code._gates import (
    GateKind,
    classify_gate as _classify_gate,
    gate_response as _gate_response,
)
from agp.plugins.claude_code._markers import PROMPT_PREFIX
from agp.plugins.claude_code._normalize import (
    normalize_screen as _normalize_screen,
    screen_tail as _screen_tail,
)
from agp.plugins.claude_code._parse import (
    extract_last_response,
    parse_turns,
)
from agp.plugins._via_file import (
    build_task_file_content,
    cleanup_task_file,
    reference_string,
    write_task_file,
)
from agp.plugins._output_contracts import (
    apply_output_contract_instruction,
    is_json_contract,
    prompt_for_claim,
    result_file_path_for_run,
    validate_json_against_contract,
)
from agp.plugins._structured_output import select_structured_result
from agp.plugins._provider_env import collect_provider_env
from agp.runtime import (
    AdapterExecutionFailed, AgentAdapter, ArtifactPayload, ExecutionResult,
    AuthFailure, BootstrapFailure, ExecutionTimeout, PaneDied,
    RecoverableExecutionError, StableButIndeterminate, TerminalHost,
    TerminalSession, _strip_ansi,
)

_logger = logging.getLogger(__name__)


# ── Convenience helpers (also re-exported from __init__ for compat) ──


def _clean_claude_code_output(text: str) -> str:
    """Extract the last Claude Code response from raw TUI output."""
    return extract_last_response(_strip_ansi(text))


def _parse_claude_code_turns(text: str) -> list[dict[str, Any]]:
    """Parse visible Claude Code TUI output into prompt/response turns.

    Returns list[dict] with "prompt" and "response" keys for backward
    compatibility.
    """
    turns = parse_turns(_strip_ansi(text))
    return [
        {"prompt": t.prompt, "response": t.response_lines}
        for t in turns
    ]


# ── Adapter ──────────────────────────────────────────────────────────


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(
        self,
        *,
        cli_command: str = "claude",
        idle_poll_seconds: float = 2.0,
        idle_after: int = 3,
        idle_timeout_seconds: float = 0.0,
        session_mode: str = "ephemeral",
        bootstrap_settle_seconds: float = 0.0,
    ) -> None:
        self.cli_command = cli_command
        self.idle_poll_seconds = idle_poll_seconds
        self.idle_after = idle_after
        self.idle_timeout_seconds = idle_timeout_seconds
        self.session_mode = session_mode
        self.bootstrap_settle_seconds = bootstrap_settle_seconds

    @property
    def kind(self) -> str:
        return "claude_code"

    # ── TUI detection helpers (public for tests) ─────────────────────

    @staticmethod
    def _looks_like_ready(text: str) -> bool:
        return _is_ready(text)

    @staticmethod
    def _visible_ends_with_prompt(text: str) -> bool:
        return _ends_with_prompt(text)

    @staticmethod
    def _looks_like_working(text: str) -> bool:
        return _is_working(text)

    @staticmethod
    def _screen_tail(text: str, n: int = 10) -> str:
        return _screen_tail(text, n)

    @staticmethod
    def _looks_like_shell_returned(text: str) -> bool:
        return _is_shell_returned(text)

    @staticmethod
    def _looks_like_gate_prompt(text: str) -> bool:
        return _classify_gate(text) != GateKind.NONE

    @staticmethod
    def _is_fatal_gate(text: str) -> bool:
        return _classify_gate(text) == GateKind.FATAL

    @staticmethod
    def _gate_response(text: str) -> str:
        return _gate_response(text)

    @staticmethod
    def _normalise_visible_screen(raw: str) -> str:
        return _normalize_screen(raw)

    def _looks_like_completed_turn(
        self,
        text: str,
        *,
        baseline_answered_turns: int,
        baseline_last_response: str | None,
    ) -> bool:
        return _is_completed_turn(
            text,
            baseline_answered_turns=baseline_answered_turns,
            baseline_last_response=baseline_last_response,
        )

    @staticmethod
    def _resolve_attachment_paths(
        session: TerminalSession, claimed: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Enrich attachment metadata with staged file paths (if available)."""
        items = claimed.get("job_attachments") or []
        if not items:
            return None
        from agp.runtime._attachments import staged_attachment_relative_path
        from urllib.parse import unquote, urlparse
        workspace = session.workspace_ref or ""
        # Normalize file:// URIs the same way the supervisor does
        if "://" in workspace:
            parsed = urlparse(workspace)
            if parsed.scheme == "file":
                workspace = unquote(parsed.path)
        enriched = []
        for item in items:
            entry = dict(item)
            # Try to resolve the staged path from workspace
            name = str(item.get("name", ""))
            artifact_id = str(item.get("artifact_id", ""))
            if workspace and name and artifact_id:
                rel = staged_attachment_relative_path(artifact_id=artifact_id, name=name)
                candidate = Path(workspace) / rel
                if candidate.exists():
                    entry["staged_path"] = str(candidate)
            enriched.append(entry)
        return enriched if enriched else None

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        cleaned = _clean_claude_code_output(text)
        screen = _strip_ansi(text)
        return {
            "adapter_kind": self.kind,
            "mode": "tui",
            "run_id": run_id,
            "cleaned_output": cleaned,
            "looks_like_ready": _is_ready(screen),
            "looks_like_gate_prompt": _classify_gate(screen) != GateKind.NONE,
            "looks_like_shell_returned": _is_shell_returned(screen),
            "supported": True,
        }

    def ensure_bootstrapped(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
    ) -> bool:  # noqa: ARG002
        """Ensure the Claude Code TUI is ready.

        Returns True when the adapter reused an already-running foreground TUI.
        Returns False when it had to launch or re-bootstrap the session.
        """
        if session.metadata.get("claude_code_bootstrapped"):
            if hasattr(host, "is_foreground_tui"):
                if not host.is_foreground_tui(session):
                    session.metadata.pop("claude_code_bootstrapped", None)
                else:
                    return True
            elif host.kind == "tmux":
                session.metadata.pop("claude_code_bootstrapped", None)
            else:
                return True

        health = host.health(session)
        if not health.healthy:
            raise BootstrapFailure(f"session unhealthy before bootstrap: {health.reason}")

        if self.session_mode == "sticky" and hasattr(host, "is_foreground_tui") and host.is_foreground_tui(session):
            session.metadata["claude_code_bootstrapped"] = True
            return True

        host.launch_command(
            session,
            command=f"{self.cli_command} --dangerously-skip-permissions",
            env=collect_provider_env(),
            cwd=session.workspace_ref,
        )

        deadline = monotonic() + (self.idle_timeout_seconds if self.idle_timeout_seconds is not None else 60.0)
        gate_dismissals = 0
        max_gate_dismissals = 10
        while monotonic() < deadline:
            sleep(self.idle_poll_seconds)
            screen = _strip_ansi(host.read_visible(session))
            gate_kind = _classify_gate(screen)
            if gate_kind != GateKind.NONE:
                if gate_kind == GateKind.FATAL:
                    raise AuthFailure(
                        "claude code requires interactive login — complete OAuth "
                        "setup in the container and re-commit the image"
                    )
                host.send_text(session, _gate_response(screen), enter=True)
                gate_dismissals += 1
                if gate_dismissals <= max_gate_dismissals:
                    deadline = max(deadline, monotonic() + 30.0)
                continue
            if _is_ready(screen):
                break
        else:
            raise BootstrapFailure("claude code did not become ready after launch")

        if self.bootstrap_settle_seconds > 0:
            sleep(self.bootstrap_settle_seconds)
            health = host.health(session)
            if not health.healthy:
                raise BootstrapFailure(f"session unhealthy after bootstrap: {health.reason}")

        session.metadata["claude_code_bootstrapped"] = True
        return False

    # ── Execution paths ──────────────────────────────────────────────

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        run_id = claimed["run"]["run_id"]
        try:
            return self._execute_run_tui(host=host, session=session, claimed=claimed, supervisor=supervisor)
        finally:
            # Clean up the via-file task file on all exit paths (success,
            # timeout, pane death, indeterminate, etc.)
            cleanup_task_file(run_id)

    def _execute_run_tui(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        """TUI mode: send prompt at ❯ -> wait for idle -> parse output."""
        contract = (claimed.get("job") or {}).get("output_contract_json")
        json_contract = is_json_contract(contract)
        result_file = result_file_path_for_run(claimed["run"]["run_id"]) if json_contract else None
        if result_file:
            try:
                Path(result_file).unlink(missing_ok=True)
            except OSError as exc:
                _logger.warning("failed to clean result file %s: %s", result_file, exc)
        prompt = apply_output_contract_instruction(
            prompt=claimed["message"]["text"],
            claimed=claimed,
            result_file_path=result_file,
        )
        run_id = claimed["run"]["run_id"]

        if self.session_mode == "ephemeral":
            session = host.reset_session(session)
            session.metadata.pop('restored_cursor', None)
        reused_existing_tui = self.ensure_bootstrapped(host=host, session=session, claimed=claimed)

        baseline_turns: list[dict[str, Any]] = []
        baseline_last_response = None
        if reused_existing_tui:
            baseline_screen = _strip_ansi(host.read_visible(session))
            baseline_turns = [t for t in _parse_claude_code_turns(baseline_screen) if t["response"]]
            if baseline_turns:
                baseline_last_response = "\n".join(baseline_turns[-1]["response"]).strip()

        health = host.health(session)
        if not health.healthy:
            raise PaneDied(f"session unhealthy at dispatch: {health.reason}")

        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        startup_settled_event = session.metadata.get("startup_settled_event") or getattr(
            supervisor, "_active_startup_settled", None,
        )
        lock = getattr(supervisor, "_session_lock", None)
        if lock is not None:
            with lock:
                supervisor._active_session = session
        else:
            supervisor._active_session = session
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={"adapter": self.kind, "session_id": session.session_id, "run_id": run_id},
        )

        # Via-file delivery: write the full prompt + metadata to a temp file
        # and send a short reference string to the TUI. This avoids paste
        # buffer corruption, size limits, and special character mangling.
        task_file_content = build_task_file_content(
            prompt=prompt,
            claimed=claimed,
            attachments=self._resolve_attachment_paths(session, claimed),
        )
        task_file_path = write_task_file(run_id=run_id, content=task_file_content)
        dispatch_text = reference_string(task_file_path)
        host.send_text(session, dispatch_text, enter=True)
        _dbg = getattr(supervisor, "debug_log", None) or (lambda entry: None)
        _dbg({"kind": "adapter_dispatch", "run_id": run_id,
              "action": "via_file_dispatch",
              "task_file": task_file_path,
              "task_file_size": len(task_file_content),
              "dispatch_text": dispatch_text})
        _dbg_ctx = {"kind": "adapter_poll", "run_id": run_id, "session_id": session.session_id}

        def _poll_hook() -> None:
            supervisor.check_interrupt(claimed)

        timeout = self.idle_timeout_seconds if self.idle_timeout_seconds is not None else 180.0
        # Hard ceiling: the idle-timeout reset extends `deadline` when
        # output is flowing, but `absolute_deadline` never moves.
        absolute_deadline = monotonic() + min(timeout * 10, 3600.0)
        deadline = monotonic() + timeout
        prev_screen = ""
        prev_tail = ""
        unchanged = 0
        indeterminate_polls = 0
        tui_active = False
        working_logged = False
        dispatch_time = monotonic()
        last_breadcrumb_at = dispatch_time
        accumulated_turns_above_baseline = 0
        last_good_screen = ""
        last_heartbeat_at = dispatch_time
        heartbeat_interval = max(self.idle_poll_seconds, min(10.0, timeout / 4.0))

        while monotonic() < min(deadline, absolute_deadline):
            sleep(self.idle_poll_seconds)
            _poll_hook()
            screen = _strip_ansi(host.read_visible(session))
            read = host.read_output(session, cursor)
            cursor = read.cursor
            snap = _normalize_screen(screen)
            tail = _screen_tail(screen)
            changed = bool(read.changed or snap != prev_screen or tail != prev_tail)
            # Re-evaluate scrollback turn count when it was previously zero
            # or when output has changed (so we track multi-turn progress).
            # The accumulator-based read.full_text may miss ⏺ markers for TUI
            # apps, so this is a best-effort heuristic.
            if accumulated_turns_above_baseline == 0 or changed:
                answered_turns = [t for t in _parse_claude_code_turns(read.full_text) if t["response"]]
                new_count = max(0, len(answered_turns) - len(baseline_turns))
                if new_count > accumulated_turns_above_baseline:
                    accumulated_turns_above_baseline = new_count

            now = monotonic()
            if changed or now - last_heartbeat_at >= heartbeat_interval:
                output_chars = len(read.full_text)
                last_line = ""
                if read.text:
                    for ln in reversed(read.text.splitlines()):
                        stripped = _strip_ansi(ln).strip()
                        if stripped:
                            last_line = stripped[:80]
                            break
                supervisor.emit_progress(
                    claimed,
                    message="runtime.progress_heartbeat",
                    details={
                        "adapter": self.kind,
                        "session_id": session.session_id,
                        "run_id": run_id,
                        "stage": "tui",
                        "changed": changed,
                        "output_chars": output_chars,
                        "last_line": last_line,
                    },
                )
                last_heartbeat_at = now

            # Periodic breadcrumb every ~30s for long tasks
            if now - last_breadcrumb_at >= 30.0:
                _dbg({**_dbg_ctx, "action": "poll_state",
                      "elapsed": round(now - dispatch_time, 1),
                      "changed": changed, "unchanged": unchanged,
                      "tui_active": tui_active,
                      "accumulated_turns": accumulated_turns_above_baseline,
                      "output_chars": len(read.full_text)})
                last_breadcrumb_at = now

            startup_settled = tui_active
            if startup_settled_event is not None and startup_settled:
                startup_settled_event.set()
            if startup_settled and _is_shell_returned(screen):
                if accumulated_turns_above_baseline > 0:
                    _dbg({**_dbg_ctx, "action": "completed", "path": "shell_returned+scrollback_turns"})
                    break
                if last_good_screen and _is_completed_turn(
                    last_good_screen,
                    baseline_answered_turns=len(baseline_turns),
                    baseline_last_response=baseline_last_response,
                ):
                    _dbg({**_dbg_ctx, "action": "completed", "path": "shell_returned+last_good_screen"})
                    break
                if hasattr(host, "_get_pane_tty") and host._get_pane_tty(session) is not None and not host.shell_idle(session):
                    continue
                _dbg({**_dbg_ctx, "action": "pane_died",
                      "accumulated_turns": accumulated_turns_above_baseline,
                      "last_good_screen_len": len(last_good_screen),
                      "elapsed": round(monotonic() - dispatch_time, 1)})
                raise PaneDied("claude code cli exited during execution")

            gate_kind = _classify_gate(screen)
            if gate_kind != GateKind.NONE:
                if gate_kind == GateKind.FATAL:
                    raise AuthFailure(
                        "claude code requires interactive login — complete OAuth "
                        "setup in the container and re-commit the image"
                    )
                if snap != prev_screen:
                    response = _gate_response(screen)
                    _dbg({**_dbg_ctx, "action": "gate_dismiss", "gate_kind": gate_kind.name, "response": response})
                    host.send_text(session, response, enter=True)
                prev_screen = snap
                prev_tail = tail
                unchanged = 0
                tui_active = True
                # Extend deadline for gate dismissals — tool-use confirmations
                # eat poll cycles and shouldn't count against the idle timeout
                deadline = min(max(deadline, monotonic() + 30.0), absolute_deadline)
                continue

            if tail == prev_tail:
                unchanged += 1
            else:
                unchanged = 0
                indeterminate_polls = 0
                working_logged = False
                tui_active = True
                deadline = min(monotonic() + timeout, absolute_deadline)
            prev_screen = snap
            prev_tail = tail
            non_empty_lines = [ln for ln in screen.splitlines() if ln.strip()]
            if PROMPT_PREFIX in screen and len(non_empty_lines) > 1:
                last_good_screen = screen

            stable_after = max(1, self.idle_after - 1)
            if unchanged < stable_after:
                continue

            if _is_completed_turn(
                screen,
                baseline_answered_turns=len(baseline_turns),
                baseline_last_response=baseline_last_response,
            ):
                _dbg({**_dbg_ctx, "action": "completed", "path": "visible_completed_turn",
                      "elapsed": round(monotonic() - dispatch_time, 1)})
                break
            if _is_working(screen):
                if not working_logged:
                    _dbg({**_dbg_ctx, "action": "working_detected",
                          "elapsed": round(monotonic() - dispatch_time, 1)})
                    working_logged = True
                unchanged = 0
                indeterminate_polls = 0
                tui_active = True
                # Agent is visibly working — extend the soft deadline so
                # long-running tasks with a frozen spinner don't timeout.
                deadline = min(monotonic() + timeout, absolute_deadline)
                continue
            if accumulated_turns_above_baseline > 0 and _ends_with_prompt(screen):
                _dbg({**_dbg_ctx, "action": "completed", "path": "scrollback_turns+prompt",
                      "accumulated_turns": accumulated_turns_above_baseline,
                      "elapsed": round(monotonic() - dispatch_time, 1)})
                break
            if (
                tui_active
                and _ends_with_prompt(screen)
                and last_good_screen
                and _is_completed_turn(
                    last_good_screen,
                    baseline_answered_turns=len(baseline_turns),
                    baseline_last_response=baseline_last_response,
                )
            ):
                _dbg({**_dbg_ctx, "action": "completed", "path": "last_good_screen_fallback",
                      "elapsed": round(monotonic() - dispatch_time, 1)})
                break

            if (
                _ends_with_prompt(screen)
                and "[pasted text" in screen.lower()
                and "0 tokens" in screen.lower()
            ):
                _logger.warning("paste-not-submitted detected, re-sending Enter")
                _dbg({**_dbg_ctx, "action": "paste_not_submitted_resend",
                      "elapsed": round(monotonic() - dispatch_time, 1)})
                host.send_text(session, "", enter=True)
                unchanged = 0
                indeterminate_polls = 0
                continue

            indeterminate_polls += 1
            if indeterminate_polls == 1:
                blank = not screen.strip()
                turns = _parse_claude_code_turns(screen) if not blank else []
                answered = [t for t in turns if t["response"]]
                _logger.warning(
                    "indeterminate state entered: turns=%d answered=%d "
                    "baseline_turns=%d accumulated_scrollback=%d "
                    "tui_active=%s visible_prompt=%s blank=%s tail=%r",
                    len(turns), len(answered), len(baseline_turns),
                    accumulated_turns_above_baseline, tui_active,
                    _ends_with_prompt(screen), blank,
                    _screen_tail(screen)[-100:],
                )
                _dbg({**_dbg_ctx, "action": "indeterminate_entered",
                      "turns": len(turns), "answered": len(answered),
                      "tui_active": tui_active, "blank_screen": blank,
                      "elapsed": round(monotonic() - dispatch_time, 1)})
            if indeterminate_polls >= 5:
                _dbg({**_dbg_ctx, "action": "indeterminate_escalation",
                      "unchanged": unchanged, "tui_active": tui_active,
                      "accumulated_turns": accumulated_turns_above_baseline,
                      "ends_with_prompt": _ends_with_prompt(screen),
                      "elapsed": round(monotonic() - dispatch_time, 1),
                      "tail": _screen_tail(screen)[-200:]})
                raise StableButIndeterminate(
                    "screen is stable but adapter cannot determine if the "
                    "agent completed, is waiting for input, or is stuck",
                    screen=screen,
                    last_good_screen=last_good_screen,
                )
        else:
            _dbg({**_dbg_ctx, "action": "timeout",
                  "tui_active": tui_active,
                  "accumulated_turns": accumulated_turns_above_baseline,
                  "elapsed": round(monotonic() - dispatch_time, 1),
                  "has_output": bool(prev_screen.strip())})
            if not prev_screen.strip():
                raise ExecutionTimeout("claude code tui produced no output after dispatch")
            raise ExecutionTimeout("claude code tui did not become idle within timeout")

        try:
            raw_output = _strip_ansi(host.read_visible(session))
            read = host.read_output(session, cursor)
            full_scrollback = _strip_ansi(host.read_scrollback(session))
        except Exception as exc:
            _logger.warning("post-loop tmux read failed: %s", exc)
            raise PaneDied(f"pane died during extraction: {exc}") from exc
        session.metadata["restored_cursor"] = read.cursor

        # ── Extraction cascade ──────────────────────────────────────
        # Priority: scrollback > visible > empty.
        #
        # Scrollback (full tmux buffer) is the primary source because it
        # always contains the ⏺ response markers even when they scroll
        # off the visible screen.  parse_turns → answered[-1] naturally
        # returns the *last* (= current) response, which is safe even
        # when scrollback contains prior-run content.
        #
        # Visible screen is only used as a fast-path or fallback when
        # scrollback extraction fails (shouldn't happen in practice).
        cleaned = _clean_claude_code_output(full_scrollback)
        extraction_source = "scrollback"
        # Guard against cross-run contamination: if the extracted response
        # matches the baseline (prior run's last response), treat as empty.
        if baseline_last_response and cleaned.strip() == baseline_last_response.strip():
            _dbg({**_dbg_ctx, "action": "extraction_stale_baseline",
                  "cleaned_len": len(cleaned)})
            cleaned = ""
        if not cleaned.strip():
            # Scrollback failed or stale — fall back to visible screen
            cleaned = _clean_claude_code_output(raw_output)
            extraction_source = "visible"
            if baseline_last_response and cleaned.strip() == baseline_last_response.strip():
                cleaned = ""
        _dbg({**_dbg_ctx, "action": "extraction_source",
              "source": extraction_source,
              "result_len": len(cleaned),
              "scrollback_len": len(full_scrollback),
              "elapsed": round(monotonic() - dispatch_time, 1)})

        # JSON contract overlay (only when explicitly requested)
        extraction_diag = None
        if json_contract:
            # Build candidate list without re-cleaning the same source.
            # `cleaned` already comes from scrollback or visible (see above).
            visible_cleaned = _clean_claude_code_output(raw_output) if extraction_source == "scrollback" else cleaned
            cleaned_sources = [
                ("cleaned", cleaned),
                ("visible_cleaned", visible_cleaned),
            ]
            for tag, src in [("scrollback", full_scrollback), ("raw_output", raw_output)]:
                if src:
                    cleaned_sources.append((tag, src))
            selected, extraction_diag = select_structured_result(
                result_file=result_file,
                cleaned_sources=cleaned_sources,
                claimed=claimed,
            )
            if selected:
                cleaned = selected

        if not cleaned.strip():
            _dbg({**_dbg_ctx, "action": "extraction_empty",
                  "scrollback_len": len(full_scrollback),
                  "raw_output_len": len(raw_output),
                  "elapsed": round(monotonic() - dispatch_time, 1)})
            raise ExecutionTimeout("claude code tui produced no output after idle")

        _dbg({**_dbg_ctx, "action": "extraction_ok",
              "result_len": len(cleaned), "elapsed": round(monotonic() - dispatch_time, 1)})
        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                ArtifactPayload(role="prompt", name="task-file.md", content=task_file_content),
                ArtifactPayload(role="transcript_log", name="transcript.txt", content=full_scrollback),
                ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                ArtifactPayload(role="result", name="result.txt", content=cleaned),
            ],
            summary={"adapter": self.kind, "host": host.kind, "run_id": run_id, "mode": "tui",
                      "dispatch": "via_file"},
            diagnostics=extraction_diag.to_dict() if extraction_diag else None,
        )

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> None:
        _dbg = getattr(supervisor, "debug_log", None) or (lambda entry: None)
        run_id = claimed.get("run", {}).get("run_id", "unknown")
        _dbg({"kind": "adapter_recovery", "action": "recover_start",
              "run_id": run_id, "attempt": attempt,
              "error_type": type(error).__name__,
              "session_id": session.session_id})
        health = host.health(session)
        if not health.healthy:
            session.metadata.pop("claude_code_bootstrapped", None)
            _dbg({"kind": "adapter_recovery", "action": "recover_skip_unhealthy",
                  "run_id": run_id, "cleared_bootstrap": True})
            return
        if isinstance(error, PaneDied):
            session.metadata.pop("claude_code_bootstrapped", None)
            _dbg({"kind": "adapter_recovery", "action": "recover_pane_died_reset", "run_id": run_id})
            return
        host.interrupt(session)
        idle = False
        for _ in range(5):
            sleep(0.2)
            if host.shell_idle(session):
                idle = True
                break
        # After Ctrl-C the TUI may have exited, leaving a bare shell.
        # Clear the bootstrap flag so the next run re-launches Claude Code.
        if idle:
            session.metadata.pop("claude_code_bootstrapped", None)
        _dbg({"kind": "adapter_recovery", "action": "recover_done",
              "run_id": run_id, "shell_idle": idle,
              "cleared_bootstrap": idle})

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        if isinstance(error, StableButIndeterminate):
            screen = error.screen or ""
            try:
                screen = screen or _strip_ansi(host.read_visible(session))
                scrollback = _strip_ansi(host.read_scrollback(session))
            except Exception:
                scrollback = ""
            cleaned = _clean_claude_code_output(scrollback) or _clean_claude_code_output(screen)
            return ExecutionResult(
                artifacts=[
                    ArtifactPayload(role="prompt", name="prompt.txt", content=prompt_for_claim(claimed=claimed)),
                    ArtifactPayload(role="transcript_log", name="transcript.txt", content=cleaned),
                    ArtifactPayload(role="exec_log", name="exec.txt", content=screen),
                    ArtifactPayload(role="failure_evidence", name="screen.txt", content=screen),
                    ArtifactPayload(role="failure_evidence", name="failure.txt", content=str(error)),
                ],
                summary={
                    "adapter": self.kind,
                    "host": host.kind,
                    "exception_type": "StableButIndeterminate",
                    "indeterminate": True,
                },
            )
        if isinstance(error, AdapterExecutionFailed):
            return ExecutionResult(
                artifacts=[
                    ArtifactPayload(role="prompt", name="prompt.txt", content=prompt_for_claim(claimed=claimed)),
                    ArtifactPayload(role="transcript_log", name="transcript.txt", content=error.transcript),
                    ArtifactPayload(role="exec_log", name="exec.txt", content=error.output),
                    ArtifactPayload(role="failure_evidence", name="failure.txt", content=str(error)),
                ],
                summary={"adapter": self.kind, "host": host.kind, "exception_type": type(error).__name__},
            )
        return super().build_failure_result(
            host=host, session=session, claimed=claimed, error=error, supervisor=supervisor,
        )
