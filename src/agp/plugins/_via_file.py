"""AGP-level task content builder.

Builds the prompt text and extra sections (metadata, attachments, context,
output contracts) that are passed separately to ``smallops.Session.send()``.

Usage in adapters::

    prompt, sections = build_task_content(prompt=..., claimed=..., attachments=...)
    response = so.send(prompt, sections=sections)

smallops handles all file delivery: it wraps *prompt* in BEGIN TASK / END TASK
markers, appends *sections* after END TASK, writes the file, and sends a short
reference string to the agent's terminal.  **Do NOT add BEGIN TASK / END TASK
markers here.**
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

# Reference string template for AGP-managed task files.
# NOTE: For normal delivery, smallops generates its own reference string
# with BEGIN TASK / END TASK framing.  This template is for AGP-only files
# which do NOT contain those markers.
_REFERENCE_TEMPLATE = (
    "Read the file {path} and follow the instructions inside."
)


def _ensure_task_dir() -> Path:
    """Create and validate the private task directory."""
    _TASK_DIR.mkdir(mode=0o700, exist_ok=True)
    stat = _TASK_DIR.stat()
    if stat.st_uid != os.getuid():
        raise RuntimeError(
            f"task directory {_TASK_DIR} is owned by uid {stat.st_uid}, not us"
        )
    return _TASK_DIR


def build_task_content(
    *,
    prompt: str,
    claimed: dict[str, Any],
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Build the prompt and extra sections for a task.

    Returns ``(prompt_text, sections_text)`` where:

    - *prompt_text* goes inside smallops' BEGIN TASK / END TASK block.
    - *sections_text* is passed as ``sections=`` to ``so.send()`` and
      appears **after** END TASK in the task file (metadata, attachments,
      conversation context, output contract).

    Callers should use::

        prompt, sections = build_task_content(...)
        response = so.send(prompt, sections=sections)
    """
    run = claimed.get("run") or {}
    job = claimed.get("job") or {}
    message = claimed.get("message") or {}
    conversation_id = message.get("conversation_id") or job.get("conversation_id")

    extra: list[str] = []

    # Header with metadata
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
        extra.append("## Metadata\n\n" + "\n".join(meta_lines))

    # Attachments — include staged path and inline content when available
    attachment_items = attachments or claimed.get("job_attachments") or []
    if attachment_items:
        att_parts = ["## Attachments\n"]
        for item in attachment_items:
            name = item.get("name", "unknown")
            role = item.get("role", "")
            staged_path = item.get("staged_path", "")
            content = item.get("content", "")
            line = f"- **{name}**"
            if role:
                line += f" (role: {role})"
            if staged_path:
                line += f" — `{staged_path}`"
            att_parts.append(line)
            if content:
                ext = name.rsplit(".", 1)[-1] if "." in name else ""
                att_parts.append(f"```{ext}\n{content}\n```")
        extra.append("\n".join(att_parts))

    # Conversation context (prior messages in the thread)
    context_messages = claimed.get("context_messages") or []
    if context_messages:
        ctx_parts = ["## Conversation Context\n"]
        for msg in context_messages:
            role = msg.get("role", "unknown")
            text = msg.get("text", "")
            if text:
                ctx_parts.append(f"### {role}\n\n{text}")
        extra.append("\n\n".join(ctx_parts))

    # Output contract
    contract = job.get("output_contract_json")
    if isinstance(contract, dict) and contract:
        extra.append(
            "## Output Contract\n\n"
            "You must respond with valid JSON matching this schema:\n\n"
            f"```json\n{json.dumps(contract.get('json_schema', {}), indent=2, sort_keys=True)}\n```\n\n"
            "Do not include markdown fences, prose, or any text outside the JSON object "
            "in your final answer."
        )

    sections_text = "\n\n".join(extra) + "\n" if extra else ""
    return prompt, sections_text


def write_task_file(
    *,
    run_id: str,
    content: str,
) -> str:
    """Write task content to an AGP-managed temp file and return its path.

    NOTE: For normal task delivery, use ``smallops.Session.send()`` instead —
    it handles via-file writing, reference string injection, and polling.
    This function is only needed for out-of-band file management.
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
    """Return AGP's reference string for a task file path.

    NOTE: For normal delivery, ``smallops.write_via_file()`` generates
    its own reference string.  This is only for AGP-managed files.
    """
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
