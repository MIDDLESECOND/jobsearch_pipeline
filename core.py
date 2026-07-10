#!/usr/bin/env python3
"""Shared foundation for the pipeline: paths, the cross-cutting constants, config loading,
the SQLite open/schema/migration path, and the API-key resolver.

Near the base of the module DAG — it imports only chain.py (for the fingerprint normalization
used by the one-time backfill/recompute), states.py (the status/verdict enums for the schema
CHECKs), and the stdlib. fetch.py, evaluation.py, filters.py, report.py, and pipeline.py all
import FROM here; nothing here imports them (so there are no cycles).
"""

import contextlib
import os
import re
import sqlite3
import sys
import threading
import traceback
from datetime import date, datetime
from pathlib import Path

import yaml

from chain import _norm_company, _norm_title, _fingerprint, _NORM_VERSION
from states import STATUSES, VERDICTS, sql_list

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

# The status/verdict/gate vocabulary lives in states.py (the leaf module) so chain.py can
# use it too without a cycle; import enums from there, not from here.


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

# settings key -> required type. Only the keys every run dereferences unconditionally —
# the optional blocks (adzuna:, ats:) keep their own tolerant per-key guards in fetch.py,
# because absent/partial is a VALID state for them, not an error.
_REQUIRED_SETTINGS = {
    "location": str,
    "hours_old": (int, float),
    "results_per_search": (int, float),
    "delay_between_searches": (int, float),
    "provider": str,
    "model": str,
    "max_description_chars": (int, float),
    "db_path": str,
    "reports_dir": str,
}
_PROVIDER_MODEL_PREFIX = {"anthropic": "claude", "deepseek": "deepseek"}


def validate_config(cfg):
    """Collect every shape problem and raise ONE ValueError listing them all — so a config
    typo dies at startup with a usable message instead of a KeyError deep inside a fetch
    stage (or after eval money was spent). Checks presence/type/consistency only; unknown
    keys are fine (forward-compatible). Returns cfg unchanged when valid."""
    errors = []
    if not isinstance(cfg, dict) or not isinstance(cfg.get("settings"), dict):
        raise ValueError("config.yaml must be a mapping with a `settings:` section "
                         "(copy config.example.yaml to config.yaml and edit)")
    s = cfg["settings"]
    for key, typ in _REQUIRED_SETTINGS.items():
        if s.get(key) is None:
            errors.append(f"settings.{key} is missing")
        elif isinstance(s[key], bool) or not isinstance(s[key], typ):
            want = typ.__name__ if isinstance(typ, type) else "a number"
            errors.append(f"settings.{key} should be {want}, got {s[key]!r}")
    provider, model = s.get("provider"), s.get("model")
    prefix = _PROVIDER_MODEL_PREFIX.get(provider)
    if isinstance(provider, str) and prefix is None:
        errors.append(f"settings.provider must be one of "
                      f"{sorted(_PROVIDER_MODEL_PREFIX)}, got {provider!r}")
    # The documented footgun: provider/model out of sync would send every posting to the
    # wrong endpoint and fail the whole batch through its retries. Caught here, pre-spend.
    if prefix and isinstance(model, str) and not model.startswith(prefix):
        errors.append(f"settings.provider '{provider}' expects a '{prefix}-*' model, "
                      f"got '{model}'")
    searches = cfg.get("searches")
    if not isinstance(searches, list):
        errors.append("`searches:` must be a list (it may be empty for an ATS-only setup)")
    else:
        for i, search in enumerate(searches):
            label = f"searches[{i}]"
            if not isinstance(search, dict):
                errors.append(f"{label} should be a mapping with name/term")
                continue
            for req in ("name", "term"):
                v = search.get(req)
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{label}.{req} is missing or empty")
            ms = search.get("min_salary")
            if ms is not None and (isinstance(ms, bool) or not isinstance(ms, (int, float))):
                errors.append(f"{label}.min_salary should be a number, got {ms!r}")
    if errors:
        raise ValueError("config.yaml problems:\n  - " + "\n  - ".join(errors))
    return cfg


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return validate_config(yaml.safe_load(f))


_wal_warned = False  # connect_db's not-in-WAL-mode warning fires once per process


def connect_db(cfg):
    """A plain connection (row factory + busy timeout + WAL), with NO schema/migration work — for
    callers that open a connection per request (the web UI), where re-running the idempotent
    DDL/migration pass on every request is pure waste. Run get_db once at process start to
    ensure the schema, then connect_db afterwards.

    The timeout (busy-wait on a locked DB) is raised above the 5s default so a concurrent
    open during the one-time fingerprint recompute waits it out instead of erroring."""
    conn = sqlite3.connect(BASE_DIR / cfg["settings"]["db_path"], timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL, so readers and the writer never block each other. In the default rollback-journal
    # mode ANY reader blocks a writer's COMMIT — a slow UI table scan concurrent with a run
    # once held its read lock past the 30s timeout above, and the eval write it starved was
    # discarded (the paid result lost, the row retried next run). WAL leaves only
    # writer-vs-writer contention, which that timeout comfortably covers. The mode persists
    # in the DB file (re-issuing it on a converted DB is an instant no-op); the
    # jobs.db-wal/-shm sidecars it creates are part of the database while anything holds it
    # open (gitignored — never delete a hot -wal, it carries committed rows).
    try:
        # The one-time conversion needs a brief exclusive lock. Don't let it inherit the
        # 30s busy timeout: during the conversion window a long-lived concurrent reader
        # would stall EVERY open — each UI request — for the full 30s. Cap the wait at 1s;
        # a lost race degrades below and the next connect retries the switch.
        conn.execute("PRAGMA busy_timeout=1000")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.OperationalError as e:  # locked/read-only. Anything else (a corrupt DB
        mode = f"switch failed: {e}"       # file raises DatabaseError) fails loud instead.
    finally:
        conn.execute("PRAGMA busy_timeout=30000")  # restore parity with timeout=30 above
    global _wal_warned
    if mode != "wal" and not _wal_warned:
        _wal_warned = True  # once per process — a stuck conversion must not spam per request
        print(f"[db] not in WAL mode ({mode}); a concurrent reader can starve writes past "
              "the busy timeout until a later connect completes the switch", file=sys.stderr)
    return conn


def _jobs_table_sql(name, if_not_exists=False):
    """The ONE authoritative jobs DDL — used by get_db's CREATE IF NOT EXISTS and by
    _rebuild_for_stale_checks' table swap, so the two can never drift. The status/verdict
    CHECKs are enforced on fresh databases; a pre-CHECK DB is covered by the code-side
    states.py constants, and a DB whose baked-in CHECK has fallen behind a grown
    STATUSES/VERDICTS is rebuilt once by _rebuild_for_stale_checks (a stale CHECK doesn't
    just under-enforce — it REJECTS newly-legal values and aborts the run)."""
    _status_ck = sql_list(STATUSES)
    _verdict_ck = sql_list(VERDICTS)
    ine = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {ine}{name} (
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
            status       TEXT CHECK (status IN ({_status_ck})),   -- see states.py
            verdict      TEXT CHECK (verdict IN ({_verdict_ck})), -- see states.VERDICT_FAVOR
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
            outcome_status TEXT,  -- chain's latest post-apply event_type (cache — chain._recompute_outcome; no CHECK, see states.py)
            outcome_date TEXT,    -- that event's event_date
            resume_variant TEXT,  -- free text: which resume variant went out
            channel      TEXT,    -- how the application went out: states.ALL_CHANNELS (applied-only, code-side enforced, no CHECK — see states.py)
            filter_source TEXT,   -- NULL | manual | rule:<name>  (hard-fail override)
            filter_gate  TEXT,    -- which gate the override represents
            filter_date  TEXT,    -- date the override was set
            source       TEXT     -- where the posting came from: 'linkedin' | 'adzuna' | an ATS board ('greenhouse' | 'lever' | 'ashby')
        )
    """


def _events_table_sql():
    """Post-application outcome history: one row per user-recorded event (interview, offer,
    ghosted, a bare note, …). Append-only; keyed to the chain's CANONICAL url at write time
    and read chain-wide via chain.chain_events, so a later dupe merge unions both sides'
    histories with no data migration. event_type carries NO CHECK deliberately — it is
    user-decision vocabulary enforced code-side in chain.record_event (see states.py's
    docstring); a CHECK here would be a frozen-CHECK liability outside
    _rebuild_for_stale_checks' jobs-only scope."""
    return """
        CREATE TABLE IF NOT EXISTS app_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url    TEXT NOT NULL,   -- chain canonical at write time (see chain.record_event)
            event_type TEXT NOT NULL,   -- states.ALL_EVENTS (code-side enforced, no CHECK)
            event_date TEXT NOT NULL,   -- YYYY-MM-DD, user-supplied (defaults to today)
            note       TEXT,
            created_at TEXT NOT NULL    -- ISO timestamp; insertion-order tiebreak for undo
        )
    """


def get_db(cfg):
    conn = connect_db(cfg)
    conn.execute(_jobs_table_sql("jobs", if_not_exists=True))
    conn.execute(_events_table_sql())
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_events_job_url ON app_events(job_url)")
    _migrate(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)")
    # repost_of is scanned per-decision by _chain_targets and per-row by _repost_info / cmd_report;
    # index it so chain resolution is O(chain) not O(table).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repost_of ON jobs(repost_of)")
    # Every run stage and both repost-skip reconcile passes gate on status; without this the
    # reverse passes (and the per-click dupe sweeps in the web UI) full-scan the table.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON jobs(status)")
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
        # Outcome-tracking cache (v4): the chain's post-application state, denormalized onto
        # every member like app_status so readers/SQL need no join. Always a pure recompute
        # of (chain applied?, app_events) — chain._recompute_outcome is the ONE writer.
        # NULL on an applied row = "no outcome recorded" (the follow-up bucket). No backfill.
        ("outcome_status", "TEXT"),   # latest non-note event_type across the chain, or NULL
        ("outcome_date", "TEXT"),     # that event's event_date
        ("resume_variant", "TEXT"),   # free text: which resume went out (set at apply time)
        ("channel", "TEXT"),          # states.ALL_CHANNELS: direct | agency | referral (applied-only)
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
    _rebuild_for_stale_checks(conn)
    _backfill_fingerprints(conn)
    _recompute_fingerprints(conn)


def _check_clause_values(sql, column):
    """The raw text inside `column`'s CHECK (... IN (<here>)) clause of a stored CREATE TABLE,
    or None when that column has no CHECK. Probing PER CLAUSE matters: the stored DDL keeps
    column comments (which quote words like 'manual', 'linkedin', 'adzuna') and the OTHER
    vocabulary's CHECK — a whole-DDL substring test would let any such text mask a value
    genuinely missing from its own CHECK, silently disabling the rebuild. Identifier quotes
    are tolerated (["'`]?): third-party DB tools commonly rewrite DDL with quoted column
    names on their own table edits, and a probe that reads that as 'no CHECK' would skip the
    rebuild while the stale CHECK keeps aborting every run."""
    q = r"[\"'`]?"
    m = re.search(
        rf"\b{q}{column}{q}[^,]*?CHECK\s*\(\s*{q}{column}{q}\s+IN\s*\(([^)]*)\)", sql)
    return m.group(1) if m else None


def _rebuild_for_stale_checks(conn):
    """One-shot table swap for a DB whose baked-in status/verdict CHECK predates the current
    STATUSES/VERDICTS. SQLite freezes CHECKs at CREATE TABLE time and can't ALTER them, so a
    DB created under an older vocabulary REJECTS a newly-added legal value (IntegrityError in
    the deliberately-unguarded run stages — every run aborts). Idempotent: no-op for
    pre-CHECK DBs (code-side constants remain their enforcement) and for DBs whose CHECKs
    already name every current value. Runs AFTER the column migrations so the old table
    carries the full current column set."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    sql = (row[0] if row else "") or ""
    if "CHECK" not in sql:
        return
    missing = []
    parsed_any = False
    for column, vocab in (("status", STATUSES), ("verdict", VERDICTS)):
        clause = _check_clause_values(sql, column)
        if clause is None:
            continue  # this column carries no parseable CHECK
        parsed_any = True
        missing += [v for v in vocab if f"'{v}'" not in clause]
    if not parsed_any:
        # A CHECK exists but neither clause parses (table-level CHECK, exotic tool-rewritten
        # DDL). Unverifiable staleness is treated as stale: the rebuild is idempotent and
        # one-shot (its own output parses), while skipping could leave a stale CHECK aborting
        # every run with no message.
        missing = ["<unparseable CHECK — rebuilding to the canonical schema>"]
    if not missing:
        return
    # A row holding a value OUTSIDE the current vocabulary (a renamed/removed status, a
    # hand-edited typo) would make the copy below raise a bare IntegrityError inside get_db —
    # bricking every command, including read-only ones, with no way to fix the data first.
    # Fail loud and actionable instead.
    _status_sql = sql_list(STATUSES)
    _verdict_sql = sql_list(VERDICTS)
    bad = conn.execute(
        f"SELECT DISTINCT status FROM jobs WHERE status IS NOT NULL "
        f"AND status NOT IN ({_status_sql}) "
        f"UNION SELECT DISTINCT verdict FROM jobs WHERE verdict IS NOT NULL "
        f"AND verdict NOT IN ({_verdict_sql})"
    ).fetchall()
    if bad:
        vals = ", ".join(repr(r[0]) for r in bad)
        # The OLD table's frozen CHECK rejects any value it doesn't already contain, so
        # "update to the new value" would itself raise IntegrityError — the escape hatch is a
        # value present in BOTH vocabularies (empirically verified: updating to one, then
        # rerunning, rebuilds cleanly).
        both = [v for v in (*STATUSES, *VERDICTS)
                if any(f"'{v}'" in (_check_clause_values(sql, c) or "")
                       for c in ("status", "verdict"))]
        hint = (f" (e.g. {', '.join(repr(v) for v in both[:4])})" if both else "")
        raise RuntimeError(
            f"jobs.db needs a schema rebuild (CHECK lacks: {', '.join(missing)}) but holds "
            f"off-vocabulary values the current CHECKs would reject: {vals}. "
            f"UPDATE those rows to a value the OLD schema also accepts{hint}, "
            f"then rerun — the rebuild will then widen the CHECKs."
        )
    # BEGIN IMMEDIATE: make the swap's atomicity EXPLICIT rather than an accident of Python
    # sqlite3's legacy implicit-transaction mode (under autocommit semantics, a crash between
    # DROP TABLE jobs and the RENAME would leave no jobs table — the next startup would mint
    # an empty one and strand all data in jobs_new). Also blocks a concurrent writer mid-swap.
    conn.execute("DROP TABLE IF EXISTS jobs_new")  # leftover from a crashed prior rebuild
    if conn.in_transaction:
        # Pin the invariant the BEGIN below depends on: every migration step before this one
        # commits its work. An uncommitted statement here would make BEGIN raise.
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(_jobs_table_sql("jobs_new"))
    # One PRAGMA read feeds both views of the old table (names for the copy list, full row
    # for type/NOT NULL/DEFAULT reconstruction) so the two can't diverge.
    old_info = {r[1]: r for r in conn.execute("PRAGMA table_info(jobs)")}
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs_new)")}
    # A hand-ALTERed extra column must survive the swap — recreate it on jobs_new rather than
    # silently dropping the user's data (this is a single-user tool; ad-hoc columns happen).
    # Identifiers are quoted (a column named `order` or "my notes" is legal), and the
    # NOT NULL/DEFAULT halves of the declaration are carried from PRAGMA table_info.
    for col, info in old_info.items():
        if col not in new_cols:
            decl = info[2] or "TEXT"
            if info[4] is not None:   # dflt_value — already SQL-literal text
                decl += f" DEFAULT {info[4]}"
            if info[3] and info[4] is not None:  # notnull — ADD COLUMN requires a default
                decl += " NOT NULL"
            elif info[3]:
                print(f"[migrate] note: NOT NULL on jobs.{col} could not be carried "
                      f"(no default) — data preserved, constraint dropped")
            conn.execute(f'ALTER TABLE jobs_new ADD COLUMN "{col}" {decl}')
            print(f"[migrate] preserved non-standard column jobs.{col} through the rebuild")
    cols_sql = ", ".join(f'"{c}"' for c in old_info)
    conn.execute(f"INSERT INTO jobs_new ({cols_sql}) SELECT {cols_sql} FROM jobs")
    conn.execute("DROP TABLE jobs")  # also drops the indexes; get_db recreates them after
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
    conn.commit()
    print(f"[migrate] rebuilt jobs table — stale CHECK constraint lacked: {', '.join(missing)}")


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
