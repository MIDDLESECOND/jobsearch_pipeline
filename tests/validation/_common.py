"""Shared path constants for the validation scripts.

Import this (never compare_models — its module level resolves API keys, builds an
anthropic client, and reads the gitignored profile/guide) when a script only needs
the paths. Safe to import anywhere: the only import-time effect is creating the
gitignored results/ dir. Everything anchors on __file__ so scripts work from any CWD.
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every validation script's outputs land here (gitignored), never in the repo root.
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# The real DB, anchored to the repo root — NOT CWD-relative, which silently
# creates an empty jobs.db (and then "no such table: jobs") when a script is
# run from anywhere else.
DB_PATH = _REPO_ROOT / "jobs.db"
