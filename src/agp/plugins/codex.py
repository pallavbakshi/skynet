"""Codex CLI agent adapter plugin."""
from __future__ import annotations
import json
import os
import shlex
from time import monotonic, sleep
from typing import Any

from agp.runtime import (
    AdapterExecutionFailed, AgentAdapter, ArtifactPayload, ExecutionResult,
    BootstrapFailure, ExecutionTimeout, PaneDied, RecoverableExecutionError,
    TerminalHost, TerminalSession,
    _strip_ansi,
)

# ── Codex TUI markers ────────────────────────────────────────────────
_PROMPT_MARKER = "\u203a"  # › = user prompt
_RESPONSE_MARKER = "\u2022"  # • = assistant response
_BOX_CHARS = set("\u2500\u2502\u256d\u256e\u256f\u2570\u2514\u250c\u2510\u2518\u2524\u251c\u252c\u2534\u253c\u2501\u2503")

# Lines matching any of these are TUI noise, not content.
_NOISE_PREFIXES = (
    "Token usage:",
    "To continue this session",
    "Tip:",
    "\u26a0",  # ⚠ warning
    "\u2728",  # ✨ update banner
    "See https://",
    "See full release",
    ">_",  # welcome box header
    "model:",
    "directory:",
    "Press enter to",
    "Approaching rate",
    "Switch to gpt-",
    "Keep current model",
    "Hide future rate",
    "Optimized for codex",
    "Working (",  # transient "Working (3s • esc to interrupt)" status
    "Use /skills",  # placeholder hint
)
_NOISE_INFIXES = (
    "\u00b7",  # · in status bar
)



def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if all(ch in _BOX_CHARS or ch in " \t" for ch in s):
        return True
    for prefix in _NOISE_PREFIXES:
        if s.startswith(prefix):
            return True
    for infix in _NOISE_INFIXES:
        if infix in s and ("left" in s or "%" in s):
            return True
    return False


def _collect_bullet_lines(lines: list[str]) -> list[str]:
    """Return the text content of all •-prefixed lines."""
    result = []
    for line in lines:
        s = line.strip()
        if s.startswith(_RESPONSE_MARKER):
            result.append(s.removeprefix(_RESPONSE_MARKER).strip())
    return result


def _clean_codex_tui_output(text: str) -> str:
    """Extract the last Codex response from raw TUI output.

    Parses the TUI structure using › (prompt) and • (response) markers,
    strips all chrome (box borders, status bar, banners, tips, token usage),
    and returns only the response text from the most recent turn.
    """
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()
    turns = _parse_codex_turns(text)

    if not turns:
        # Fallback: collect all •-prefixed lines as response content.
        bullet_lines = _collect_bullet_lines(lines)
        if bullet_lines:
            return "\n".join(bullet_lines)
        # Last resort: strip noise and return whatever is left.
        fallback = [ln.rstrip() for ln in lines if not _is_noise_line(ln)]
        while fallback and not fallback[0]:
            fallback.pop(0)
        while fallback and not fallback[-1]:
            fallback.pop()
        return "\n".join(fallback)

    # Find the last turn that has actual response content (not dialog noise).
    for turn in reversed(turns):
        content = [ln for ln in turn["response"] if ln.strip()]
        if content:
            return "\n".join(content)

    # All turns were noise — fall back to collecting all • lines.
    return "\n".join(_collect_bullet_lines(lines))


def _parse_codex_turns(text: str) -> list[dict[str, Any]]:
    """Parse visible Codex TUI output into prompt/response turns."""
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()
    turns: list[dict[str, Any]] = []
    current_prompt = ""
    response_lines: list[str] = []
    in_response = False

    for line in lines:
        s = line.strip()
        if s.startswith(_PROMPT_MARKER):
            if in_response and response_lines:
                turns.append({"prompt": current_prompt, "response": list(response_lines)})
            current_prompt = s.removeprefix(_PROMPT_MARKER).strip()
            response_lines = []
            in_response = False
        elif s.startswith(_RESPONSE_MARKER):
            in_response = True
            content = s.removeprefix(_RESPONSE_MARKER).strip()
            if content and not _is_noise_line(content):
                response_lines.append(content)
        elif in_response and not _is_noise_line(line):
            response_lines.append(s)

    if in_response and response_lines:
        turns.append({"prompt": current_prompt, "response": list(response_lines)})
    return turns


class CodexAdapter(AgentAdapter):
    def __init__(
        self,
        *,
        begin_marker: str = "AGP_RUN_BEGIN",
        result_marker: str = "AGP_RUN_RESULT",
        max_polls: int = 20,
        poll_interval_seconds: float = 0.25,
        bootstrap_settle_seconds: float = 0.0,
        idle_timeout_polls: int = 0,
        health_check_interval_polls: int = 10,
        cli_command: str = "codex",
        tui_mode: bool = False,
        idle_poll_seconds: float = 2.0,
        idle_after: int = 3,
        idle_timeout_seconds: float = 0.0,
        session_mode: str = "ephemeral",
    ) -> None:
        self.begin_marker = begin_marker
        self.result_marker = result_marker
        self.max_polls = max_polls
        self.poll_interval_seconds = poll_interval_seconds
        self.bootstrap_settle_seconds = bootstrap_settle_seconds
        self.idle_timeout_polls = idle_timeout_polls
        self.health_check_interval_polls = health_check_interval_polls
        self.cli_command = cli_command
        self.tui_mode = tui_mode
        self.idle_poll_seconds = idle_poll_seconds
        self.idle_after = idle_after
        self.idle_timeout_seconds = idle_timeout_seconds
        self.session_mode = session_mode

    @property
    def kind(self) -> str:
        return "codex"

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        cleaned = _clean_codex_tui_output(text)
        payload = None
        if run_id:
            try:
                payload = self._extract_terminal_payload(run_id=run_id, output=text)
            except RuntimeError as exc:
                payload = {"error": str(exc)}
        return {
            "adapter_kind": self.kind,
            "mode": "tui" if self.tui_mode else "marker",
            "run_id": run_id,
            "cleaned_output": cleaned,
            "marker_payload": payload,
            "looks_like_gate_prompt": self._looks_like_gate_prompt(_strip_ansi(text)),
            "looks_like_codex_ready": self._looks_like_codex_ready(_strip_ansi(text)),
            "looks_like_shell_returned": self._looks_like_shell_returned(_strip_ansi(text)),
            "supported": True,
        }

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        if session.metadata.get("codex_bootstrapped"):
            # In sticky mode, verify the TUI process is still alive before
            # skipping re-bootstrap.  If it crashed between jobs, clear the
            # flag and fall through to re-launch.
            if self.tui_mode and hasattr(host, "is_foreground_tui"):
                if not host.is_foreground_tui(session):
                    session.metadata.pop("codex_bootstrapped", None)
                else:
                    return
            else:
                return
        health = host.health(session)
        if not health.healthy:
            raise BootstrapFailure(f"session unhealthy before bootstrap: {health.reason}")
        if self.tui_mode:
            # On this Linux host, interactive Codex launched inside tmux accepts the
            # prompt visually but does not progress when the prompt is injected later
            # with send-keys.  We therefore use a per-run launch path for tmux TUI
            # execution and skip persistent bootstrap here.
            if host.kind == "tmux":
                session.metadata["codex_bootstrapped"] = True
                return
            # If the TUI is already running in this pane (e.g. reused session
            # from a prior process), skip launching and just set the flag.
            if hasattr(host, "is_foreground_tui") and host.is_foreground_tui(session):
                session.metadata["codex_bootstrapped"] = True
                return
            host.send_text(session, self.cli_command, enter=True)
            # Poll the visible screen (alternate buffer) to detect gate
            # prompts, CLI exit, and the Codex ready state.
            deadline = monotonic() + (self.idle_timeout_seconds if self.idle_timeout_seconds > 0 else 60.0)
            while monotonic() < deadline:
                sleep(self.idle_poll_seconds)
                screen = _strip_ansi(host.read_visible(session))
                if self._looks_like_gate_prompt(screen):
                    host.send_text(session, self._gate_response(screen), enter=True)
                    continue
                if self._looks_like_codex_ready(screen):
                    break
            else:
                raise BootstrapFailure("codex cli did not become ready after launch")
        else:
            bootstrap = (
                "You are running inside AGP. "
                "Each AGP task will provide a run envelope. "
                f"When you see a line starting with {self.begin_marker} followed by a run id, treat that as the current task context. "
                f"When that task reaches a terminal outcome, emit exactly one line beginning with "
                f"{self.result_marker} <run_id> "
                'followed by compact JSON like {"status":"success","result":"..."} '
                'or {"status":"failure","error":"..."}. '
                f"Do not emit lines beginning with {self.result_marker} except as the single terminal line for the active AGP task."
            )
            host.send_text(session, bootstrap, enter=True)
        if self.bootstrap_settle_seconds > 0:
            sleep(self.bootstrap_settle_seconds)
            health = host.health(session)
            if not health.healthy:
                raise BootstrapFailure(f"session unhealthy after bootstrap: {health.reason}")
        session.metadata["codex_bootstrapped"] = True

    # Patterns that indicate a TUI gate/confirmation prompt that should
    # be auto-dismissed.  For numbered menus, the adapter sends the
    # preferred choice; for simple confirmations it sends Enter.
    _GATE_PATTERNS = (
        "welcome to codex",
        "sign in with chatgpt",
        "sign in with device code",
        "provide your own api key",
        "trust the contents",
        "do you trust",
        "press enter to continue",
        "yes, continue",
        "approaching rate limits",
        "introducing gpt-",
        "try new model",
        "use existing model",
        "switch to gpt-",
        "press enter to confirm or esc",
    )

    # Preferred default choices for numbered dialog menus.
    # Maps a recognisable phrase to the number key to send.
    # Iteration order matters — first match wins when multiple phrases overlap.
    _GATE_CHOICES = {
        "approaching rate limits": "3",  # "Keep current model (never show again)"
        "introducing gpt-": "2",  # Use existing model
        "try new model": "2",
        "switch to gpt-": "3",
    }

    @staticmethod
    def _preferred_auth_choice() -> str:
        """Choose the safest onboarding path for the current runtime env."""
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"):
            return "3"  # Provide your own API key
        if os.environ.get("CODEX_PREFER_DEVICE_CODE", "").lower() in {"1", "true", "yes"}:
            return "2"
        return "1"

    def _looks_like_onboarding_prompt(self, text: str) -> bool:
        lower = text.lower()
        return (
            "welcome to codex" in lower
            and "sign in with chatgpt" in lower
            and "provide your own api key" in lower
        )

    def _looks_like_gate_prompt(self, text: str) -> bool:
        lower = text.lower()
        if self._looks_like_onboarding_prompt(text):
            return True
        return any(pat in lower for pat in self._GATE_PATTERNS)

    def _gate_response(self, text: str) -> str:
        """Return the key to send for a gate prompt (a number for menus, empty for Enter)."""
        if self._looks_like_onboarding_prompt(text):
            return self._preferred_auth_choice()
        lower = text.lower()
        for phrase, choice in self._GATE_CHOICES.items():
            if phrase in lower:
                return choice
        return ""

    # Characters that indicate a shell prompt (CLI exited).
    _SHELL_MARKERS = {"\u276f", "$", "%", "#"}

    @staticmethod
    def _looks_like_codex_ready(text: str) -> bool:
        """Return True when the visible screen shows the Codex input prompt."""
        return _PROMPT_MARKER in text

    # Box-drawing characters and keywords that indicate a TUI is rendering
    # (broader than just the › prompt marker — covers startup banners and
    # welcome boxes that appear before the first › prompt is drawn).
    _TUI_BOX_CHARS = set("\u256d\u256e\u256f\u2570\u2502\u2500")  # ╭╮╯╰│─
    _TUI_CONTENT_HINTS = (
        "codex",
        "model:",
        "directory:",
        "context left",
        "update available",
    )

    def _looks_like_shell_returned(self, text: str) -> bool:
        """Return True when the visible screen shows a shell prompt (CLI exited).

        Checks the last 5 non-empty lines for shell prompt characters vs.
        TUI indicators.  TUI indicators include the › prompt marker, box-
        drawing characters, and known TUI content keywords.  This avoids
        false positives during TUI startup when the shell prompt is still
        visible in scrollback but the TUI has begun rendering.
        """
        lines = text.strip().splitlines()
        tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
        has_tui = any(_PROMPT_MARKER in ln for ln in tail)
        has_shell = any(ln[0] in self._SHELL_MARKERS for ln in tail if ln)
        if not has_shell:
            return False
        if has_tui:
            return False
        # Check for TUI box-drawing chars or content hints anywhere in tail
        for ln in tail:
            if any(ch in self._TUI_BOX_CHARS for ch in ln):
                return False
        lower_tail = "\n".join(tail).lower()
        if any(hint in lower_tail for hint in self._TUI_CONTENT_HINTS):
            return False
        return True

    def _begin_line(self, run_id: str) -> str:
        return f"{self.begin_marker} {run_id}"

    def _result_prefix(self, run_id: str) -> str:
        return f"{self.result_marker} {run_id} "

    def _task_payload(self, *, run_id: str, prompt: str) -> str:
        return (
            f"{self._begin_line(run_id)}\n"
            "AGP task instructions:\n"
            f"{prompt}\n\n"
            "Terminal contract:\n"
            f"- Finalize this task by emitting exactly one line that starts with {self._result_prefix(run_id)}\n"
            '- Use JSON payload {"status":"success","result":"..."} or {"status":"failure","error":"..."}\n'
            "- Do not emit terminal lines for any other run id.\n"
        )

    def _extract_terminal_payload(self, *, run_id: str, output: str) -> dict[str, Any] | None:
        prefix = self._result_prefix(run_id)
        for line in reversed(output.splitlines()):
            if not line.startswith(prefix):
                continue
            raw = line.removeprefix(prefix).strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"invalid codex terminal payload for run {run_id}") from None
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid codex terminal payload type for run {run_id}")
            return payload
        return None

    @staticmethod
    def _normalise_visible_screen(raw: str) -> str:
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        lines = [ln.rstrip() for ln in lines]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def _looks_like_completed_turn(
        self,
        text: str,
        *,
        baseline_answered_turns: int,
        baseline_last_response: str | None,
    ) -> bool:
        """Return True when Codex has answered and returned to a fresh prompt."""
        turns = _parse_codex_turns(text)
        if not turns:
            return False
        meaningful: list[str] = []
        for raw in _strip_ansi(text).splitlines():
            s = raw.strip()
            if not s:
                continue
            if _is_noise_line(raw):
                continue
            meaningful.append(s)
        if not meaningful:
            return False
        last = meaningful[-1]
        if not last.startswith(_PROMPT_MARKER):
            return False
        answered = [turn for turn in turns if turn["response"]]
        if len(answered) > baseline_answered_turns:
            return True
        if not answered:
            return False
        latest_response = "\n".join(answered[-1]["response"]).strip()
        if latest_response and latest_response != (baseline_last_response or ""):
            return True
        return False

    def _looks_like_working(self, text: str) -> bool:
        """Return True when Codex shows an active Working indicator."""
        return any(ln.strip().startswith("Working (") for ln in text.splitlines())

    @staticmethod
    def _screen_tail(text: str, n: int = 10) -> str:
        """Return the last N non-empty lines of the visible screen."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        lines = [ln.rstrip() for ln in lines if ln.strip()]
        return "\n".join(lines[-n:])

    def _idle_timeout_window(self) -> float:
        if self.idle_timeout_seconds > 0:
            return self.idle_timeout_seconds
        # Keep a legacy fallback when the explicit idle timeout is unset so
        # older configs still terminate, but all execution loops consume the
        # resolved window as a single timeout budget.
        if self.max_polls > 0 and self.poll_interval_seconds > 0:
            return self.max_polls * self.poll_interval_seconds
        return 180.0

    def _progress_heartbeat_interval(self, *, timeout: float, poll_seconds: float) -> float:
        baseline = poll_seconds if poll_seconds > 0 else 0.25
        return max(baseline, min(10.0, max(timeout / 4.0, baseline)))

    def _maybe_emit_progress_heartbeat(
        self,
        *,
        supervisor: "RuntimeSupervisor",
        claimed: dict[str, Any],
        session: TerminalSession,
        stage: str,
        changed: bool,
        poll: int,
        now: float,
        last_heartbeat_at: float,
        heartbeat_interval: float,
        extra: dict[str, Any] | None = None,
    ) -> float:
        if changed or now - last_heartbeat_at >= heartbeat_interval:
            self._emit_progress_heartbeat(
                supervisor=supervisor,
                claimed=claimed,
                session=session,
                stage=stage,
                changed=changed,
                poll=poll,
                extra=extra,
            )
            return now
        return last_heartbeat_at

    def _emit_progress_heartbeat(
        self,
        *,
        supervisor: "RuntimeSupervisor",
        claimed: dict[str, Any],
        session: TerminalSession,
        stage: str,
        changed: bool,
        poll: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        details = {
            "adapter": self.kind,
            "session_id": session.session_id,
            "run_id": claimed["run"]["run_id"],
            "stage": stage,
            "changed": changed,
            "poll": poll,
        }
        if extra:
            details.update(extra)
        supervisor.emit_progress(
            claimed,
            message="runtime.progress_heartbeat",
            details=details,
        )

    def _salvage_timeout_artifacts(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        error: Exception,
    ) -> list[ArtifactPayload]:
        if not isinstance(error, ExecutionTimeout):
            return []
        artifacts: list[ArtifactPayload] = []
        snapshot_text = ""
        try:
            snapshot = host.snapshot(session)
        except Exception:  # noqa: BLE001
            snapshot = {}
        else:
            snapshot_text = _strip_ansi(str(snapshot.get("text") or snapshot.get("accumulated_text") or ""))
        try:
            visible = _strip_ansi(host.read_visible(session))
        except Exception:  # noqa: BLE001
            visible = ""
        pane_text = snapshot_text if snapshot_text.strip() else visible
        if pane_text.strip():
            artifacts.append(
                ArtifactPayload(
                    role="failure_evidence",
                    name=f"{host.kind}-pane.txt",
                    content=pane_text,
                )
            )
        if visible.strip() and visible.strip() != pane_text.strip():
            artifacts.append(
                ArtifactPayload(
                    role="failure_evidence",
                    name=f"{host.kind}-visible.txt",
                    content=visible,
                )
            )
        return artifacts

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        if self.tui_mode:
            return self._execute_run_tui(host=host, session=session, claimed=claimed, supervisor=supervisor)
        return self._execute_run_marker(host=host, session=session, claimed=claimed, supervisor=supervisor)

    def _execute_run_tui(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        """TUI mode: send prompt -> wait for idle -> read delta -> clean output."""
        prompt = claimed["message"]["text"]
        run_id = claimed["run"]["run_id"]

        # Session reset logic depends on host kind and session_mode:
        # - tmux always resets (send-keys unreliable with running TUI)
        # - wezterm resets only in ephemeral mode; sticky keeps the session
        if host.kind == "tmux":
            if self.session_mode == "sticky":
                import logging
                logging.getLogger(__name__).warning(
                    "sticky session_mode is not supported on tmux — falling back to ephemeral"
                )
            session = host.reset_session(session)
            self.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        elif self.session_mode == "ephemeral":
            session = host.reset_session(session)
            self.ensure_bootstrapped(host=host, session=session, claimed=claimed)

        # Capture baseline AFTER reset/bootstrap so it reflects the fresh pane.
        baseline_screen = _strip_ansi(host.read_visible(session))
        baseline_turns = [turn for turn in _parse_codex_turns(baseline_screen) if turn["response"]]
        baseline_last_response = None
        if baseline_turns:
            baseline_last_response = "\n".join(baseline_turns[-1]["response"]).strip()

        health = host.health(session)
        if not health.healthy:
            raise PaneDied(f"session unhealthy at dispatch: {health.reason}")

        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={"adapter": self.kind, "session_id": session.session_id, "run_id": run_id},
        )
        if host.kind == "tmux":
            # Provider env vars are already exported into the shell by
            # get_or_create_session (called via reset_session above), so
            # codex inherits them without inline key interpolation.
            host.send_text(session, f"{self.cli_command} {shlex.quote(prompt)}", enter=True)
        else:
            host.send_text(session, prompt, enter=True)

        def _poll_hook() -> None:
            supervisor.check_interrupt(claimed)

        # Verify Codex produced a response, and auto-dismiss any gate prompts
        # (including first-run onboarding) that appear mid-run.
        timeout = self._idle_timeout_window()
        idle_deadline = monotonic() + timeout
        heartbeat_interval = self._progress_heartbeat_interval(timeout=timeout, poll_seconds=self.idle_poll_seconds)
        prev_screen = ""
        prev_tail = ""
        unchanged = 0
        tui_active = False  # True once the TUI has drawn at least one frame
        dispatch_time = monotonic()
        last_heartbeat_at = dispatch_time
        poll_count = 0
        while monotonic() < idle_deadline:
            poll_count += 1
            sleep(self.idle_poll_seconds)
            _poll_hook()
            screen = _strip_ansi(host.read_visible(session))
            read = host.read_output(session, cursor)
            cursor = read.cursor
            snap = self._normalise_visible_screen(screen)
            tail = self._screen_tail(screen)
            changed = bool(read.changed or snap != prev_screen or tail != prev_tail)
            now = monotonic()
            if changed:
                idle_deadline = now + timeout
            last_heartbeat_at = self._maybe_emit_progress_heartbeat(
                supervisor=supervisor,
                claimed=claimed,
                session=session,
                stage="tui",
                changed=changed,
                poll=poll_count,
                now=now,
                last_heartbeat_at=last_heartbeat_at,
                heartbeat_interval=heartbeat_interval,
                extra={
                    "tail_lines": len([ln for ln in tail.splitlines() if ln.strip()]),
                    "idle_seconds_remaining": max(0.0, idle_deadline - now),
                },
            )
            # Only check for shell return after the TUI has had time to
            # render.  During the first few seconds the old shell prompt
            # may still be visible in scrollback while the TUI boots.
            startup_settled = tui_active or (now - dispatch_time > 5.0)
            if startup_settled and self._looks_like_shell_returned(screen):
                raise PaneDied("codex cli exited during execution")
            if self._looks_like_gate_prompt(screen):
                # Only dismiss if the screen changed since the last dismiss
                # to avoid spamming Enter on an unrecognised persistent dialog.
                if snap != prev_screen:
                    host.send_text(session, self._gate_response(screen), enter=True)
                prev_screen = snap
                prev_tail = tail
                unchanged = 0
                tui_active = True
                continue

            # Track stability based on the tail of the visible screen.
            if tail == prev_tail:
                unchanged += 1
            else:
                unchanged = 0
                tui_active = True
            prev_screen = snap
            prev_tail = tail

            # Stability gate: only evaluate state after the screen is stable.
            stable_after = max(1, self.idle_after - 1)
            if unchanged < stable_after:
                continue

            # Screen is stable.  Check if Codex is still actively working.
            if self._looks_like_working(screen):
                unchanged = 0
                continue

            # Stable and not working.  Check for completion.
            if self._looks_like_completed_turn(
                screen,
                baseline_answered_turns=len(baseline_turns),
                baseline_last_response=baseline_last_response,
            ):
                break
        else:
            if not prev_screen.strip():
                raise ExecutionTimeout("codex tui produced no output after dispatch")
            raise ExecutionTimeout("codex tui did not become idle within timeout")

        # Use the visible screen for TUI output — scrollback deltas are
        # unreliable because TUI apps repaint the entire screen.
        raw_output = _strip_ansi(host.read_visible(session))
        # Also update the cursor/accumulator for bookkeeping.
        read = host.read_output(session, cursor)
        # Preserve the updated cursor so the next sticky run starts from
        # this point instead of creating a fresh baseline.
        session.metadata["restored_cursor"] = read.cursor
        cleaned = _clean_codex_tui_output(raw_output)

        if not cleaned.strip():
            raise ExecutionTimeout("codex tui produced no output after idle")

        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                ArtifactPayload(role="transcript_log", name="transcript.txt", content=raw_output),
                ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                ArtifactPayload(role="result", name="result.txt", content=cleaned),
            ],
            summary={"adapter": self.kind, "host": host.kind, "run_id": run_id, "mode": "tui"},
        )

    def _execute_run_marker(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        """Marker mode: send AGP envelope -> poll for result marker -> parse JSON payload."""
        prompt = claimed["message"]["text"]
        run_id = claimed["run"]["run_id"]

        health = host.health(session)
        if not health.healthy:
            raise PaneDied(f"session unhealthy at dispatch: {health.reason}")

        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        host.send_text(session, self._task_payload(run_id=run_id, prompt=prompt), enter=True)
        transcript_parts: list[str] = [f"prompt={prompt}\n"]
        idle_count = 0
        timeout = self._idle_timeout_window()
        idle_deadline = monotonic() + timeout
        heartbeat_interval = self._progress_heartbeat_interval(timeout=timeout, poll_seconds=self.poll_interval_seconds)
        last_heartbeat_at = monotonic()
        attempt = 0
        while monotonic() < idle_deadline:
            attempt += 1
            supervisor.check_interrupt(claimed)

            if self.health_check_interval_polls > 0 and attempt > 0 and attempt % self.health_check_interval_polls == 0:
                h = host.health(session)
                if not h.healthy:
                    raise PaneDied(f"session lost during execution at poll {attempt}: {h.reason}")

            read = host.read_output(session, cursor)
            cursor = read.cursor
            now = monotonic()
            if read.changed and read.text:
                idle_count = 0
                idle_deadline = now + timeout
                transcript_parts.append(read.text)
                last_heartbeat_at = self._maybe_emit_progress_heartbeat(
                    supervisor=supervisor,
                    claimed=claimed,
                    session=session,
                    stage="marker",
                    changed=True,
                    poll=attempt,
                    now=now,
                    last_heartbeat_at=last_heartbeat_at,
                    heartbeat_interval=heartbeat_interval,
                    extra={"idle_seconds_remaining": max(0.0, idle_deadline - now)},
                )
                try:
                    payload = self._extract_terminal_payload(run_id=run_id, output=read.full_text)
                except RuntimeError as exc:
                    raise RecoverableExecutionError(str(exc)) from exc
                if payload is not None:
                    transcript = "".join(transcript_parts)
                    status = str(payload.get("status", "")).strip().lower()
                    if status == "failure":
                        raise AdapterExecutionFailed(
                            str(payload.get("error") or "codex adapter reported task failure"),
                            transcript=transcript,
                            output=read.full_text,
                        )
                    if status != "success":
                        raise RecoverableExecutionError(f"invalid codex terminal status for run {run_id}: {status or 'missing'}")
                    result_text = str(payload.get("result") or "").strip()
                    session.metadata["restored_cursor"] = read.cursor
                    return ExecutionResult(
                        artifacts=[
                            ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                            ArtifactPayload(role="transcript_log", name="transcript.txt", content=transcript),
                            ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                            ArtifactPayload(role="result", name="result.txt", content=result_text or json.dumps(payload, sort_keys=True)),
                        ],
                        summary={"adapter": self.kind, "host": host.kind, "run_id": run_id},
                    )
            else:
                idle_count += 1
                last_heartbeat_at = self._maybe_emit_progress_heartbeat(
                    supervisor=supervisor,
                    claimed=claimed,
                    session=session,
                    stage="marker",
                    changed=False,
                    poll=attempt,
                    now=now,
                    last_heartbeat_at=last_heartbeat_at,
                    heartbeat_interval=heartbeat_interval,
                    extra={
                        "idle_polls": idle_count,
                        "idle_seconds_remaining": max(0.0, idle_deadline - now),
                    },
                )
            sleep(self.poll_interval_seconds)
        raise ExecutionTimeout("codex adapter did not observe completion marker before idle timeout")

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,  # noqa: ARG002
        error: Exception,
        supervisor: "RuntimeSupervisor",  # noqa: ARG002
    ) -> None:
        health = host.health(session)
        if not health.healthy:
            return
        # If Codex exited (crashed), clear the bootstrap flag so the
        # supervisor retry path will re-launch it via ensure_bootstrapped().
        if isinstance(error, PaneDied):
            session.metadata.pop("codex_bootstrapped", None)
            return
        host.interrupt(session)
        sleep(max(self.poll_interval_seconds, 0.1))

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        if isinstance(error, AdapterExecutionFailed):
            return ExecutionResult(
                artifacts=[
                    ArtifactPayload(role="prompt", name="prompt.txt", content=claimed["message"]["text"]),
                    ArtifactPayload(role="transcript_log", name="transcript.txt", content=error.transcript),
                    ArtifactPayload(role="exec_log", name="exec.txt", content=error.output),
                    ArtifactPayload(role="failure_evidence", name="failure.txt", content=str(error)),
                ],
                summary={"adapter": self.kind, "host": host.kind, "exception_type": type(error).__name__},
            )
        result = super().build_failure_result(
            host=host,
            session=session,
            claimed=claimed,
            error=error,
            supervisor=supervisor,
        )
        result.artifacts.extend(self._salvage_timeout_artifacts(host=host, session=session, error=error))
        return result
