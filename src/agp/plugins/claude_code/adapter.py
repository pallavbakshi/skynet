"""Claude Code CLI agent adapter — backed by smallops.

Delegates TUI lifecycle (bootstrap, prompt delivery, polling, parsing,
gate handling) to a smallops Session.  AGP-specific concerns (output
contracts, artifact generation, supervisor integration, exception
mapping) remain here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agp.plugins._output_contracts import (
    apply_output_contract_instruction,
    is_json_contract,
    prompt_for_claim,
    result_file_path_for_run,
)
from agp.plugins._provider_env import collect_provider_env
from agp.plugins._structured_output import select_structured_result
from agp.plugins._via_file import build_task_content
from agp.runtime._abc import AgentAdapter, TerminalHost
from agp.runtime._types import (
    AdapterExecutionFailed,
    ArtifactPayload,
    AuthFailure,
    BootstrapFailure,
    ExecutionResult,
    ExecutionTimeout,
    PaneDied,
    TerminalSession,
)
from smallops import (
    ClaudeCodeTui,
    Session,
)
from smallops import (
    Config as SmallopsConfig,
)
from smallops._types import (
    BootstrapTimeout as _BootstrapTimeout,
)
from smallops._types import (
    FatalGate as _FatalGate,
)
from smallops._types import (
    PaneDied as _PaneDied,
)
from smallops._types import (
    SendTimeout as _SendTimeout,
)
from smallops._util import strip_ansi as _strip_ansi

if TYPE_CHECKING:
    from agp.runtime._supervisor import RuntimeSupervisor

_logger = logging.getLogger(__name__)


class ClaudeCodeAdapter(AgentAdapter):
    """Agent adapter for Claude Code CLI, backed by smallops Session."""

    def __init__(
        self,
        *,
        cli_command: str = "claude",
        idle_poll_seconds: float = 2.0,
        idle_after: int = 3,
        idle_timeout_seconds: float = 0.0,
        session_mode: str = "ephemeral",
    ) -> None:
        self.cli_command = cli_command
        self.idle_poll_seconds = idle_poll_seconds
        self.idle_after = idle_after
        self.idle_timeout_seconds = idle_timeout_seconds
        self.session_mode = session_mode
        self._smallops_session: Session | None = None

    @property
    def _effective_timeout(self) -> float:
        return self.idle_timeout_seconds if self.idle_timeout_seconds > 0 else 300.0

    @property
    def kind(self) -> str:
        return "claude_code"

    def _get_or_create_session(
        self, host: TerminalHost, session: TerminalSession,
    ) -> Session:
        """Get or create a smallops Session sharing the host's Mux."""
        from agp.plugins._smallops_host import SmallopsTerminalHost
        if not isinstance(host, SmallopsTerminalHost):
            raise TypeError(
                f"ClaudeCodeAdapter requires SmallopsTerminalHost, got {type(host).__name__}"
            )
        if self._smallops_session is not None:
            return self._smallops_session

        config = SmallopsConfig(
            poll_interval=self.idle_poll_seconds,
            idle_threshold=self.idle_after,
            timeout=self._effective_timeout,
            bootstrap_timeout=max(60.0, self._effective_timeout),
        )
        tui = ClaudeCodeTui(cli=self.cli_command)
        self._smallops_session = Session(
            mux=host.mux,
            tui=tui,
            config=config,
            name=session.agent_id,
        )
        return self._smallops_session

    def _resolve_attachment_paths(
        self, session: TerminalSession, claimed: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Enrich attachment metadata with staged file paths and content."""
        items = claimed.get("job_attachments") or []
        if not items:
            return None
        from urllib.parse import unquote, urlparse

        from agp.runtime._attachments import staged_attachment_relative_path
        workspace = session.workspace_ref or ""
        if "://" in workspace:
            parsed = urlparse(workspace)
            if parsed.scheme == "file":
                workspace = unquote(parsed.path)
        enriched = []
        for item in items:
            entry = dict(item)
            name = str(item.get("name", ""))
            artifact_id = str(item.get("artifact_id", ""))
            if workspace and name and artifact_id:
                rel = staged_attachment_relative_path(artifact_id=artifact_id, name=name)
                candidate = Path(workspace) / rel
                if candidate.exists():
                    entry["staged_path"] = str(candidate)
                    # Include content inline so the task file is self-contained
                    if "content" not in entry:
                        try:
                            entry["content"] = candidate.read_text(encoding="utf-8")
                        except Exception:
                            pass
            enriched.append(entry)
        return enriched if enriched else None

    def inspect_output(self, *, text: str, run_id: str | None = None) -> dict[str, Any]:
        clean = _strip_ansi(text)
        tui = ClaudeCodeTui(cli=self.cli_command)
        from smallops.tui.claude_code._classify import (
            ends_with_prompt,
            is_shell_returned,
        )
        return {
            "adapter_kind": self.kind,
            "mode": "tui",
            "run_id": run_id,
            "cleaned_output": tui.parse_response(clean, ""),
            "looks_like_ready": ends_with_prompt(clean),
            "looks_like_gate_prompt": tui.gate_response(clean) is not None,
            "looks_like_shell_returned": is_shell_returned(clean),
            "supported": True,
        }

    def ensure_bootstrapped(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
    ) -> bool:
        """Bootstrap the Claude Code TUI via smallops Session.up().

        In sticky mode (default), reuses the existing TUI if it's still alive.
        """
        so = self._get_or_create_session(host, session)

        # Sticky: skip bootstrap if TUI is already running
        if so.is_alive():
            session.metadata["claude_code_bootstrapped"] = True
            return True

        env = collect_provider_env()

        try:
            so.up(cwd=session.workspace_ref, env=env)
        except _BootstrapTimeout as exc:
            raise BootstrapFailure(str(exc)) from exc
        except _FatalGate as exc:
            raise AuthFailure(str(exc)) from exc
        except _PaneDied as exc:
            raise BootstrapFailure(f"pane died during bootstrap: {exc}") from exc

        session.metadata["claude_code_bootstrapped"] = True
        return True

    # ── Execution ────────────────────────────────────────────────────

    def execute_run(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: RuntimeSupervisor,
    ) -> ExecutionResult:
        return self._execute_run_impl(
            host=host, session=session, claimed=claimed, supervisor=supervisor,
        )

    def _execute_run_impl(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        supervisor: RuntimeSupervisor,
    ) -> ExecutionResult:
        """Send prompt via smallops, wrap Response -> ExecutionResult."""
        run_id = claimed["run"]["run_id"]
        contract = (claimed.get("job") or {}).get("output_contract_json")
        json_contract = is_json_contract(contract)
        result_file = result_file_path_for_run(run_id) if json_contract else None

        if result_file:
            try:
                Path(result_file).unlink(missing_ok=True)
            except OSError:
                pass

        # Build enriched prompt
        prompt = apply_output_contract_instruction(
            prompt=claimed["message"]["text"],
            claimed=claimed,
            result_file_path=result_file,
        )

        # Split prompt from extra sections (metadata, attachments, context).
        # smallops wraps prompt in BEGIN TASK / END TASK; sections go after.
        prompt_text, sections_text = build_task_content(
            prompt=prompt,
            claimed=claimed,
            attachments=self._resolve_attachment_paths(session, claimed),
        )

        # Check if the original message was sent with --via-file and the
        # file is still accessible — if so, pass the path directly so
        # smallops reads it instead of AGP creating a second copy.
        # IMPORTANT: skip file= when an output contract enriched the prompt,
        # because the original file lacks the injected contract instructions.
        via_file_path = (claimed.get("message") or {}).get("metadata", {}).get("via_file")
        if via_file_path and (result_file or not Path(via_file_path).is_file()):
            via_file_path = None  # enriched or file gone — use prompt_text

        so = self._get_or_create_session(host, session)
        if not session.metadata.get("claude_code_bootstrapped"):
            self.ensure_bootstrapped(host=host, session=session, claimed=claimed)

        # Emit dispatch progress
        supervisor.emit_progress(
            claimed,
            message="runtime.tui_dispatch",
            details={
                "adapter": self.kind,
                "session_id": session.session_id,
                "run_id": run_id,
            },
        )

        # so.send() writes to a via-file (with BEGIN TASK / END TASK) and
        # sends a short reference string to the TUI.
        try:
            if via_file_path:
                # Original file unmodified — pass it directly, avoid double-wrapping.
                response = so.send(
                    file=via_file_path,
                    sections=sections_text or None,
                    timeout=self._effective_timeout,
                )
            else:
                # Prompt was enriched (output contract) or no via-file — use prompt_text.
                response = so.send(
                    prompt_text,
                    sections=sections_text or None,
                    timeout=self._effective_timeout,
                )
        except _SendTimeout as exc:
            screen = ""
            try:
                screen = so.peek(300)
            except Exception:
                _logger.debug("failed to capture screen on timeout", exc_info=True)
            raise ExecutionTimeout(
                f"claude code tui did not complete: {exc}\n--- screen at timeout ---\n{screen}"
            ) from exc
        except _PaneDied as exc:
            raise PaneDied(f"pane died during execution: {exc}") from exc
        except _FatalGate as exc:
            raise AuthFailure(f"fatal gate during execution: {exc}") from exc

        # Collect scrollback for transcript artifact
        scrollback = ""
        try:
            scrollback = _strip_ansi(so.peek(500))
        except Exception:
            _logger.debug("failed to capture scrollback", exc_info=True)
            scrollback = response.raw

        # Prefer parsed LLM prose; fall back to raw capture if empty
        parsed = getattr(response, "parsed", None)
        if parsed and parsed.text and parsed.text.strip():
            cleaned = parsed.text
        elif parsed and parsed.raw and parsed.raw.strip():
            cleaned = parsed.raw
        else:
            cleaned = response.text or response.raw or ""

        # JSON contract overlay
        extraction_diag = None
        if json_contract and cleaned:
            cleaned_sources = [
                ("cleaned", cleaned),
                ("parsed_raw", parsed.raw if parsed else ""),
                ("raw", response.raw),
            ]
            if scrollback:
                cleaned_sources.append(("scrollback", scrollback))
            selected, extraction_diag = select_structured_result(
                result_file=result_file,
                cleaned_sources=cleaned_sources,
                claimed=claimed,
            )
            if selected:
                cleaned = selected

        if not cleaned.strip():
            raise AdapterExecutionFailed("claude code tui produced no output after idle")

        # Reconstruct full task content for the artifact record — use the
        # actual prompt the agent received (original file when file= was used).
        if via_file_path:
            try:
                actual_prompt = Path(via_file_path).read_text(encoding="utf-8")
            except Exception:
                actual_prompt = prompt_text
        else:
            actual_prompt = prompt_text
        task_file_content = actual_prompt + ("\n\n" + sections_text if sections_text else "") + "\n"

        return ExecutionResult(
            artifacts=[
                ArtifactPayload(role="prompt", name="prompt.txt", content=prompt),
                ArtifactPayload(role="prompt", name="task-file.md", content=task_file_content),
                ArtifactPayload(role="transcript_log", name="transcript.txt", content=scrollback),
                ArtifactPayload(role="exec_log", name="exec.txt", content=response.raw),
                ArtifactPayload(role="result", name="result.txt", content=cleaned),
            ],
            summary={
                "adapter": self.kind,
                "host": host.kind,
                "run_id": run_id,
                "mode": "tui",
                "dispatch": "via_file",
                "elapsed": response.elapsed,
                **(
                    {
                        "model": parsed.status.model,
                        "effort": parsed.status.effort,
                        "tokens": parsed.status.tokens,
                        "context_pct": parsed.status.context_pct,
                        "session_id": parsed.status.session_id,
                    }
                    if parsed and parsed.status and parsed.status.model
                    else {}
                ),
            },
            diagnostics=extraction_diag.to_dict() if extraction_diag else None,
        )

    # ── Recovery ─────────────────────────────────────────────────────

    def recover(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        attempt: int,
        error: Exception,
        supervisor: RuntimeSupervisor,
    ) -> None:
        session.metadata.pop("claude_code_bootstrapped", None)
        if self._smallops_session is not None:
            try:
                self._smallops_session.interrupt()
            except Exception:
                _logger.debug("interrupt failed during recovery", exc_info=True)
            try:
                self._smallops_session.down()
            except Exception:
                _logger.debug("down failed during recovery", exc_info=True)
            self._smallops_session = None

    def build_failure_result(
        self,
        *,
        host: TerminalHost,
        session: TerminalSession,
        claimed: dict[str, Any],
        error: Exception,
        supervisor: RuntimeSupervisor,
    ) -> ExecutionResult:
        screen = ""
        try:
            screen = host.read_visible(session)
        except Exception:
            pass
        return ExecutionResult(
            artifacts=[
                ArtifactPayload(
                    role="prompt", name="prompt.txt",
                    content=prompt_for_claim(claimed=claimed),
                ),
                ArtifactPayload(
                    role="transcript_log", name="transcript.txt",
                    content=screen,
                ),
                ArtifactPayload(
                    role="failure_evidence", name="failure.txt",
                    content=f"{type(error).__name__}: {error}\n",
                ),
            ],
            summary={
                "adapter": self.kind,
                "host": host.kind,
                "exception_type": type(error).__name__,
            },
        )
