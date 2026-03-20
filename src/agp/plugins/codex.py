"""Codex CLI agent adapter plugin."""
from __future__ import annotations
import json
from time import monotonic, sleep
from typing import Any

from agp.runtime import (
    AdapterExecutionFailed, AgentAdapter, ArtifactPayload, ExecutionResult,
    RecoverableExecutionError, TerminalHost, TerminalSession,
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


def _clean_codex_tui_output(text: str) -> str:
    """Extract the last Codex response from raw TUI output.

    Parses the TUI structure using › (prompt) and • (response) markers,
    strips all chrome (box borders, status bar, banners, tips, token usage),
    and returns only the response text from the most recent turn.
    """
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()

    # Find turns: each turn starts with a › prompt line.
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
            if content:
                response_lines.append(content)
        elif in_response and not _is_noise_line(line):
            response_lines.append(s)

    # Capture the last open turn.
    if in_response and response_lines:
        turns.append({"prompt": current_prompt, "response": list(response_lines)})

    if not turns:
        # Fallback: collect all •-prefixed lines as response content.
        response_lines = []
        for line in lines:
            s = line.strip()
            if s.startswith(_RESPONSE_MARKER):
                response_lines.append(s.removeprefix(_RESPONSE_MARKER).strip())
        if response_lines:
            return "\n".join(response_lines)
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
            while content and not content[-1]:
                content.pop()
            return "\n".join(content)

    # All turns were noise — fall back to collecting all • lines.
    response_lines = []
    for line in lines:
        s = line.strip()
        if s.startswith(_RESPONSE_MARKER):
            response_lines.append(s.removeprefix(_RESPONSE_MARKER).strip())
    return "\n".join(response_lines)


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

    @property
    def kind(self) -> str:
        return "codex"

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        cleaned = _clean_codex_tui_output(text)
        payload = self._extract_terminal_payload(run_id=run_id, output=text) if run_id else None
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
            return
        health = host.health(session)
        if not health.healthy:
            raise RecoverableExecutionError(f"session unhealthy before bootstrap: {health.reason}")
        if self.tui_mode:
            host.send_text(session, self.cli_command, enter=True)
            # Poll the visible screen (alternate buffer) to detect gate
            # prompts, CLI exit, and the Codex ready state.
            deadline = monotonic() + (self.idle_timeout_seconds or 60.0)
            while monotonic() < deadline:
                sleep(self.idle_poll_seconds)
                screen = _strip_ansi(host.read_visible(session))
                if self._looks_like_gate_prompt(screen):
                    host.send_text(session, self._gate_response(screen), enter=True)
                    continue
                if self._looks_like_codex_ready(screen):
                    break
            else:
                raise RecoverableExecutionError("codex cli did not become ready after launch")
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
                raise RecoverableExecutionError(f"session unhealthy after bootstrap: {health.reason}")
        session.metadata["codex_bootstrapped"] = True

    # Patterns that indicate a TUI gate/confirmation prompt that should
    # be auto-dismissed.  For numbered menus, the adapter sends the
    # preferred choice; for simple confirmations it sends Enter.
    _GATE_PATTERNS = (
        "trust the contents",
        "do you trust",
        "press enter to continue",
        "yes, continue",
        "approve",
        "confirm",
        "permission",
        "allow",
        "approaching rate limits",
        "switch to gpt-",
        "press enter to confirm or esc",
    )

    # Preferred default choices for numbered dialog menus.
    # Maps a recognisable phrase to the number key to send.
    _GATE_CHOICES = {
        "approaching rate limits": "3",  # "Keep current model (never show again)"
        "switch to gpt-": "3",
    }

    def _looks_like_gate_prompt(self, text: str) -> bool:
        lower = text.lower()
        return any(pat in lower for pat in self._GATE_PATTERNS)

    def _gate_response(self, text: str) -> str:
        """Return the key to send for a gate prompt (a number for menus, empty for Enter)."""
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

    def _looks_like_shell_returned(self, text: str) -> bool:
        """Return True when the visible screen shows a shell prompt (CLI exited)."""
        lines = text.strip().splitlines()
        tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
        has_tui = any(_PROMPT_MARKER in ln for ln in tail)
        has_shell = any(ln[0] in self._SHELL_MARKERS for ln in tail if ln)
        return has_shell and not has_tui

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

        health = host.health(session)
        if not health.healthy:
            raise RecoverableExecutionError(f"session unhealthy at dispatch: {health.reason}")

        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={"adapter": self.kind, "session_id": session.session_id, "run_id": run_id},
        )
        host.send_text(session, prompt, enter=True)

        def _poll_hook() -> None:
            supervisor.check_interrupt(claimed)

        # Wait for idle, verify Codex produced a response, and auto-dismiss
        # any gate prompts (like rate-limit dialogs) that appear mid-run.
        timeout = self.idle_timeout_seconds or 180.0
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            remaining = max(deadline - monotonic(), 1.0)
            idle = host.wait_for_idle(
                session,
                poll_seconds=self.idle_poll_seconds,
                idle_after=self.idle_after,
                timeout_seconds=remaining,
                on_poll=_poll_hook,
            )
            if not idle:
                raise RecoverableExecutionError("codex tui did not become idle within timeout")

            screen = _strip_ansi(host.read_visible(session))
            if self._looks_like_shell_returned(screen):
                raise RecoverableExecutionError("codex cli exited during execution")
            if self._looks_like_gate_prompt(screen):
                host.send_text(session, self._gate_response(screen), enter=True)
                continue
            if _RESPONSE_MARKER in screen:
                break
            sleep(self.idle_poll_seconds)

        # Use the visible screen for TUI output — scrollback deltas are
        # unreliable because TUI apps repaint the entire screen.
        raw_output = _strip_ansi(host.read_visible(session))
        # Also update the cursor/accumulator for bookkeeping.
        read = host.read_output(session, cursor)
        cleaned = _clean_codex_tui_output(raw_output)

        if not cleaned.strip():
            raise RecoverableExecutionError("codex tui produced no output after idle")

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
            raise RecoverableExecutionError(f"session unhealthy at dispatch: {health.reason}")

        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        host.send_text(session, self._task_payload(run_id=run_id, prompt=prompt), enter=True)
        transcript_parts: list[str] = [f"prompt={prompt}\n"]
        idle_count = 0
        for attempt in range(self.max_polls):
            supervisor.check_interrupt(claimed)

            if self.health_check_interval_polls > 0 and attempt > 0 and attempt % self.health_check_interval_polls == 0:
                h = host.health(session)
                if not h.healthy:
                    raise RecoverableExecutionError(f"session lost during execution at poll {attempt}: {h.reason}")

            read = host.read_output(session, cursor)
            cursor = read.cursor
            if read.changed and read.text:
                idle_count = 0
                transcript_parts.append(read.text)
                supervisor.emit_progress(
                    claimed,
                    message="runtime.output",
                    details={
                        "adapter": self.kind,
                        "session_id": session.session_id,
                        "poll": attempt + 1,
                        "changed": True,
                    },
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
                if self.idle_timeout_polls > 0 and idle_count >= self.idle_timeout_polls:
                    raise RecoverableExecutionError(
                        f"codex adapter idle for {idle_count} consecutive polls — possible CLI wedge"
                    )
            sleep(self.poll_interval_seconds)
        raise RecoverableExecutionError("codex adapter did not observe completion marker before poll budget exhausted")

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,  # noqa: ARG002
        error: Exception,  # noqa: ARG002
        supervisor: "RuntimeSupervisor",  # noqa: ARG002
    ) -> None:
        health = host.health(session)
        if not health.healthy:
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
        return super().build_failure_result(
            host=host,
            session=session,
            claimed=claimed,
            error=error,
            supervisor=supervisor,
        )
