"""Ensure the tests directory is importable so that ``from _base import ...`` works."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
