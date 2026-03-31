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

_TEST_DB_PATH = Path(tempfile.gettempdir()) / f"agp-pytest-{os.getpid()}.db"
os.environ["AGP_DATABASE_URL"] = f"sqlite+pysqlite:///{_TEST_DB_PATH}"

sys.path.insert(0, os.path.dirname(__file__))
