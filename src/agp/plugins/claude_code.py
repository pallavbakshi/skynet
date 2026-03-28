"""Claude Code CLI agent adapter plugin."""
from __future__ import annotations
import re
from time import monotonic, sleep
from typing import Any

from agp.runtime import (
    AdapterExecutionFailed, AgentAdapter, ArtifactPayload, ExecutionResult,
    RecoverableExecutionError, TerminalHost, TerminalSession,
    _strip_ansi,
)

# ── Claude Code TUI markers ─────────────────────────────────────────
_PROMPT_PREFIX = "\u276f"       # ❯ = user prompt
_RESPONSE_PREFIXES = (
    "\u23fa",  # ⏺ older Claude Code response marker
    "\u25cf",  # ● newer Claude Code response marker
)
_TOOL_RESULT_PREFIX = "\u23bf"  # ⎿ = tool result / continuation
_SEPARATOR_RE = re.compile(r"^\u2500{4,}$")  # ────
_COMPACTION_RE = re.compile(r"^\u273b\s+(Conversation compacted|Churned)")  # ✻
_FEEDBACK_RE = re.compile(r"how is claude doing", re.IGNORECASE)
_WELCOME_START = "\u256d"  # ╭
_WELCOME_END = "\u2570"    # ╰
_STATUS_BAR_RE = re.compile(r"^\s*\u23f5\u23f5\s+")  # ⏵⏵ (any status bar variant)
_BOX_CHARS = set("\u2500\u2502\u256d\u256e\u256f\u2570\u2514\u250c\u2510\u2518\u2524\u251c\u252c\u2534\u253c\u2501\u2503")

# Lines matching these are TUI chrome, not content.
_NOISE_PREFIXES = (
    "\u256d",  # ╭ welcome box top
    "\u2570",  # ╰ welcome box bottom
    "\u2502",  # │ welcome box side
    "\u23f5\u23f5",  # ⏵⏵ status bar
)


def _is_noise_line(line: str) -> bool:
    """Return True for TUI chrome / noise lines.

    Blank lines are NOT noise — they are preserved as paragraph breaks.
    """
    s = line.strip()
    if not s:
        return False
    if _SEPARATOR_RE.match(s):
        return True
    if _STATUS_BAR_RE.match(s):
        return True
    if _FEEDBACK_RE.search(s):
        return True
    # Feedback survey options: "1: Bad    2: Fine   3: Good   0: Dismiss"
    if s.startswith("1:") and "dismiss" in s.lower():
        return True
    for prefix in _NOISE_PREFIXES:
        if s.startswith(prefix):
            return True
    if all(ch in _BOX_CHARS or ch in " \t" for ch in s):
        return True
    return False


def _is_response_line(line: str) -> bool:
    """Return True when a line starts with a Claude response marker."""
    s = line.strip()
    return any(s.startswith(prefix) for prefix in _RESPONSE_PREFIXES)


def _response_content(line: str) -> str:
    """Return response content with the Claude response marker removed."""
    s = line.strip()
    for prefix in _RESPONSE_PREFIXES:
        if s.startswith(prefix):
            return s.removeprefix(prefix).strip()
    return s


def _clean_claude_code_output(text: str) -> str:
    """Extract the last Claude Code response from raw TUI output.

    Parses the TUI structure using ❯ (prompt) and Claude response markers,
    strips all chrome (box borders, status bar, separators, system lines),
    and returns only the response text from the most recent turn.
    """
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()

    # Handle compaction: only look at post-compaction lines when possible.
    comp_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] and _COMPACTION_RE.match(lines[i].strip()):
            comp_idx = i
            break
    if comp_idx >= 0:
        post = lines[comp_idx + 1:]
        if any(ln and _is_response_line(ln) for ln in post):
            lines = post

    # Find turns: each turn starts with a ❯ prompt line.
    turns: list[dict[str, Any]] = []
    current_prompt = ""
    response_lines: list[str] = []
    in_response = False

    for line in lines:
        s = line.strip()
        if s.startswith(_PROMPT_PREFIX):
            if in_response and response_lines:
                turns.append({"prompt": current_prompt, "response": list(response_lines)})
            current_prompt = s.removeprefix(_PROMPT_PREFIX).strip()
            response_lines = []
            in_response = False
        elif _is_response_line(line):
            in_response = True
            content = _response_content(line)
            if content and not _is_noise_line(content):
                response_lines.append(content)
        elif s.startswith(_TOOL_RESULT_PREFIX):
            if in_response:
                content = s.removeprefix(_TOOL_RESULT_PREFIX).strip()
                if content:
                    response_lines.append(f"  \u23bf {content}")
        elif in_response and not _is_noise_line(line):
            raw = line
            if raw.startswith("  "):
                response_lines.append(raw[2:].rstrip())
            else:
                response_lines.append(s)

    # Capture the last open turn.
    if in_response and response_lines:
        turns.append({"prompt": current_prompt, "response": list(response_lines)})

    if not turns:
        # Fallback: collect all Claude response-prefixed lines as content.
        response_lines = []
        for line in lines:
            if _is_response_line(line):
                content = _response_content(line)
                if content:
                    response_lines.append(content)
        if response_lines:
            return "\n".join(response_lines)
        # Last resort: strip noise and return whatever is left.
        fallback = [ln.rstrip() for ln in lines if not _is_noise_line(ln)]
        while fallback and not fallback[0]:
            fallback.pop(0)
        while fallback and not fallback[-1]:
            fallback.pop()
        return "\n".join(fallback)

    # Find the last turn that has actual response content.
    for turn in reversed(turns):
        content = [ln for ln in turn["response"] if ln.strip()]
        if content:
            while content and not content[-1]:
                content.pop()
            return "\n".join(content)

    # All turns were noise — fall back to collecting all response lines.
    response_lines = []
    for line in lines:
        if _is_response_line(line):
            content = _response_content(line)
            if content:
                response_lines.append(content)
    return "\n".join(response_lines)


def _parse_claude_code_turns(text: str) -> list[dict[str, Any]]:
    """Parse visible Claude Code TUI output into prompt/response turns."""
    stripped = _strip_ansi(text)
    lines = stripped.splitlines()

    # Apply compaction trimming so mid-run compactions don't inflate
    # the turn count with stale pre-compaction content.
    comp_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] and _COMPACTION_RE.match(lines[i].strip()):
            comp_idx = i
            break
    if comp_idx >= 0:
        post = lines[comp_idx + 1:]
        if any(ln and _is_response_line(ln) for ln in post):
            lines = post
    turns: list[dict[str, Any]] = []
    current_prompt = ""
    response_lines: list[str] = []
    in_response = False

    for line in lines:
        s = line.strip()
        if s.startswith(_PROMPT_PREFIX):
            if in_response and response_lines:
                turns.append({"prompt": current_prompt, "response": list(response_lines)})
            current_prompt = s.removeprefix(_PROMPT_PREFIX).strip()
            response_lines = []
            in_response = False
        elif _is_response_line(line):
            in_response = True
            content = _response_content(line)
            if content and not _is_noise_line(content):
                response_lines.append(content)
        elif s.startswith(_TOOL_RESULT_PREFIX):
            if in_response:
                content = s.removeprefix(_TOOL_RESULT_PREFIX).strip()
                if content:
                    response_lines.append(content)
        elif in_response and not _is_noise_line(line):
            response_lines.append(s)

    if in_response and response_lines:
        turns.append({"prompt": current_prompt, "response": list(response_lines)})
    return turns


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

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        cleaned = _clean_claude_code_output(text)
        screen = _strip_ansi(text)
        return {
            "adapter_kind": self.kind,
            "mode": "tui",
            "run_id": run_id,
            "cleaned_output": cleaned,
            "looks_like_ready": self._looks_like_ready(screen),
            "looks_like_gate_prompt": self._looks_like_gate_prompt(screen),
            "looks_like_shell_returned": self._looks_like_shell_returned(screen),
            "supported": True,
        }

    def ensure_bootstrapped(self, *, host: TerminalHost, session: TerminalSession, claimed: dict[str, Any]) -> None:  # noqa: ARG002
        if session.metadata.get("claude_code_bootstrapped"):
            # In sticky mode, verify the TUI is still alive before skipping.
            if hasattr(host, "is_foreground_tui"):
                if not host.is_foreground_tui(session):
                    session.metadata.pop("claude_code_bootstrapped", None)
                else:
                    return
            elif host.kind == "tmux":
                session.metadata.pop("claude_code_bootstrapped", None)
            else:
                return

        health = host.health(session)
        if not health.healthy:
            raise RecoverableExecutionError(f"session unhealthy before bootstrap: {health.reason}")

        # If the TUI is already running in a reused pane, skip launching.
        if hasattr(host, "is_foreground_tui") and host.is_foreground_tui(session):
            session.metadata["claude_code_bootstrapped"] = True
            return

        # Launch Claude Code interactively with permissions bypassed
        # so tool-use prompts don't block autonomous execution.
        host.send_text(session, f"{self.cli_command} --dangerously-skip-permissions", enter=True)

        deadline = monotonic() + (self.idle_timeout_seconds or 60.0)
        while monotonic() < deadline:
            sleep(self.idle_poll_seconds)
            screen = _strip_ansi(host.read_visible(session))
            if self._looks_like_gate_prompt(screen):
                if self._is_fatal_gate(screen):
                    raise RecoverableExecutionError(
                        "claude code requires interactive login — complete OAuth "
                        "setup in the container and re-commit the image"
                    )
                host.send_text(session, self._gate_response(screen), enter=True)
                continue
            if self._looks_like_ready(screen):
                break
        else:
            raise RecoverableExecutionError("claude code did not become ready after launch")

        if self.bootstrap_settle_seconds > 0:
            sleep(self.bootstrap_settle_seconds)
            health = host.health(session)
            if not health.healthy:
                raise RecoverableExecutionError(f"session unhealthy after bootstrap: {health.reason}")

        session.metadata["claude_code_bootstrapped"] = True

    # ── TUI detection helpers ────────────────────────────────────────

    # Box-drawing and TUI-unique characters that distinguish the Claude
    # Code TUI from a shell that also uses ❯ as its prompt marker.
    _TUI_BOX_CHARS = set("\u256d\u256e\u256f\u2570\u2502\u2500")  # ╭╮╯╰│─
    _TUI_CONTENT_HINTS = (
        "claude code",
        "bypass permissions",
        "tokens",
        "conversation compacted",
    )

    # Shell prompt characters (excluding ❯ which is ambiguous).
    _SHELL_MARKERS = {"$", "%", "#"}

    @staticmethod
    def _looks_like_ready(text: str) -> bool:
        """Return True when the visible screen shows the Claude Code idle prompt.

        Requires ❯ plus at least one definitive TUI indicator (separator,
        welcome box, or status bar) to avoid false positives from shells
        that also use ❯ as a prompt.
        """
        if _PROMPT_PREFIX not in text:
            return False
        for line in text.strip().splitlines():
            s = line.strip()
            if _SEPARATOR_RE.match(s):
                return True
            if s.startswith(_WELCOME_START) or s.startswith(_WELCOME_END):
                return True
            if _STATUS_BAR_RE.match(s):
                return True
            if _is_response_line(line):
                return True
        return False

    @staticmethod
    def _looks_like_working(text: str) -> bool:
        """Return True when Claude Code still shows an active working state."""
        for line in text.splitlines():
            s = line.strip().lower()
            if not s:
                continue
            if s.startswith("\u2234 ") and any(
                word in s for word in ("thinking", "working", "analy", "planning")
            ):
                return True
            if s.startswith("thinking...") or s.startswith("thinking…"):
                return True
        return False

    @staticmethod
    def _screen_tail(text: str, n: int = 10) -> str:
        """Return the last N non-empty lines of the visible screen."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        lines = [ln.rstrip() for ln in lines if ln.strip()]
        return "\n".join(lines[-n:])

    def _looks_like_shell_returned(self, text: str) -> bool:
        """Return True when the CLI exited and a shell prompt is visible.

        Checks the last 5 non-empty lines for shell markers while
        ruling out TUI indicators that would mean Claude Code is still
        rendering.
        """
        lines = text.strip().splitlines()
        tail = [ln.strip() for ln in lines[-5:] if ln.strip()]
        if not tail:
            return False

        has_shell = any(ln[0] in self._SHELL_MARKERS for ln in tail if ln)
        if not has_shell:
            return False

        # If any TUI indicators are present, the TUI is still alive.
        # ❯ is ambiguous (used by both the TUI and some shells like
        # Starship/Pure), so check if it appears alongside a definitive
        # TUI indicator — that means the TUI is running.
        has_tui_indicator = False
        for ln in tail:
            if any(ch in self._TUI_BOX_CHARS for ch in ln):
                return False
            if _SEPARATOR_RE.match(ln):
                has_tui_indicator = True
            if _is_response_line(ln):
                return False
            if _STATUS_BAR_RE.match(ln):
                return False
        # ❯ + any TUI indicator = TUI alive, not shell
        if has_tui_indicator and any(_PROMPT_PREFIX in ln for ln in tail):
            return False

        lower_tail = "\n".join(tail).lower()
        if any(hint in lower_tail for hint in self._TUI_CONTENT_HINTS):
            return False

        return True

    # ── Gate screen classification ─────────────────────────────────────
    #
    # AUTO gates are dismissed automatically (Enter or numbered choice).
    # FATAL gates require user action — the adapter raises an error so
    # the job fails with a clear message instead of timing out silently.

    _AUTO_GATE_PATTERNS = (
        # First-run setup (auto-dismiss)
        "choose the text style",          # theme picker → Enter (accept default)
        "syntax highlighting",            # theme preview → Enter
        # Login success / security notes (auto-dismiss with Enter)
        "login successful",               # "Logged in as … Press Enter to continue"
        "press enter to continue",        # generic continue prompt
        "security notes",                 # security reminder → Enter
        # Feedback survey (dismiss immediately)
        "how is claude doing",            # → 0 (Dismiss)
        # Bypass permissions confirmation
        "bypass permissions mode",        # → 2 (Yes, I accept)
        "accept all responsibility",      # same screen, alternate match
        # Trust prompts
        "yes, i trust this folder",       # workspace trust → 1
        "i trust this folder",            # alternate phrasing
        "i trust this project",           # alternate phrasing
        "trust the contents",             # alternate phrasing
        "quick safety check",             # workspace trust intro
        # Permission prompts (fallback if --dangerously-skip-permissions not active)
        "allow tool",
        "allow bash",
        "allow read",
        "allow edit",
        "allow write",
        "(y/n)",
    )

    _FATAL_GATE_PATTERNS = (
        # Login required — needs browser-based OAuth, cannot auto-resolve.
        "select login method",
        "paste code here",
        "browser didn't open",
        "oauth error",
    )

    # Preferred choices for auto-dismiss numbered gate menus.
    # Empty string = press Enter to accept the default selection.
    _GATE_CHOICES = {
        "choose the text style": "",      # accept default theme (Dark mode)
        "syntax highlighting": "",        # dismiss theme preview
        "login successful": "",           # dismiss login confirmation
        "security notes": "",             # dismiss security reminder
        "press enter to continue": "",    # generic continue
        "how is claude doing": "0",       # dismiss feedback survey
        "bypass permissions mode": "2",   # Yes, I accept
        "accept all responsibility": "2", # same screen
        "yes, i trust this folder": "1",  # trust workspace
        "i trust this folder": "1",
        "i trust this project": "1",
        "trust the contents": "1",
        "quick safety check": "1",        # trust workspace intro
    }

    def _looks_like_gate_prompt(self, text: str) -> bool:
        lower = text.lower()
        return (any(pat in lower for pat in self._AUTO_GATE_PATTERNS)
                or any(pat in lower for pat in self._FATAL_GATE_PATTERNS))

    def _is_fatal_gate(self, text: str) -> bool:
        """Return True if the gate requires user action (e.g. OAuth login)."""
        lower = text.lower()
        return any(pat in lower for pat in self._FATAL_GATE_PATTERNS)

    def _gate_response(self, text: str) -> str:
        """Return the key to send for an auto-dismissable gate prompt."""
        lower = text.lower()
        for phrase, choice in self._GATE_CHOICES.items():
            if phrase in lower:
                return choice
        return ""

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
        """Return True when Claude Code has answered and returned to a fresh prompt."""
        turns = _parse_claude_code_turns(text)
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

        # The last meaningful line should be an empty ❯ prompt (idle).
        last = meaningful[-1]
        if not last.startswith(_PROMPT_PREFIX):
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

    # ── Execution paths ──────────────────────────────────────────────

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        return self._execute_run_tui(host=host, session=session, claimed=claimed, supervisor=supervisor)

    def _execute_run_tui(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: "RuntimeSupervisor",
    ) -> ExecutionResult:
        """TUI mode: send prompt at ❯ -> wait for idle -> parse output."""
        prompt = claimed["message"]["text"]
        run_id = claimed["run"]["run_id"]

        # Session reset depends on host kind and session_mode:
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

        baseline_screen = _strip_ansi(host.read_visible(session))
        baseline_turns = [t for t in _parse_claude_code_turns(baseline_screen) if t["response"]]
        baseline_last_response = None
        if baseline_turns:
            baseline_last_response = "\n".join(baseline_turns[-1]["response"]).strip()

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

        timeout = self.idle_timeout_seconds or 180.0
        deadline = monotonic() + timeout
        prev_screen = ""
        prev_tail = ""
        unchanged = 0
        tui_active = False
        dispatch_time = monotonic()

        while monotonic() < deadline:
            sleep(self.idle_poll_seconds)
            _poll_hook()
            screen = _strip_ansi(host.read_visible(session))
            snap = self._normalise_visible_screen(screen)
            tail = self._screen_tail(screen)

            startup_settled = tui_active or (monotonic() - dispatch_time > 5.0)
            if startup_settled and self._looks_like_shell_returned(screen):
                raise RecoverableExecutionError("claude code cli exited during execution")

            if self._looks_like_gate_prompt(screen):
                if self._is_fatal_gate(screen):
                    raise RecoverableExecutionError(
                        "claude code requires interactive login — complete OAuth "
                        "setup in the container and re-commit the image"
                    )
                host.send_text(session, self._gate_response(screen), enter=True)
                prev_screen = snap
                prev_tail = tail
                unchanged = 0
                tui_active = True
                continue

            if tail == prev_tail:
                unchanged += 1
            else:
                unchanged = 0
                tui_active = True
            prev_screen = snap
            prev_tail = tail

            stable_after = max(1, self.idle_after - 1)
            if unchanged < stable_after:
                continue

            if self._looks_like_working(screen):
                unchanged = 0
                continue

            if self._looks_like_completed_turn(
                screen,
                baseline_answered_turns=len(baseline_turns),
                baseline_last_response=baseline_last_response,
            ):
                break
        else:
            if not prev_screen.strip():
                raise RecoverableExecutionError("claude code tui produced no output after dispatch")
            raise RecoverableExecutionError("claude code tui did not become idle within timeout")

        raw_output = _strip_ansi(host.read_visible(session))
        read = host.read_output(session, cursor)
        session.metadata["restored_cursor"] = read.cursor
        cleaned = _clean_claude_code_output(raw_output)

        if not cleaned.strip():
            raise RecoverableExecutionError("claude code tui produced no output after idle")

        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                ArtifactPayload(role="transcript_log", name="transcript.txt", content=raw_output),
                ArtifactPayload(role="exec_log", name="exec.txt", content=read.full_text),
                ArtifactPayload(role="result", name="result.txt", content=cleaned),
            ],
            summary={"adapter": self.kind, "host": host.kind, "run_id": run_id, "mode": "tui"},
        )

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],  # noqa: ARG002
        attempt: int,  # noqa: ARG002
        error: Exception,
        supervisor: "RuntimeSupervisor",  # noqa: ARG002
    ) -> None:
        health = host.health(session)
        if not health.healthy:
            return
        if "exited during execution" in str(error):
            session.metadata.pop("claude_code_bootstrapped", None)
            return
        host.interrupt(session)
        sleep(0.1)

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
            host=host, session=session, claimed=claimed, error=error, supervisor=supervisor,
        )
