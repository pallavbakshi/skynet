"""Pytest bootstrap for AGP tests.

Force the test process onto the SQLite test database before any ``agp.*``
modules import ``agp.config`` / ``agp.db``. The local dev shell often exports
``AGP_DATABASE_URL`` for a running Postgres-backed control plane, and the ORM
engine is created at import time.
"""

import os
import sys
import tempfile
from pathlib import Path

# Fail fast if agp.config was already imported — the module-level Settings()
# singleton would have captured the wrong DATABASE_URL.
assert "agp.config" not in sys.modules, (
    "agp.config was imported before tests/conftest.py could redirect "
    "AGP_DATABASE_URL to a temporary database; a pytest plugin or early "
    "import is pulling in the agp package too soon"
)

_TEST_DB_PATH = Path(tempfile.gettempdir()) / f"agp-pytest-{os.getpid()}.db"
os.environ["AGP_DATABASE_URL"] = f"sqlite+pysqlite:///{_TEST_DB_PATH}"

sys.path.insert(0, os.path.dirname(__file__))
