# tests/conftest.py
# Ensures the repo root is importable regardless of how pytest is invoked
# (bare `pytest`, `python -m pytest`, or from a different cwd) — same
# pattern already used in pipeline/batch_processor.py for script execution.

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
