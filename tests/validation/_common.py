"""Shared path constants for the validation scripts.

Import this (never compare_models — its module level resolves API keys, builds an
anthropic client, and reads the gitignored profile/guide) when a script only needs
the paths. Import-time effects are limited to creating the gitignored results/ dir
and reading config.yaml for the DB location. Everything anchors on __file__ so the
scripts work from any CWD.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import core  # noqa: E402

# Every validation script's outputs land here (gitignored), never in the repo root.
RESULTS_DIR = Path(__file__).resolve().parent / "results"
try:
    RESULTS_DIR.mkdir(exist_ok=True)
except OSError as e:
    sys.exit(f"cannot create {RESULTS_DIR} ({e}) — is 'results' a stray FILE?")

# The DB the pipeline actually uses: the same config knob core.connect_db reads
# (settings.db_path under core.BASE_DIR) — never a hardcoded filename, so moving
# the DB via config can't silently split the pipeline and the validation scripts
# onto different files.
DB_PATH = core.BASE_DIR / core.load_config()["settings"]["db_path"]
