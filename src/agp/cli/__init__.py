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
from agp.cli import _infra as _infra  # noqa: F401,E402
from agp.cli import _lifecycle as _lifecycle  # noqa: F401,E402
from agp.cli import _send as _send  # noqa: F401,E402
from agp.cli import _review as _review  # noqa: F401,E402
from agp.cli import _wait as _wait  # noqa: F401,E402
from agp.cli import _status as _status  # noqa: F401,E402
from agp.cli import _info as _info  # noqa: F401,E402
from agp.cli import _nudge as _nudge  # noqa: F401,E402

# Re-export commonly used internals for backward compat (tests, skyops, etc.)
from agp.cli._helpers import (  # noqa: F401,E402
    _cli_client as _cli_client,
    _cli_idempotency_key as _cli_idempotency_key,
    _extract_trailing_json_payload as _extract_trailing_json_payload,
    _format_http_error as _format_http_error,
    _heartbeat_age_seconds as _heartbeat_age_seconds,
    _make_client as _make_client,
    _poll_until_done as _poll_until_done,
    _strip_tui_action_traces as _strip_tui_action_traces,
    _REVIEW_OUTPUT_CONTRACT as _REVIEW_OUTPUT_CONTRACT,
)
from agp.cli._review import (  # noqa: F401,E402
    _capture_git_diff as _capture_git_diff,
    _review_attachment_note as _review_attachment_note,
    _review_fix_attachment_note as _review_fix_attachment_note,
)
from agp.cli._infra import (  # noqa: F401,E402
    runtime_work_loop as runtime_work_loop,
    serve as serve,
    sweep_loop as sweep_loop,
    sweep_runtimes_loop as sweep_runtimes_loop,
)
