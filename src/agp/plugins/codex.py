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

# Box-drawing and decorative characters used by TUI borders/frames.
_BOX_CHARS = set("\u2500\u2502\u256d\u256e\u256f\u2570\u2514\u250c\u2510\u2518\u2524\u251c\u252c\u2534\u253c\u2501\u2503")


def _clean_codex_tui_output(text: str) -> str:
    """Strip Codex TUI chrome from raw terminal text.

    Removes ANSI escapes, box-drawing borders, the status bar, and blank noise
    to leave only the meaningful response text.
    """
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()
    cleaned: list[str] = []
    for line in lines:
        content = line.rstrip()
        # Skip lines that are entirely box-drawing / whitespace.
        if content and all(ch in _BOX_CHARS or ch in " \t" for ch in content):
            continue
        # Skip Codex status-bar lines (model · tokens · path pattern).
        if "\u00b7" in content and ("left" in content or "%" in content):
            continue
        cleaned.append(content)
    # Trim leading/trailing blank lines.
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


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

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        if session.metadata.get("codex_bootstrapped"):
            return
        health = host.health(session)
        if not health.healthy:
            raise RecoverableExecutionError(f"session unhealthy before bootstrap: {health.reason}")
        if self.tui_mode:
            host.send_text(session, self.cli_command, enter=True)
            # Poll the visible screen (alternate buffer) to detect gate
            # prompts and the Codex ready state.
            deadline = monotonic() + (self.idle_timeout_seconds or 60.0)
            while monotonic() < deadline:
                sleep(self.idle_poll_seconds)
                screen = _strip_ansi(host.read_visible(session))
                if self._looks_like_gate_prompt(screen):
                    host.send_text(session, "", enter=True)
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

    # Patterns that indicate a TUI gate/confirmation prompt.
    _GATE_PATTERNS = (
        "trust the contents",
        "do you trust",
        "press enter to continue",
        "yes, continue",
        "approve",
        "confirm",
        "permission",
        "allow",
    )

    def _looks_like_gate_prompt(self, text: str) -> bool:
        lower = text.lower()
        return any(pat in lower for pat in self._GATE_PATTERNS)

    @staticmethod
    def _looks_like_codex_ready(text: str) -> bool:
        """Return True when the visible screen shows the Codex input prompt."""
        # The Codex TUI shows › as the input prompt marker when ready.
        return "\u203a" in text

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

        cursor = host.create_cursor(session)
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={"adapter": self.kind, "session_id": session.session_id, "run_id": run_id},
        )
        host.send_text(session, prompt, enter=True)

        def _poll_hook() -> None:
            supervisor.check_interrupt(claimed)

        idle = host.wait_for_idle(
            session,
            poll_seconds=self.idle_poll_seconds,
            idle_after=self.idle_after,
            timeout_seconds=self.idle_timeout_seconds,
            on_poll=_poll_hook,
        )
        if not idle:
            raise RecoverableExecutionError("codex tui did not become idle within timeout")

        read = host.read_output(session, cursor)
        raw_output = read.text or read.full_text
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

        cursor = host.create_cursor(session)
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
