#!/usr/bin/env python3
"""Shared foundation for the pipeline: paths, the cross-cutting constants, config loading,
the SQLite open/schema/migration path, and the API-key resolver.

This is the base of the module DAG — it imports only chain.py (for the fingerprint normalization
used by the one-time backfill/recompute) and the stdlib. fetch.py, evaluation.py, filters.py,
report.py, and pipeline.py all import FROM here; nothing here imports them (so there are no cycles).
"""

import contextlib
import os
import sqlite3
import sys
import threading
import traceback
from datetime import date, datetime
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


# ------------------------------------------------------------ posting-date parsing

# Sanity window for posting dates. A parseable value outside it is a placeholder
# ("9999-12-31") or corruption, not information — parse_iso treats it as unparseable, so
# consumers degrade to their honest first_seen fallback instead of crashing (Windows'
# mktime raises OSError outside roughly 1970..3000, and .timestamp()'s naive-datetime fold
# probe reaches a further day back — so even "safe-looking" extreme values aren't) or
# pinning a fake-fresh row to the top of the triage sort. PARSE_MIN also serves as the
# sort-last sentinel: year 2000 is far from both mktime cliffs in every timezone.
PARSE_MIN = datetime(2000, 1, 1)
PARSE_MAX = datetime(2500, 1, 1)


def parse_iso(s):
    """Parse a stored posting-date string → (local-naive datetime, day_only), or None when
    the value is empty, unparseable, or outside the PARSE_MIN..PARSE_MAX sanity window.

    The ONE parser for `date_posted`/`first_seen` values: the fetch side normalizes through
    it (fetch._ats_date) and the read side ranks/labels through it (report._recency_dt), so
    the stored shape's producer and consumer can't drift. Accepts every stored convention —
    bare dates, local-naive timestamps, and offset/'Z' timestamps (converted to local naive,
    the same convention first_seen uses). day_only is True when the string carries no
    time-of-day marker (a bare calendar date in any ISO form: '2026-07-04', '20260704', a
    week date) — such values must never be given fake hour precision downstream."""
    s = str(s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            # astimezone() also goes through mktime's range check on Windows (OSError on
            # absurd years) — inside the try on purpose.
            dt = dt.astimezone().replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None
    if not (PARSE_MIN <= dt <= PARSE_MAX):
        return None
    return dt, not any(c in s for c in "T: ")


# ------------------------------------------------------------------- run log
#
# Capture is app-level, not shell-level: a `run` tees stdout+stderr into
# logs/pipeline-YYYY-MM-DD.log itself, so a manual terminal run is recorded identically
# to a scheduled one — the scheduler .bat no longer redirects (that would double-log).
# The tee is an object-level swap of sys.stdout/stderr; it works for library logging
# too because jobspy is imported lazily inside the run (after this swap), so its
# StreamHandler binds the tee, not the original console stream.
# One file per DAY (not per run): the frequent-run schedule would interleave a single
# file into unreadability, while per-run files would scatter one day's story across
# many; size rotation is moot at a day's volume, so retention is age-based instead.
LOGS_DIR = BASE_DIR / "logs"
_LOG_KEEP_DAYS = 30


class _Tee:
    """Mirror a text stream to a second sink (the log file). Three design choices matter:

    - Both writes are best-effort: neither an unreliable stdout/stderr (a detached/invalid
      handle when the scheduler runs us without a console) nor a failing sink (the disk
      fills mid-run) may drop into an innocent print() and crash the pipeline. The sink is
      written first — it's the destination we try hardest to keep — but a failure in either
      is swallowed, so logging can degrade but never blocks the work.
    - Per-thread line buffering keeps concurrent output from garbling. print() emits a
      message and its newline as TWO write() calls, so without buffering another thread's
      write lands between them and the two lines merge ("[eval] attempt 1[eval] attempt
      2 …") — this bites exactly during an eval-retry storm, when the pool has several
      workers printing at once. Each thread accumulates its own partial line (thread-local)
      and only complete lines are emitted, as one locked write, so lines never interleave.
      flush() emits any partial so interactive/no-newline output still appears.
    - A shared lock (the same object across the stdout and stderr tees, which share one
      sink) serializes those emits across both streams.

    Other attributes (isatty, encoding, fileno, …) delegate to the real stream so
    libraries that introspect the console still see it."""

    def __init__(self, stream, sink, lock):
        self._stream = stream
        self._sink = sink
        self._lock = lock
        self._local = threading.local()  # per-thread partial-line buffer

    def _emit(self, s):
        # Caller holds the lock. Both writes are best-effort: a logging I/O failure
        # (e.g. the disk fills mid-run) must never crash the pipeline from inside an
        # innocent print() — same "logging never blocks the work" rule that guards the
        # initial open(). Sink first so the log is the destination we try hardest to keep.
        for dest in (self._sink, self._stream):
            try:
                dest.write(s)
            except Exception:
                pass

    def write(self, s):
        pending = getattr(self._local, "buf", "") + s
        if "\n" in pending:
            head, _, tail = pending.rpartition("\n")
            self._local.buf = tail          # keep the trailing incomplete line, if any
            with self._lock:
                self._emit(head + "\n")     # emit all complete lines as one atomic write
        else:
            self._local.buf = pending
        return len(s)

    def flush(self):
        with self._lock:
            partial = getattr(self._local, "buf", "")
            if partial:                     # surface a not-yet-newlined line (e.g. a prompt)
                self._local.buf = ""
                self._emit(partial)
            for dest in (self._sink, self._stream):
                try:
                    dest.flush()
                except Exception:
                    pass

    def __getattr__(self, name):
        # Delegate unknown attributes to the wrapped stream, but never for the private
        # names set in __init__: if one is accessed before __init__ runs (copy/pickle),
        # `getattr(self._stream, …)` would re-enter __getattr__ on the same name forever.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)


@contextlib.contextmanager
def run_log(label="run"):
    """Tee stdout+stderr for one CLI invocation into the day's logs/pipeline-YYYY-MM-DD.log,
    bracketed by the same session markers the scheduler used to write. UTF-8/errors='replace'
    (matching the stream reconfigure above) so a stray glyph degrades instead of crashing.

    On an uncaught exception the full traceback is written to the log before the streams
    are restored — the interpreter prints it only after this context exits, to the
    by-then-restored original stderr, which the scheduler discards; without this the log
    would record only the exception's class name. The end marker is always written and the
    streams always restored; the exception then propagates unchanged."""
    LOGS_DIR.mkdir(exist_ok=True)
    # The sink is dated per INVOCATION (not at import) so a long-lived process still logs
    # each run under the day it ran. Best-effort retention sweep in place of the old size
    # rotation: filename-dated files older than the window are removed; lexicographic
    # comparison is date order for this name shape, and the old fixed-name pipeline.log /
    # pipeline.log.1 never match the cutoff pattern, so they are simply left behind.
    log_path = LOGS_DIR / f"pipeline-{date.today():%Y-%m-%d}.log"
    cutoff = f"pipeline-{date.fromordinal(date.today().toordinal() - _LOG_KEEP_DAYS):%Y-%m-%d}.log"
    # Retention must never block the run — and one stuck file must never block the sweep:
    # the try sits INSIDE the loop, so a locked/read-only old log (editor, AV scan) or a file
    # a concurrently-overlapping run already deleted (missing_ok) skips just that file, not
    # every file after it.
    try:
        old_logs = list(LOGS_DIR.glob("pipeline-????-??-??.log"))
    except OSError:
        old_logs = []
    for old in old_logs:
        if old.name < cutoff:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass

    # Logging must never block the actual work: if the file can't be opened (e.g. locked by
    # an overlapping run on Windows), run WITHOUT file capture instead of aborting the run.
    # Line-buffered (buffering=1): _Tee emits whole lines but never flushes the sink itself,
    # so with default block buffering a hard kill (timeout, closed laptop) silently discards
    # the last ~8KB — the very tail that says where the run died. Log volume is tiny; the
    # per-line flush is noise.
    try:
        sink = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[run_log] could not open {log_path} ({e}); running without file capture",
              file=sys.stderr)
        yield
        return

    lock = threading.Lock()
    saved_out, saved_err = sys.stdout, sys.stderr
    status = "ok"
    try:
        sink.write(f"\n===== {label} started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        sink.flush()
        sys.stdout = _Tee(saved_out, sink, lock)
        sys.stderr = _Tee(saved_err, sink, lock)
        yield
    except BaseException as e:  # KeyboardInterrupt/SystemExit included — capture + stamp
        status = type(e).__name__
        # Write straight to the sink (not via the tee) so it lands in the file without
        # also double-printing to the console the interpreter will print to anyway.
        try:
            sink.write(traceback.format_exc())
        except Exception:
            pass
        raise
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        # Everything here must be swallowed: an exception raised in this finally (from the
        # end-marker write/flush OR from close(), which re-flushes buffered data and can
        # itself fail on a full disk) would REPLACE the real pipeline exception propagating
        # out of the run. Guard close() too, not just the write.
        try:
            sink.write(f"===== {label} ended   {datetime.now():%Y-%m-%d %H:%M:%S} ({status}) =====\n")
            sink.flush()
        except Exception:
            pass
        try:
            sink.close()
        except Exception:
            pass


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
            source       TEXT     -- where the posting came from: 'linkedin' | 'adzuna' | an ATS board ('greenhouse' | 'lever' | 'ashby')
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
        ("source", "TEXT"),  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby' — multi-source provenance
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
