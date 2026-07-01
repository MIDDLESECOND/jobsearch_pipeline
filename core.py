#!/usr/bin/env python3
"""Shared foundation for the pipeline: paths, the cross-cutting constants, config loading,
the SQLite open/schema/migration path, and the API-key resolver.

This is the base of the module DAG — it imports only chain.py (for the fingerprint normalization
used by the one-time backfill/recompute) and the stdlib. fetch.py, evaluation.py, filters.py,
report.py, and pipeline.py all import FROM here; nothing here imports them (so there are no cycles).
"""

import os
import sqlite3
import sys
from pathlib import Path

import yaml

from chain import _norm_company, _norm_title, _fingerprint, _NORM_VERSION

# Windows consoles (and redirected log files) default to the locale code page — here `gbk` —
# which can't encode characters that show up in scraped LinkedIn text: em-dashes in our own
# format strings, and non-breaking spaces (\xa0) inside job titles/companies. A single such
# character used to crash the whole run with UnicodeEncodeError mid-print. Force UTF-8 with
# errors="replace" so output degrades a stray glyph instead of aborting. Runs on import, so
# it covers every entry point (pipeline.py, app.py, the validation scripts) that pulls in core.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
PROFILE_PATH = BASE_DIR / "profile.md"
GUIDE_PATH = BASE_DIR / "evaluation_guide.md"

GATE_NAMES = ["years_floor", "domain_requirement", "role_substance", "tool_requirement", "work_auth", "employment_type"]
SCORE_DIMS = ["ai_applied_vs_research", "ai_artifact_depth", "learning_value",
              "technical_skill_match", "title_trajectory", "years_vs_stated"]
VERDICTS = ["PASS", "GATE_FAIL", "RECRUITER_ONLY"]


# ---------------------------------------------------------------- config / db

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db(cfg):
    # timeout (busy-wait on a locked DB) raised above the 5s default so a concurrent open
    # during the one-time fingerprint recompute waits it out instead of erroring.
    conn = sqlite3.connect(BASE_DIR / cfg["settings"]["db_path"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_url      TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            location     TEXT,
            search_name  TEXT,
            tier         TEXT,
            date_posted  TEXT,
            first_seen   TEXT,
            salary_min   REAL,
            salary_max   REAL,
            description  TEXT,
            status       TEXT,   -- new | evaluated | needs_manual | salary_filtered | rule_filtered | repost_decided | error
            verdict      TEXT,   -- PASS | GATE_FAIL | RECRUITER_ONLY
            failed_gate  TEXT,
            fit_score    INTEGER,
            bucket       INTEGER, -- 1 | 2 | 3 (channel routing; null for gate fails)
            eval_json    TEXT,
            norm_company TEXT,    -- normalized company (suffix-stripped) for repost matching
            norm_title   TEXT,    -- normalized title (abbrevs expanded) for fuzzy matching
            fingerprint  TEXT,    -- blocking key: norm_company|norm_location
            repost_of    TEXT,    -- job_url of the canonical original if this is a repost
            repost_source TEXT,   -- NULL = auto (_find_repost) | 'manual' = user-linked original |
                                  -- 'manual:<prev_url>' = user-linked relisting (prev parent encoded for undo)
            app_status   TEXT,    -- NULL (backlog) | applied | passed  (user's decision)
            status_date  TEXT,    -- date app_status was set
            filter_source TEXT,   -- NULL | manual | rule:<name>  (hard-fail override)
            filter_gate  TEXT,    -- which gate the override represents
            filter_date  TEXT,    -- date the override was set
            source       TEXT     -- where the posting came from: 'linkedin' | 'adzuna'
        )
    """)
    _migrate(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)")
    # repost_of is scanned per-decision by _chain_targets and per-row by _repost_info / cmd_report;
    # index it so chain resolution is O(chain) not O(table).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repost_of ON jobs(repost_of)")
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an existing DB up to the current schema. Idempotent — safe to run
    every startup. Added for the v2 guide: the `bucket` column (channel routing).
    Repost dedup (v3): content fingerprint + application-status tracking columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "bucket" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN bucket INTEGER")
        print("[migrate] added column jobs.bucket")
    new_cols = [
        ("norm_company", "TEXT"),
        ("norm_title", "TEXT"),
        ("fingerprint", "TEXT"),
        ("repost_of", "TEXT"),
        ("repost_source", "TEXT"),  # NULL=auto | manual | manual:<prev_url>  (manual-link provenance)
        ("app_status", "TEXT"),   # NULL | applied | passed
        ("status_date", "TEXT"),
        ("filter_source", "TEXT"),  # NULL | manual | rule:<name>
        ("filter_gate", "TEXT"),
        ("filter_date", "TEXT"),
        ("source", "TEXT"),  # 'linkedin' | 'adzuna' — multi-source provenance
    ]
    added = False
    for col, decl in new_cols:
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            print(f"[migrate] added column jobs.{col}")
            added = True
    if added:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)")
    # Every pre-source row was a LinkedIn scrape; backfill so reports/UI can rely on it.
    if "source" not in cols:
        conn.execute("UPDATE jobs SET source='linkedin' WHERE source IS NULL")
        print("[migrate] backfilled jobs.source='linkedin' for existing rows")
    conn.commit()
    _migrate_applied_to_status(conn, cols)
    _backfill_fingerprints(conn)
    _recompute_fingerprints(conn)


def _migrate_applied_to_status(conn, cols):
    """v3.1: the binary `applied`/`applied_date` columns became the `app_status`
    lifecycle (NULL | applied | passed). Fold the old flag into the new column, then
    drop the dead columns. `DROP COLUMN` needs SQLite >= 3.35; if older, the columns
    are left in place (harmless — nothing reads them)."""
    if "applied" not in cols:
        return
    conn.execute(
        "UPDATE jobs SET app_status='applied', status_date=applied_date "
        "WHERE applied=1 AND app_status IS NULL"
    )
    conn.commit()
    print("[migrate] folded jobs.applied into jobs.app_status")
    for dead in ("applied", "applied_date"):
        try:
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {dead}")
            print(f"[migrate] dropped column jobs.{dead}")
        except sqlite3.OperationalError:
            pass  # SQLite < 3.35: leave it, it's unused
    conn.commit()


def _backfill_fingerprints(conn):
    """Populate norm_company / norm_title / fingerprint for rows that predate the
    repost-dedup columns, so historical postings participate in repost detection.
    One-time: only touches rows where fingerprint is still NULL."""
    rows = conn.execute(
        "SELECT job_url, company, title, location FROM jobs WHERE fingerprint IS NULL"
    ).fetchall()
    if not rows:
        return
    for r in rows:
        conn.execute(
            "UPDATE jobs SET norm_company=?, norm_title=?, fingerprint=? WHERE job_url=?",
            (
                _norm_company(r["company"]),
                _norm_title(r["title"]),
                _fingerprint(r["company"], r["location"]),
                r["job_url"],
            ),
        )
    conn.commit()
    print(f"[migrate] backfilled fingerprints for {len(rows)} existing rows")


def _recompute_fingerprints(conn):
    """Re-derive the content fingerprint for every row when the normalization scheme
    changes, so historical rows and new inserts share one key space (else a relisting of
    an old role under a new-style location label wouldn't match). Gated on PRAGMA
    user_version, so it runs exactly once per scheme bump. Only the fingerprint is
    rewritten; existing repost_of links are left as-is — consistent with the original
    backfill, historical rows are not retroactively cross-linked."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _NORM_VERSION:
        return
    rows = conn.execute("SELECT job_url, company, title, location FROM jobs").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE jobs SET norm_company=?, norm_title=?, fingerprint=? WHERE job_url=?",
            (
                _norm_company(r["company"]),
                _norm_title(r["title"]),
                _fingerprint(r["company"], r["location"]),
                r["job_url"],
            ),
        )
    conn.execute(f"PRAGMA user_version = {_NORM_VERSION}")
    conn.commit()
    if rows:  # stay silent on a fresh/empty DB (mirrors _backfill_fingerprints)
        print(f"[migrate] recomputed fingerprints for {len(rows)} rows (norm v{_NORM_VERSION})")


def _ensure_api_key(var="ANTHROPIC_API_KEY", label="eval"):
    """Return the named API key, self-healing the common Windows case where the
    key was set with `setx` but the current shell was opened before that and so
    never inherited it. Falls back to the persistent HKCU user environment.
    `label` is just the log-prefix for the load notice (e.g. "eval" vs "adzuna")."""
    key = os.environ.get(var)
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val, _ = winreg.QueryValueEx(k, var)
            if val:
                os.environ[var] = val
                print(f"[{label}] loaded {var} from persistent user environment")
                return val
        except (OSError, FileNotFoundError):
            pass
    return None
