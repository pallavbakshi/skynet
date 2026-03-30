"""Test package bootstrap.

Defaults the test process to an isolated SQLite database so ad-hoc
``pytest``/``unittest`` runs do not accidentally bind to the repo-local
``./agp.db`` that a live control plane may be using.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _default_test_database_url() -> str:
    db_path = Path(tempfile.gettempdir()) / f"agp-test-suite-{os.getpid()}.db"
    return f"sqlite+pysqlite:///{db_path}"


os.environ.setdefault("AGP_DATABASE_URL", _default_test_database_url())
