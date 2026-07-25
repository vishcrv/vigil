"""Make the `ml` package importable regardless of where pytest is invoked from.

Without this the suite only collects when the working directory is `ml_workstream/`, which is
an easy way for CI (or a teammate running `pytest` at the repo root) to see three collection
errors and conclude the tests are broken.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
