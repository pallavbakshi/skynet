"""Codex CLI agent adapter plugin."""
from __future__ import annotations
import json
import logging
import os
import shlex
from time import monotonic, sleep
from typing import Any

_logger = logging.getLogger(__name__)

from agp.plugins._output_contracts import (
    apply_output_contract_instruction,
    is_json_contract,
    prompt_for_claim,
    result_file_path_for_run,
    validate_json_against_contract,
)
from agp.plugins._structured_output import select_structured_result
from agp.plugins._provider_env import collect_provider_env
from agp.plugins._via_file import (
    build_task_file_content,
    cleanup_task_file,
    reference_string,
    write_task_file,
)
from agp.runtime import (
    AdapterExecutionFailed, AgentAdapter, ArtifactPayload, ExecutionResult,
    BootstrapFailure, ExecutionTimeout, PaneDied, RecoverableExecutionError,
    StableButIndeterminate, TerminalHost, TerminalSession,
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


def _repair_json_string(text: str) -> str:
    """Best-effort repair of unescaped double-quotes in LLM JSON output.

    Iteratively parses the text, finds the position where parsing fails
    (typically right after an unescaped interior quote that the parser
    mistook for a string terminator), escapes the offending quote, and
    retries.  Handles chains of unescaped quotes like ``"x": y`` inside
    a JSON string value.
    """
    repaired = text
    for _ in range(50):
        try:
            json.loads(repaired)
            return repaired
        except (json.JSONDecodeError, ValueError) as exc:
            pos = getattr(exc, "pos", None)
            if pos is None or pos <= 0:
                break
            # Walk backward from the error position to find the
            # unescaped " that the parser treated as a string close.
            fixed = False
            for j in range(pos - 1, 0, -1):
                if repaired[j] != '"':
                    continue
                # Count consecutive backslashes before this quote.
                # Even count (including zero) means the quote is unescaped.
                num_bs = 0
                while j - 1 - num_bs >= 0 and repaired[j - 1 - num_bs] == '\\':
                    num_bs += 1
                if num_bs % 2 != 0:
                    continue  # quote is already escaped
                repaired = repaired[:j] + '\\"' + repaired[j + 1:]
                fixed = True
                break
            if not fixed:
                break
    return repaired


def _extract_exec_response(text: str) -> str:
    """Extract the final model response from ``codex exec`` output.

    ``codex exec`` writes tool-use traces (file reads, command outputs)
    interleaved with model responses.  The model's final response
    appears after a bare ``codex`` marker line.  A duplicate of the JSON
    also appears as the very last non-empty line (clean stdout).

    We try multiple strategies in order of reliability:
    1. Find the ``codex`` marker line and return the text after it
    2. Find the last valid JSON object near the end of output
    3. Return a narrow tail for downstream extraction
    """
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()

    # Strategy 1: find the last bare "codex" marker line and return
    # the text between it and the next section/end.
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "codex":
            response_lines: list[str] = []
            for j in range(i + 1, len(lines)):
                ln = lines[j].strip()
                if ln in ("exec", "user", "tokens used"):
                    break
                response_lines.append(lines[j])
            result = "\n".join(response_lines).strip()
            if result:
                return result

    # Strategy 2: the last non-empty line of codex exec stdout is the
    # JSON response (duplicated from the interactive log).  Scan the
    # last ~20 lines backwards, looking for a line that is valid JSON.
    for i in range(len(lines) - 1, max(len(lines) - 20, -1), -1):
        ln = lines[i].strip()
        if not ln:
            continue
        if ln.startswith("{"):
            try:
                json.loads(ln)
                return ln
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 3: return a narrow tail for extract_trailing_json.
    return stripped[-4096:].strip()


def _extract_trailing_json_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    for idx in range(len(stripped) - 1, -1, -1):
        if stripped[idx] not in "[{":
            continue
        suffix = stripped[idx:]
        attempts = [
            suffix,
            "".join(line.strip() for line in suffix.splitlines()),
            " ".join(line.strip() for line in suffix.splitlines()),
        ]
        for attempt in attempts:
            for text_to_parse in (attempt, _repair_json_string(attempt)):
                try:
                    payload, end = decoder.raw_decode(text_to_parse)
                except json.JSONDecodeError:
                    continue
                if text_to_parse[end:].strip():
                    continue
                return json.dumps(payload, separators=(",", ":"))
    return None


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


def _candidate_codex_tui_result(candidate: str) -> tuple[str, bool, int, int]:
    stripped = _strip_ansi(candidate)
    answered_turns = [turn for turn in _parse_codex_turns(stripped) if turn["response"]]
    bullet_lines = _collect_bullet_lines(stripped.splitlines())
    cleaned = _clean_codex_tui_output(candidate).strip()
    return cleaned, bool(answered_turns or bullet_lines), len(answered_turns), len(bullet_lines)


def _extract_codex_tui_result(*candidates: str, baseline_last_response: str | None = None) -> str:
    """Return the most meaningful Codex TUI response from candidate transcripts."""
    fallback_candidates: list[str] = []
    answered_candidates: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        cleaned, has_answer, answered_turn_count, bullet_line_count = _candidate_codex_tui_result(candidate)
        if not cleaned:
            continue
        if has_answer:
            fresh = int(cleaned != (baseline_last_response or ""))
            # Caller order matters. If the visible pane shows a fresh answer,
            # trust it immediately; only fall back to accumulated output when
            # the visible pane is stale or does not contain an answer.
            if fresh:
                return cleaned
            answered_candidates.append(cleaned)
            continue
        fallback_candidates.append(cleaned)
    if answered_candidates:
        return answered_candidates[0]
    return fallback_candidates[0] if fallback_candidates else ""


def _select_codex_tui_transcript(*candidates: str, baseline_last_response: str | None = None) -> str:
    """Return the richest transcript that still contains an answered turn when possible."""
    fallback = ""
    answered_candidates: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        stripped = _strip_ansi(candidate)
        cleaned, has_answer, answered_turn_count, bullet_line_count = _candidate_codex_tui_result(stripped)
        if has_answer:
            fresh = int(cleaned != (baseline_last_response or ""))
            if fresh:
                return stripped
            answered_candidates.append(stripped)
            continue
        if not fallback:
            fallback = stripped
    if answered_candidates:
        return answered_candidates[0]
    return fallback


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
            # In ephemeral mode on tmux, skip persistent bootstrap — each
            # execute_run invokes codex as a one-shot process via launch_command.
            # In sticky mode, fall through to launch a persistent TUI.
            if host.kind == "tmux" and self.session_mode != "sticky":
                session.metadata["codex_bootstrapped"] = True
                return
            # If the TUI is already running in this pane (e.g. reused session
            # from a prior process), skip launching and just set the flag.
            if hasattr(host, "is_foreground_tui") and host.is_foreground_tui(session):
                session.metadata["codex_bootstrapped"] = True
                return
            host.launch_command(
                session,
                command=self.cli_command,
                env=collect_provider_env(),
                cwd=session.workspace_ref,
            )
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

        # Use _visible_ends_with_prompt for the idle-prompt check — it
        # correctly handles noise lines and other TUI chrome that a naive
        # "last meaningful line" scan would trip over.
        if not self._visible_ends_with_prompt(text):
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

    @staticmethod
    def _looks_like_working(text: str) -> bool:
        """Return True when Codex shows an active Working indicator.

        Only scans the BOTTOM of the screen (last ~5 meaningful lines) to avoid
        false positives from response content that quotes working indicators.

        Note: "Working (" is in _NOISE_PREFIXES for content extraction, but it
        IS the primary signal here.  We check for it BEFORE noise filtering so
        it doesn't get silently discarded.
        """
        meaningful = 0
        for line in reversed(text.splitlines()):
            s = line.strip()
            if not s:
                continue
            # Check working indicators BEFORE noise filtering — "Working ("
            # is classified as noise for content extraction but is the exact
            # signal this function needs to detect.
            if s.startswith("Working ("):
                return True
            if "esc to interrupt" in s.lower():
                return True
            if _is_noise_line(line):
                continue
            meaningful += 1
            if meaningful >= 5:
                break
        return False

    @staticmethod
    def _visible_ends_with_prompt(text: str) -> bool:
        """Return True when the last meaningful line on screen is a prompt marker."""
        for raw in reversed(_strip_ansi(text).splitlines()):
            s = raw.strip()
            if not s or _is_noise_line(raw):
                continue
            return s.startswith(_PROMPT_MARKER)
        return False

    @staticmethod
    def _screen_tail(text: str, n: int = 10) -> str:
        """Return the last N non-empty, non-noise lines of the visible screen.

        Noise lines (status indicators, token counts) are excluded so that
        transient display updates do not reset the stability timer.
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        filtered = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if _is_noise_line(ln):
                continue
            filtered.append(ln.rstrip())
        return "\n".join(filtered[-n:])

    def _idle_timeout_window(self) -> float:
        if self.idle_timeout_seconds > 0:
            return self.idle_timeout_seconds
        if self.idle_timeout_polls > 0:
            poll_seconds = self.idle_poll_seconds if self.tui_mode else self.poll_interval_seconds
            return max(0.0, self.idle_timeout_polls * poll_seconds)
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
        output_chars: int = 0,
        output_delta: str = "",
        tui_state: str = "",
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
                output_chars=output_chars,
                output_delta=output_delta,
                tui_state=tui_state,
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
        output_chars: int = 0,
        output_delta: str = "",
        tui_state: str = "",
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
        details["output_chars"] = output_chars
        last_line = ""
        if output_delta:
            for ln in reversed(output_delta.splitlines()):
                stripped = _strip_ansi(ln).strip()
                if not stripped:
                    continue
                # Skip noise lines (status bar, token counts, etc.) so
                # downstream consumers (e.g. agp wait) see real progress.
                if _is_noise_line(stripped):
                    continue
                # A bare prompt marker is not useful progress info.
                if stripped == _PROMPT_MARKER:
                    continue
                last_line = stripped[:80]
                break
        details["last_line"] = last_line
        details["tui_state"] = tui_state
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
        run_id = claimed["run"]["run_id"]
        try:
            if self.tui_mode:
                return self._execute_run_tui(host=host, session=session, claimed=claimed, supervisor=supervisor)
            return self._execute_run_marker(host=host, session=session, claimed=claimed, supervisor=supervisor)
        finally:
            cleanup_task_file(run_id)
            # Clean up schema + stdout files written for --output-schema.
            try:
                import os as _os
                schema_dir = f"/tmp/agp-schemas-{_os.getuid()}"
                for fname in (f"agp-schema-{run_id}.json", f"agp-stdout-{run_id}.json"):
                    __import__("pathlib").Path(f"{schema_dir}/{fname}").unlink(missing_ok=True)
            except Exception:
                pass

    def _execute_run_tui(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        """TUI mode: send prompt -> wait for idle -> read delta -> clean output."""
        run_id = claimed["run"]["run_id"]
        # For JSON contract jobs, instruct the LLM to also write its result to a
        # file so we don't depend solely on terminal capture for long responses.
        contract = (claimed.get("job") or {}).get("output_contract_json")
        json_contract = is_json_contract(contract)
        result_file = result_file_path_for_run(run_id) if json_contract else None
        # Pre-delete any stale file from a prior attempt so we don't read old data.
        if result_file:
            try:
                __import__("pathlib").Path(result_file).unlink(missing_ok=True)
            except Exception:
                pass
        prompt = apply_output_contract_instruction(
            prompt=claimed["message"]["text"], claimed=claimed,
            result_file_path=result_file,
        )

        # Session reset depends on session_mode:
        # - ephemeral: always reset (fresh TUI per job)
        # - sticky: keep the TUI alive across jobs (history preserved)
        if self.session_mode == "ephemeral":
            session = host.reset_session(session)
            session.metadata.pop('restored_cursor', None)
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
        startup_settled_event = session.metadata.get("startup_settled_event") or getattr(
            supervisor, "_active_startup_settled", None,
        )
        setattr(supervisor, "_active_session", session)
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={"adapter": self.kind, "session_id": session.session_id, "run_id": run_id},
        )
        # Via-file delivery: write the full prompt + metadata to a temp file
        # and send a short reference string. Avoids paste buffer corruption,
        # CLI argument length limits, and special character mangling.
        task_file_content = build_task_file_content(prompt=prompt, claimed=claimed)
        task_file_path = write_task_file(run_id=run_id, content=task_file_content)
        dispatch_text = reference_string(task_file_path)

        # For JSON contract jobs in ephemeral mode, write the schema to a
        # temp file and use `codex exec --output-schema` to enforce structured
        # output at the API level.  Appending "respond with JSON" to the prompt
        # doesn't work — codex's system prompt overrides user-level instructions,
        # but --output-schema sets strict JSON mode via the Responses API.
        schema_file = None
        if json_contract and host.kind == "tmux" and self.session_mode != "sticky":
            import os as _os
            schema_dir = f"/tmp/agp-schemas-{_os.getuid()}"
            _os.makedirs(schema_dir, mode=0o700, exist_ok=True)
            schema_file = f"{schema_dir}/agp-schema-{run_id}.json"
            schema_content = contract.get("json_schema") or {}
            __import__("pathlib").Path(schema_file).write_text(
                json.dumps(schema_content), encoding="utf-8",
            )

        # For exec mode, capture stdout to a file.  codex exec prints the
        # JSON response to stdout and the interactive session log (tool traces,
        # "codex" marker, "tokens used") to stderr.  tmux merges both into the
        # pane, making it hard to extract the JSON from the scrollback.
        # Redirecting stdout gives us a clean, reliable source.
        exec_stdout_file = None
        if schema_file:
            exec_stdout_file = f"{os.path.dirname(schema_file)}/agp-stdout-{run_id}.json"

        if host.kind == "tmux" and self.session_mode != "sticky":
            # Ephemeral on tmux: one-shot codex invocation per job.
            if schema_file:
                # Use exec mode with --output-schema for API-level JSON enforcement.
                # Insert "exec" after the codex binary and add --output-schema.
                parts = shlex.split(self.cli_command)
                parts.insert(1, "exec")
                base_cmd = " ".join(shlex.quote(p) for p in parts)
                cmd = (f"{base_cmd}"
                       f" --output-schema {shlex.quote(schema_file)}"
                       f" {shlex.quote(dispatch_text)}"
                       f" > {shlex.quote(exec_stdout_file)}")
            else:
                cmd = f"{self.cli_command} {shlex.quote(dispatch_text)}"
            host.launch_command(
                session,
                command=cmd,
                env=collect_provider_env(),
                cwd=session.workspace_ref,
            )
        else:
            # Sticky (any host) or non-tmux: send prompt to persistent TUI.
            host.send_text(session, dispatch_text, enter=True)

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
        indeterminate_polls = 0
        tui_active = False  # True once the TUI has drawn at least one frame
        dispatch_time = monotonic()
        last_heartbeat_at = dispatch_time
        poll_count = 0
        accumulated_turns_above_baseline = 0
        last_good_screen = ""  # last screen that had meaningful TUI content
        exec_empty_shell_polls = 0  # consecutive polls where shell returned but stdout is 0 bytes
        while monotonic() < idle_deadline:
            poll_count += 1
            sleep(self.idle_poll_seconds)
            _poll_hook()
            screen = _strip_ansi(host.read_visible(session))
            read = host.read_output(session, cursor)
            cursor = read.cursor
            answered_turns = [turn for turn in _parse_codex_turns(read.full_text) if turn["response"]]
            accumulated_turns_above_baseline = max(
                accumulated_turns_above_baseline,
                len(answered_turns) - len(baseline_turns),
            )
            snap = self._normalise_visible_screen(screen)
            tail = self._screen_tail(screen)
            changed = bool(read.changed or snap != prev_screen or tail != prev_tail)
            now = monotonic()
            # Derive semantic tui_state for structured consumption.
            tui_state = "unknown"
            if self._looks_like_onboarding_prompt(screen):
                tui_state = "gate.fatal"
            elif self._looks_like_gate_prompt(screen):
                tui_state = "gate.auto"
            elif self._looks_like_completed_turn(
                screen,
                baseline_answered_turns=len(baseline_turns),
                baseline_last_response=baseline_last_response,
            ):
                tui_state = "completed"
            elif self._looks_like_working(screen):
                tui_state = "working"
            elif self._visible_ends_with_prompt(screen):
                tui_state = "ready"
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
                output_chars=len(read.full_text),
                output_delta=read.text,
                tui_state=tui_state,
            )
            # Only check for shell return after the TUI has had time to
            # render.  During the first few seconds the old shell prompt
            # may still be visible in scrollback while the TUI boots.
            startup_settled = tui_active or (now - dispatch_time > 5.0)
            if startup_settled_event is not None and startup_settled:
                startup_settled_event.set()
            if startup_settled and self._looks_like_shell_returned(screen):
                if accumulated_turns_above_baseline > 0:
                    break
                # The scrollback may not have captured turns that the
                # visible screen showed before the TUI exited.  Check
                # last_good_screen for a completed turn.
                if last_good_screen and self._looks_like_completed_turn(
                    last_good_screen,
                    baseline_answered_turns=len(baseline_turns),
                    baseline_last_response=baseline_last_response,
                ):
                    break
                # For exec mode (JSON contract jobs), the process exits after
                # producing output to stdout.  The stdout redirect creates
                # the file (0 bytes) before codex starts, so we must check
                # that the file has content before concluding completion.
                if schema_file:
                    if exec_stdout_file:
                        try:
                            sz = __import__("pathlib").Path(exec_stdout_file).stat().st_size
                        except Exception:
                            sz = 0
                        if sz > 0:
                            break
                        # Shell returned but stdout file is empty — codex may
                        # still be starting (> truncates first) or it failed.
                        exec_empty_shell_polls += 1
                        if exec_empty_shell_polls >= 5:
                            # 5 consecutive polls (~10s) with shell back and
                            # 0-byte stdout → exec command failed.
                            raise AdapterExecutionFailed(
                                "codex exec exited without producing output "
                                "(stdout file is empty after shell returned)",
                                transcript="",
                                output=screen,
                            )
                        continue
                    break
                # Guard against false positives during CLI startup: the
                # visible buffer may still show the pre-launch shell
                # prompt while the CLI is loading.  Use process-based
                # detection as a tiebreaker — but only when the host
                # supports it (_get_pane_tty returns a TTY).
                if hasattr(host, "_get_pane_tty") and host._get_pane_tty(session) is not None and not host.shell_idle(session):
                    continue
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
                indeterminate_polls = 0
                exec_empty_shell_polls = 0
                tui_active = True
                # Only extend idle deadline on actual content progress
                # (tail changes), not on full-screen diffs from TUI
                # repaints that shift absolute scrollback lines.
                idle_deadline = now + timeout
            prev_screen = snap
            prev_tail = tail
            # Preserve the last screen that has TUI content for extraction,
            # because the TUI may exit between loop break and read_visible.
            if _PROMPT_MARKER in screen or _RESPONSE_MARKER in screen:
                last_good_screen = screen

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
            # Fallback 1: scrollback-based turn count.  When the response is
            # long enough to scroll the • markers off the visible screen,
            # _looks_like_completed_turn fails because _parse_codex_turns
            # finds no turns.  Use accumulated scrollback turns instead.
            if accumulated_turns_above_baseline > 0 and self._visible_ends_with_prompt(screen):
                break
            # Fallback 2: idle prompt + observed activity.  The scrollback
            # turn count can be 0 in the alternate screen buffer (tmux
            # reports history_size=0).  If the TUI was active at some point
            # and now shows an idle › prompt, the response is complete
            # regardless of whether we can parse the turn structure.
            if tui_active and self._visible_ends_with_prompt(screen):
                break

            # Exec mode fallback: codex exec exits after producing output,
            # returning to the shell.  The visible screen may still show
            # exec output (no › prompt, no shell markers visible) but the
            # foreground process is back to the shell.
            #
            # shell_idle alone is not enough: the > redirect truncates the
            # stdout file before codex starts, so a premature shell_idle
            # detection would see a 0-byte file.  Require the stdout file
            # to be non-empty as proof that codex actually finished.
            if schema_file and tui_active and host.shell_idle(session):
                if exec_stdout_file:
                    try:
                        sz = __import__("pathlib").Path(exec_stdout_file).stat().st_size
                    except Exception:
                        sz = 0
                    if sz > 0:
                        break
                    # Shell idle but stdout file empty — same counter as
                    # shell_returned path to detect exec failure.
                    exec_empty_shell_polls += 1
                    if exec_empty_shell_polls >= 5:
                        raise AdapterExecutionFailed(
                            "codex exec exited without producing output "
                            "(stdout file is empty after shell idle)",
                            transcript="",
                            output=screen,
                        )
                    continue
                break

            # None of the checks could determine the state.  The screen is
            # stable, the agent isn't visibly working, but we can't tell if
            # it completed, is waiting for input, or is stuck.  Give it a
            # few polls of grace then escalate to the caller.
            # Only escalate when there is meaningful content on screen —
            # an empty pane should fall through to ExecutionTimeout instead.
            if not screen.strip():
                continue
            indeterminate_polls += 1
            if indeterminate_polls == 1:
                turns = _parse_codex_turns(screen)
                answered = [t for t in turns if t["response"]]
                _logger.warning(
                    "indeterminate state entered: turns=%d answered=%d "
                    "baseline_turns=%d accumulated_scrollback=%d "
                    "tui_active=%s visible_prompt=%s tail=%r",
                    len(turns), len(answered), len(baseline_turns),
                    accumulated_turns_above_baseline, tui_active,
                    self._visible_ends_with_prompt(screen),
                    self._screen_tail(screen)[-100:],
                )
            # ~10 seconds of grace (5 polls × 2s) before escalating.
            if indeterminate_polls >= 5:
                raise StableButIndeterminate(
                    "screen is stable but adapter cannot determine if the "
                    "agent completed, is waiting for input, or is stuck",
                    screen=screen,
                    last_good_screen=last_good_screen,
                )
        else:
            if not prev_screen.strip():
                raise ExecutionTimeout("codex tui produced no output after dispatch")
            raise ExecutionTimeout("codex tui did not become idle within timeout")

        # Prefer the visible TUI buffer for result extraction because tmux's
        # accumulated scrollback can lag behind repaint-only responses and end
        # up containing transient status lines instead of the final answer.
        # Also use last_good_screen as a candidate because the TUI may exit
        # between the loop break and this read_visible call.
        if schema_file:
            # Exec mode: give tmux a moment to flush the final stdout line
            # (the JSON response).  codex exec prints tool traces to stderr
            # and the JSON result to stdout; the stdout line may not be in
            # the scrollback yet when shell_idle fires.
            sleep(0.5)
        visible_output = _strip_ansi(host.read_visible(session))
        read = host.read_output(session, cursor)
        raw_output = _strip_ansi(read.full_text)
        # For exec mode, also read the full scrollback — the cursor-based
        # delta may have missed the final JSON line.
        full_scrollback = ""
        if schema_file:
            try:
                full_scrollback = _strip_ansi(host.read_scrollback(session))
            except Exception:
                pass
        # Preserve the updated cursor so the next sticky run starts from
        # this point instead of creating a fresh baseline.
        session.metadata["restored_cursor"] = read.cursor
        extraction_diag = None
        if schema_file:
            # Exec mode: codex exec outputs tool traces (file reads, command
            # outputs — potentially huge diffs) followed by the JSON response.
            # The TUI parsing pipeline (_clean_codex_tui_output) expects › / •
            # markers that don't exist in exec mode, so it falls back to
            # returning ALL non-noise lines including tool output.  This feeds
            # hundreds of lines of code with { and [ to extract_trailing_json
            # which then picks up the wrong JSON fragment.
            #
            # Primary source: the stdout capture file.  codex exec prints
            # its JSON response to stdout; we redirected it to a file to
            # avoid losing it in the tmux scrollback noise.
            # Retry a few times — the OS may not have flushed the write yet
            # even though the process has exited (shell_idle = True).
            exec_stdout = ""
            if exec_stdout_file:
                for _attempt in range(5):
                    try:
                        raw = __import__("pathlib").Path(exec_stdout_file).read_text(encoding="utf-8").strip()
                        if raw:
                            exec_stdout = raw
                            break
                    except Exception:
                        pass
                    sleep(0.5)

            # Fallback: extract from scrollback/visible using the "codex"
            # marker line or by finding the last valid JSON near the end.
            exec_response = _extract_exec_response(
                full_scrollback or raw_output or visible_output,
            )
            cleaned_sources: list[tuple[str, str]] = []
            # stdout capture is the most reliable source
            if exec_stdout:
                cleaned_sources.append(("exec_stdout", exec_stdout))
            cleaned_sources.append(("exec_response", exec_response))
            # Also try the narrow tail of each source
            for tag, src in [("scrollback", full_scrollback), ("raw", raw_output), ("visible", visible_output)]:
                if src:
                    cleaned_sources.append((f"{tag}_tail", src[-4096:]))
            selected, extraction_diag = select_structured_result(
                result_file=result_file,
                cleaned_sources=cleaned_sources,
                claimed=claimed,
            )
            cleaned = selected or exec_response
        else:
            cleaned = _extract_codex_tui_result(
                visible_output,
                last_good_screen,
                raw_output,
                baseline_last_response=baseline_last_response,
            )
            if json_contract:
                # TUI mode with output contract: build cleaned sources from
                # TUI-parsed content for the shared extraction pipeline.
                cleaned_sources = [
                    ("cleaned", cleaned),
                ]
                if raw_output:
                    raw_cleaned = _clean_codex_tui_output(raw_output)
                    if raw_cleaned:
                        cleaned_sources.append(("raw_cleaned", raw_cleaned))
                for tag, src in [("visible", visible_output), ("last_good", last_good_screen), ("raw", raw_output)]:
                    if src:
                        cleaned_sources.append((f"{tag}_tui_cleaned", _clean_codex_tui_output(src)))
                        bullet_text = "\n".join(_collect_bullet_lines(_strip_ansi(src).splitlines()))
                        if bullet_text:
                            cleaned_sources.append((f"{tag}_bullets", bullet_text))
                selected, extraction_diag = select_structured_result(
                    result_file=result_file,
                    cleaned_sources=cleaned_sources,
                    claimed=claimed,
                )
                if selected:
                    cleaned = selected
        transcript_output = _select_codex_tui_transcript(
            visible_output,
            last_good_screen,
            raw_output,
            baseline_last_response=baseline_last_response,
        )

        if not cleaned.strip():
            raise ExecutionTimeout("codex tui produced no output after idle")

        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                ArtifactPayload(role="prompt", name="task-file.md", content=task_file_content),
                ArtifactPayload(role="transcript_log", name="transcript.txt", content=_select_codex_tui_transcript(
                    _strip_ansi(host.read_scrollback(session)),
                    visible_output, last_good_screen, raw_output,
                    baseline_last_response=baseline_last_response,
                )),
                ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                ArtifactPayload(role="result", name="result.txt", content=cleaned),
            ],
            summary={"adapter": self.kind, "host": host.kind, "run_id": run_id, "mode": "tui",
                      "dispatch": "via_file"},
            diagnostics=extraction_diag.to_dict() if extraction_diag else None,
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
        prompt = apply_output_contract_instruction(prompt=claimed["message"]["text"], claimed=claimed)
        run_id = claimed["run"]["run_id"]

        health = host.health(session)
        if not health.healthy:
            raise PaneDied(f"session unhealthy at dispatch: {health.reason}")

        # Via-file: write the task payload to a file and send a reference.
        # The marker envelope includes the reference string instead of the
        # full prompt, avoiding paste buffer issues for long prompts.
        task_file_content = build_task_file_content(prompt=prompt, claimed=claimed)
        task_file_path = write_task_file(run_id=run_id, content=task_file_content)
        via_file_prompt = reference_string(task_file_path)
        cursor = session.metadata.pop("restored_cursor", None) or host.create_cursor(session)
        host.send_text(session, self._task_payload(run_id=run_id, prompt=via_file_prompt), enter=True)
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
                    output_chars=len(read.full_text),
                    output_delta=read.text,
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
                            ArtifactPayload(role="prompt", name="task-file.md", content=task_file_content),
                            ArtifactPayload(role="transcript_log", name="transcript.txt", content=transcript),
                            ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                            ArtifactPayload(role="result", name="result.txt", content=result_text or json.dumps(payload, sort_keys=True)),
                        ],
                        summary={"adapter": self.kind, "host": host.kind, "run_id": run_id,
                                  "dispatch": "via_file"},
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
                    output_chars=len(read.full_text),
                    output_delta=read.text,
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
        if isinstance(error, StableButIndeterminate):
            screen = error.screen or _strip_ansi(host.read_visible(session))
            cleaned = _clean_codex_tui_output(error.last_good_screen or screen)
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
        result = super().build_failure_result(
            host=host,
            session=session,
            claimed=claimed,
            error=error,
            supervisor=supervisor,
        )
        result.artifacts.extend(self._salvage_timeout_artifacts(host=host, session=session, error=error))
        return result
