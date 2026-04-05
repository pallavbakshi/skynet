"""Via-file prompt delivery for TUI and terminal adapters.

Instead of pasting long prompts into the terminal (risking paste buffer
corruption, size limits, and special character mangling), write a structured
task file and send a short reference string to the agent.

The agent reads the file directly with full fidelity.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Private directory under /tmp owned by the current process user.
_TASK_DIR = Path(f"/tmp/agp-tasks-{os.getuid()}")

# Reference string template — the agent sees this in its prompt input.
_REFERENCE_TEMPLATE = "Read the file {path} and follow the instructions inside."


def _ensure_task_dir() -> Path:
    """Create and validate the private task directory."""
    _TASK_DIR.mkdir(mode=0o700, exist_ok=True)
    stat = _TASK_DIR.stat()
    if stat.st_uid != os.getuid():
        raise RuntimeError(
            f"task directory {_TASK_DIR} is owned by uid {stat.st_uid}, not us"
        )
    return _TASK_DIR


def build_task_file_content(
    *,
    prompt: str,
    claimed: dict[str, Any],
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Build the structured markdown content for the task file.

    Includes: task prompt, job/run metadata, conversation context,
    attachment manifest, and output contract.
    """
    run = claimed.get("run") or {}
    job = claimed.get("job") or {}
    message = claimed.get("message") or {}
    conversation_id = message.get("conversation_id") or job.get("conversation_id")

    sections: list[str] = []

    # Header with metadata
    sections.append("# AGP Task\n")
    meta_lines = []
    if run.get("run_id"):
        meta_lines.append(f"- **Run ID:** `{run['run_id']}`")
    if job.get("job_id"):
        meta_lines.append(f"- **Job ID:** `{job['job_id']}`")
    if conversation_id:
        meta_lines.append(f"- **Conversation:** `{conversation_id}`")
    if claimed.get("agent_id"):
        meta_lines.append(f"- **Agent:** `{claimed['agent_id']}`")
    if job.get("parent_job_id"):
        meta_lines.append(f"- **Parent Job:** `{job['parent_job_id']}`")
    if meta_lines:
        sections.append("\n".join(meta_lines))

    # Task prompt (the main content)
    sections.append("## Task\n")
    sections.append(prompt)

    # Attachments manifest
    attachment_items = attachments or claimed.get("job_attachments") or []
    if attachment_items:
        sections.append("## Attachments\n")
        for item in attachment_items:
            name = item.get("name", "unknown")
            role = item.get("role", "")
            staged_path = item.get("staged_path", "")
            line = f"- **{name}**"
            if role:
                line += f" (role: {role})"
            if staged_path:
                line += f" — `{staged_path}`"
            sections.append(line)

    # Conversation context (prior messages in the thread)
    context_messages = claimed.get("context_messages") or []
    if context_messages:
        sections.append("## Conversation Context\n")
        for msg in context_messages:
            role = msg.get("role", "unknown")
            text = msg.get("text", "")
            if text:
                sections.append(f"### {role}\n\n{text}")

    # Output contract
    contract = job.get("output_contract_json")
    if isinstance(contract, dict) and contract:
        sections.append("## Output Contract\n")
        sections.append(
            "You must respond with valid JSON matching this schema:\n"
        )
        sections.append(f"```json\n{json.dumps(contract.get('json_schema', {}), indent=2, sort_keys=True)}\n```")
        sections.append(
            "Do not include markdown fences, prose, or any text outside the JSON object "
            "in your final answer."
        )

    return "\n\n".join(sections) + "\n"


def write_task_file(
    *,
    run_id: str,
    content: str,
) -> str:
    """Write the task content to a temp file and return its path.

    Uses a deterministic filename based on run_id so the file can be
    found and cleaned up reliably.
    """
    task_dir = _ensure_task_dir()
    path = task_dir / f"agp-task-{run_id}.md"
    # Use atomic write pattern: write to temp then rename
    fd, tmp_path = tempfile.mkstemp(
        prefix="agp-task-", suffix=".md.tmp", dir=str(task_dir),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            written = f.write(content)
            if written != len(content):
                raise OSError(f"short write: expected {len(content)} chars, wrote {written}")
        os.rename(tmp_path, str(path))
    except BaseException:
        # Clean up the temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(path)


def reference_string(path: str) -> str:
    """Return the short reference string the agent sees in the TUI."""
    return _REFERENCE_TEMPLATE.format(path=path)


def cleanup_task_file(run_id: str) -> bool:
    """Remove the task file for a specific run. Returns True if deleted."""
    path = _TASK_DIR / f"agp-task-{run_id}.md"
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        _logger.warning("failed to clean task file %s: %s", path, exc)
        return False


def cleanup_stale_task_files(*, max_age_seconds: float = 1800) -> int:
    """Remove task files older than *max_age_seconds* (default: 30 min).

    Returns the number of files cleaned.
    """
    import time

    if not _TASK_DIR.is_dir():
        return 0
    cleaned = 0
    now = time.time()
    for f in _TASK_DIR.iterdir():
        try:
            if f.name.startswith("agp-task-"):
                age = now - f.stat().st_mtime
                if age > max_age_seconds:
                    f.unlink()
                    cleaned += 1
        except Exception:  # noqa: BLE001
            pass
    return cleaned
