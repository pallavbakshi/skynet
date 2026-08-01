"""CLI entrypoint for the AGP scaffold.

Split into submodules for maintainability:
  _helpers.py    — shared formatting, client setup, polling
  _infra.py      — hidden infra commands (initdb, serve, sweeper loops)
  _lifecycle.py  — up, down, interrupt
  _send.py       — send, reply
  _review.py     — review, review-status, review-diagnose
  _wait.py       — wait, attach, peek
  _status.py     — status, jobs, result
  _info.py       — info (agent, runtime, capability)
  _nudge.py      — nudge, cleanup
"""

import typer

app = typer.Typer(help="AGP agent CLI.")

# Import all command modules so their @app.command() decorators register.
from agp.cli import _info as _info
from agp.cli import _infra as _infra
from agp.cli import _lifecycle as _lifecycle
from agp.cli import _nudge as _nudge
from agp.cli import _review as _review
from agp.cli import _send as _send
from agp.cli import _status as _status
from agp.cli import _wait as _wait
from agp.cli._helpers import (
    _REVIEW_OUTPUT_CONTRACT as _REVIEW_OUTPUT_CONTRACT,
)

# Re-export commonly used internals for backward compat (tests, skyops, etc.)
from agp.cli._helpers import (
    _cli_client as _cli_client,
)
from agp.cli._helpers import (
    _cli_idempotency_key as _cli_idempotency_key,
)
from agp.cli._helpers import (
    _extract_trailing_json_payload as _extract_trailing_json_payload,
)
from agp.cli._helpers import (
    _format_http_error as _format_http_error,
)
from agp.cli._helpers import (
    _heartbeat_age_seconds as _heartbeat_age_seconds,
)
from agp.cli._helpers import (
    _make_client as _make_client,
)
from agp.cli._helpers import (
    _poll_until_done as _poll_until_done,
)
from agp.cli._helpers import (
    _strip_tui_action_traces as _strip_tui_action_traces,
)
from agp.cli._infra import (
    runtime_work_loop as runtime_work_loop,
)
from agp.cli._infra import (
    serve as serve,
)
from agp.cli._infra import (
    sweep_loop as sweep_loop,
)
from agp.cli._infra import (
    sweep_runtimes_loop as sweep_runtimes_loop,
)
from agp.cli._review import (
    _capture_git_diff as _capture_git_diff,
)
from agp.cli._review import (
    _review_attachment_note as _review_attachment_note,
)
from agp.cli._review import (
    _review_fix_attachment_note as _review_fix_attachment_note,
)
