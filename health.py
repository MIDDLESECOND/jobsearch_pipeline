"""Structured pipeline-run health and descriptive search-attribution read models.

The module stores counts and exception *types*, never exception messages. Source adapters may
return :class:`FetchSummary`, an ``int`` subclass that preserves their historical public return
contract while adding per-configured-unit success/failure facts for the run recorder.
"""

import hashlib
import json
import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


MAX_HEALTH_DAYS = 365
MAX_RUN_HISTORY = 100
MAX_EFFECTIVENESS_ROWS = 500
MAX_ATTEMPTS_IN_RESPONSE = 1_000
# A run whose process died leaves its row at 'running' forever: the recorder only ever writes a
# terminal status from inside the process it is recording. Measured 2026-08-16, 5 of 44 runs
# (11%) sat that way — the oldest for 8 days — which made "is a run in flight" unanswerable and
# hid the death rate entirely. STALE_RUN_HOURS is the age past which a 'running' row is read as
# dead rather than slow. It must exceed the longest LEGITIMATE run by a wide margin: eval-heavy
# cycles paying off a peak-deferral backlog have measured 85–194 minutes, and concurrent runs are
# deliberately unguarded here, so a live run must never be reaped by another one starting.
STALE_RUN_HOURS = 12
# --- Scheduled-producer silence thresholds (staleness_readings) --------------------------
# The 2026-08-18 seam audit found the silent-death direction completely uninstrumented: a
# whole log day (pipeline-2026-07-28.log) absent from an otherwise daily sequence with
# nothing noticing, a canary schedule that never once executed (its history is all manual
# runs), and the only arithmetic ever applied to meta.last_run_ok_ended being the cooldown
# guard — which protects against running too OFTEN, never too RARELY. These thresholds are
# the too-rarely direction. Each is sized to its producer's schedule cadence: one missed
# slot plus slack, so a single late slot stays quiet but a dead schedule does not.
RUN_SILENCE_HOURS = 26            # pipeline runs several times a day; >26h = a whole day of
                                  # slots missed, with slack for slot jitter
CANARY_SILENCE_HOURS = 8 * 24     # canary is a weekly schedule; >8 days = the weekly slot
                                  # missed plus a day of slack
SECOND_JUDGE_SILENCE_HOURS = 48   # second judge runs daily; >48h = two missed days. A
                                  # legitimately empty actionable zone also ages this
                                  # reading — the sentinel reports the fact, the reader
                                  # decides what it means.
# Where tests/validation/canary.py appends its run history (HISTORY_PATH there — change one,
# change both). Spelled here rather than imported: tests/validation is deliberately not an
# importable package, and this module stays stdlib-only. The parent of this file is the repo
# root, the same value as core.BASE_DIR.
CANARY_HISTORY_PATH = (Path(__file__).resolve().parent
                       / "tests" / "validation" / "results" / "canary_history.jsonl")
_RUN_STATUSES = {"running", "succeeded", "degraded", "failed", "interrupted", "skipped",
                 "abandoned"}
_ATTEMPT_STATUSES = {"success", "failed", "skipped"}
_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
_ACTIVE_RUN_ID: ContextVar[int | None] = ContextVar(
    "pipeline_health_run_id", default=None,
)


class FetchSummary(int):
    """Inserted count plus source-unit outcomes, while remaining integer-compatible."""

    units: int
    successes: int
    failures: int
    skipped_reason: str | None
    error_type: str | None

    def __new__(cls, inserted: int, *, units: int, successes: int, failures: int,
                skipped_reason: str | None = None,
                error_type: str | None = None) -> "FetchSummary":
        values = (inserted, units, successes, failures)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values):
            raise ValueError("fetch summary counts must be non-negative integers")
        if successes + failures != units:
            raise ValueError("fetch summary successes and failures must sum to units")
        if skipped_reason and units:
            raise ValueError("a skipped fetch summary cannot also contain units")
        obj = int.__new__(cls, inserted)
        obj.units = units
        obj.successes = successes
        obj.failures = failures
        obj.skipped_reason = _bounded_reason(skipped_reason)
        obj.error_type = _safe_token(error_type, "error type") if error_type else None
        return obj

    @property
    def status(self):
        if self.skipped_reason is not None:
            return "skipped"
        if self.failures and self.successes:
            return "partial"
        if self.failures:
            return "failed"
        return "completed"

    @classmethod
    def skipped(cls, reason):
        return cls(0, units=0, successes=0, failures=0, skipped_reason=reason)

    @classmethod
    def failed(cls, error_type):
        return cls(0, units=1, successes=0, failures=1, error_type=error_type)


def _bounded_reason(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    if not value or len(value) > 200:
        raise ValueError("skip reason must be 1..200 characters")
    return value


def _safe_token(value, label):
    value = str(value or "")
    if not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_now_iso():
    return _iso_now()


def fetch_definition_hash(value):
    """Hash a secret-free target definition so same-label config changes remain visible."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_error_kind(exc):
    """Classify without reading or persisting the exception message."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "timeout" if isinstance(exc, TimeoutError) else "connection"
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 429:
            return "http_429"
        if 400 <= code < 500:
            return "http_4xx"
        if 500 <= code < 600:
            return "http_5xx"
        return "http_other"
    if isinstance(exc, (KeyError, TypeError, ValueError, json.JSONDecodeError)):
        return "parse_or_validation"
    return "unexpected"


def abandon_stale_runs(conn, *, now=None, older_than_hours=STALE_RUN_HOURS):
    """Mark 'running' rows older than `older_than_hours` as 'abandoned'. Returns the count.

    A run only ever writes its own terminal status, so a process that is killed — machine sleep
    or shutdown, a closed console, the USB-drive death — leaves its row 'running' permanently.
    Nothing else notices: the cooldown stamp (`meta.last_run_ok_ended`) is written only on full
    success, so zombies never suppress a slot, and that is exactly why they stayed invisible.

    `ended_at` is deliberately left NULL. The row's end time is genuinely unknown, and stamping
    "now" would manufacture a duration for a run that stopped hours earlier. Liveness therefore
    reads off `status`, not `ended_at IS NULL` — the two guarded UPDATEs in this module already
    require `status='running'`, so an abandoned row can never be finished or accrue attempts
    afterwards, which is the correct behaviour for a run whose process is gone.
    """
    if isinstance(older_than_hours, bool) or not isinstance(older_than_hours, (int, float)) \
            or older_than_hours <= 0:
        raise ValueError("older_than_hours must be a positive number of hours")
    try:
        if now is None:
            reference = datetime.now(timezone.utc)
        else:
            reference = datetime.fromisoformat(now) if isinstance(now, str) else now
        # start_pipeline_run writes _iso_now(), i.e. an aware UTC stamp, so the column is
        # offset-consistent and a string comparison is a time comparison. A naive `now` would
        # silently compare against those offsets, so normalize it rather than trust the caller.
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        cutoff = (reference.astimezone(timezone.utc)
                  - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("now must be an ISO timestamp or datetime") from exc
    updated = conn.execute(
        """UPDATE pipeline_runs SET status='abandoned'
            WHERE status='running' AND ended_at IS NULL AND started_at < ?""",
        (cutoff,),
    )
    conn.commit()
    return updated.rowcount


def start_pipeline_run(conn, *, trigger, run_date, started_at=None):
    """Insert a durable running marker before any source or paid stage starts.

    Reaps stale 'running' rows first: a starting run is the one moment the pipeline is certainly
    executing, which makes it the natural place to retire rows that can no longer be in flight.
    """
    if conn.in_transaction:
        raise RuntimeError("cannot start pipeline health record inside another transaction")
    abandon_stale_runs(conn)
    trigger = _safe_token(trigger, "run trigger")
    try:
        run_date = date.fromisoformat(str(run_date)).isoformat()
        started_at = datetime.fromisoformat(started_at or _iso_now()).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("run timestamps must be ISO dates/timestamps") from exc
    cursor = conn.execute(
        """INSERT INTO pipeline_runs
           (started_at,ended_at,trigger,run_date,status,error_stage,error_type)
           VALUES (?,NULL,?,?,'running',NULL,NULL)""",
        (started_at, trigger, run_date),
    )
    conn.commit()
    return cursor.lastrowid


def set_active_pipeline_run(run_id):
    """Make ``run_id`` visible to fetch adapters for this execution context."""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    return _ACTIVE_RUN_ID.set(run_id)


def reset_active_pipeline_run(token):
    _ACTIVE_RUN_ID.reset(token)


def current_pipeline_run_id():
    return _ACTIVE_RUN_ID.get()


def record_active_fetch_attempt(conn, **fields):
    """No-op outside a recorded pipeline run, preserving direct fetcher/test use."""
    run_id = current_pipeline_run_id()
    if run_id is None:
        return None
    return record_fetch_attempt(conn, run_id=run_id, **fields)


def _optional_count(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return value


def _target_label(value):
    value = " ".join(str(value or "").split())
    if not value or len(value) > 200:
        raise ValueError("target label must be 1..200 characters")
    return value


def record_fetch_attempt(conn, *, run_id, source_family, target_kind, target_label,
                         definition_hash, status, returned_count=None,
                         eligible_count=None, inserted_count=None, repost_count=None,
                         skip_reason=None, error_kind=None, started_at=None,
                         ended_at=None, commit=True):
    """Record one configured fetch unit without raw request or exception text.

    Successful target rows may be inserted with ``commit=False`` immediately before the
    fetcher's existing target commit, making the job rows and success fact one transaction.
    Failed targets are recorded only after their partial job transaction has been rolled back.
    """
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    active_run = conn.execute(
        """SELECT 1 FROM pipeline_runs
            WHERE id=? AND status='running' AND ended_at IS NULL""",
        (run_id,),
    ).fetchone()
    if active_run is None:
        raise ValueError("pipeline run is missing or already finished")
    source_family = _safe_token(source_family, "source family")
    target_kind = _safe_token(target_kind, "target kind")
    target_label = _target_label(target_label)
    if definition_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", definition_hash):
        raise ValueError("definition_hash must be a lowercase SHA-256 or null")
    if status not in _ATTEMPT_STATUSES:
        raise ValueError("invalid fetch-attempt status")
    counts = tuple(_optional_count(value, label) for value, label in (
        (returned_count, "returned_count"), (eligible_count, "eligible_count"),
        (inserted_count, "inserted_count"), (repost_count, "repost_count"),
    ))
    if status == "success":
        if skip_reason is not None or error_kind is not None:
            raise ValueError("successful attempt cannot carry skip/error attribution")
    elif any(value is not None for value in counts):
        raise ValueError("failed/skipped attempts must leave result counts null")
    if status == "failed":
        error_kind = _safe_token(error_kind, "error kind")
        if skip_reason is not None:
            raise ValueError("failed attempt cannot carry a skip reason")
    elif status == "skipped":
        skip_reason = _bounded_reason(skip_reason)
        if error_kind is not None:
            raise ValueError("skipped attempt cannot carry an error kind")
    try:
        started_at = datetime.fromisoformat(started_at or _iso_now()).isoformat()
        ended_at = datetime.fromisoformat(ended_at or _iso_now()).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("attempt timestamps must be ISO timestamps") from exc
    cursor = conn.execute(
        """INSERT INTO pipeline_fetch_attempts
           (run_id,source_family,target_kind,target_label,definition_hash,started_at,ended_at,
            status,skip_reason,error_kind,returned_count,eligible_count,inserted_count,repost_count)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, source_family, target_kind, target_label, definition_hash,
         started_at, ended_at, status, skip_reason, error_kind, *counts),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid


def finish_pipeline_run(conn, run_id, *, status, ended_at=None,
                        error_stage=None, error_type=None, eval_deferred=False):
    """Finish exactly one still-running record without retaining exception messages.

    `eval_deferred` marks a cycle that completed WITHOUT running the paid eval stage
    (the DeepSeek peak-rate window). It is a separate fact from `status`, which keeps
    describing fetch health only."""
    if status not in _RUN_STATUSES - {"running"}:
        raise ValueError("invalid terminal pipeline run status")
    if status in {"failed", "interrupted"}:
        error_stage = _safe_token(error_stage, "error stage")
        error_type = _safe_token(error_type, "error type")
    elif error_stage is not None or error_type is not None:
        raise ValueError("only failed runs may carry error attribution")
    try:
        ended_at = datetime.fromisoformat(ended_at or _iso_now()).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("ended_at must be an ISO timestamp") from exc
    updated = conn.execute(
        """UPDATE pipeline_runs
              SET ended_at=?,status=?,error_stage=?,error_type=?,eval_deferred=?
            WHERE id=? AND status='running' AND ended_at IS NULL""",
        (ended_at, status, error_stage, error_type, 1 if eval_deferred else 0, run_id),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise ValueError("pipeline run is missing or already finished")
    conn.commit()


def source_attempts_exist(conn, run_id, source_family):
    return conn.execute(
        """SELECT 1 FROM pipeline_fetch_attempts
            WHERE run_id=? AND source_family=? LIMIT 1""",
        (run_id, source_family),
    ).fetchone() is not None


def completed_run_status(conn, run_id):
    """A completed downstream cycle is degraded when any configured fetch unit failed."""
    failed = conn.execute(
        "SELECT 1 FROM pipeline_fetch_attempts WHERE run_id=? AND status='failed' LIMIT 1",
        (run_id,),
    ).fetchone()
    return "degraded" if failed else "succeeded"


def run_has_successful_target(conn, run_id):
    """True for a real successful fetch target, including a legitimate zero-result response."""
    return conn.execute(
        "SELECT 1 FROM pipeline_fetch_attempts WHERE run_id=? AND status='success' LIMIT 1",
        (run_id,),
    ).fetchone() is not None


def _configured_attributions(cfg):
    expected = set()
    for search in cfg.get("searches") or []:
        if not isinstance(search, dict) or not search.get("name"):
            continue
        name = str(search["name"])
        expected.add(("linkedin", name))
        if search.get("adzuna"):
            expected.add(("adzuna", name))
        if search.get("dice"):
            expected.add(("dice", name))
    ats = (cfg.get("settings") or {}).get("ats") or {}
    companies = ats.get("companies") or []
    if isinstance(companies, dict) or isinstance(companies, str):
        companies = [companies]
    for company in companies:
        if not isinstance(company, dict):
            continue
        slug, board = company.get("slug"), company.get("board")
        if slug and board in {"greenhouse", "lever", "ashby"}:
            expected.add((board, f"ats:{slug}"))
    return expected


def _search_effectiveness(conn, cfg, cutoff):
    # Raw posting volume belongs to the row's own stored source/search. Role outcomes are a
    # separate, unique first-touch cohort: one current chain can never give several sources
    # credit merely because it has cross-posted relistings.
    aggregates = {
        (row["source"], row["search_name"]): {
            "postings": int(row["postings"]), "roles": 0,
            "strong_roles": 0, "applied_roles": 0,
        }
        for row in conn.execute(
            """SELECT COALESCE(source,'unknown') AS source,
                      COALESCE(search_name,'(unattributed)') AS search_name,
                      COUNT(*) AS postings
                 FROM jobs WHERE date(first_seen)>=date(?)
                GROUP BY source,search_name""",
            (cutoff,),
        )
    }
    role_rows = conn.execute(
        """WITH ranked AS (
                 SELECT COALESCE(repost_of,job_url) AS root,
                        COALESCE(source,'unknown') AS source,
                        COALESCE(search_name,'(unattributed)') AS search_name,
                        first_seen,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(repost_of,job_url)
                            ORDER BY (julianday(first_seen) IS NULL),
                                     julianday(first_seen),first_seen,job_url
                        ) AS position
                   FROM jobs
             ), chain_state AS (
                 SELECT COALESCE(repost_of,job_url) AS root,
                        MAX(CASE WHEN status='evaluated' AND verdict='PASS'
                                 THEN 1 ELSE 0 END) AS strong,
                        MAX(CASE WHEN app_status='applied' THEN 1 ELSE 0 END) AS applied
                   FROM jobs GROUP BY COALESCE(repost_of,job_url)
             )
             SELECT f.source,f.search_name,COUNT(*) AS roles,
                    SUM(c.strong) AS strong_roles,SUM(c.applied) AS applied_roles
               FROM ranked f JOIN chain_state c ON c.root=f.root
              WHERE f.position=1 AND date(f.first_seen)>=date(?)
              GROUP BY f.source,f.search_name""",
        (cutoff,),
    ).fetchall()
    for row in role_rows:
        key = (row["source"], row["search_name"])
        values = aggregates.setdefault(key, {
            "postings": 0, "roles": 0, "strong_roles": 0, "applied_roles": 0,
        })
        values.update({
            "roles": int(row["roles"]),
            "strong_roles": int(row["strong_roles"] or 0),
            "applied_roles": int(row["applied_roles"] or 0),
        })
    last_seen = {
        (row["source"], row["search_name"]): row["last_discovered_at"]
        for row in conn.execute(
            """WITH ranked AS (
                   SELECT COALESCE(source,'unknown') AS source,
                          COALESCE(search_name,'(unattributed)') AS search_name,
                          first_seen,
                          ROW_NUMBER() OVER (
                              PARTITION BY COALESCE(source,'unknown'),
                                           COALESCE(search_name,'(unattributed)')
                              ORDER BY julianday(first_seen) DESC,first_seen DESC,job_url DESC
                          ) AS position
                     FROM jobs
               )
               SELECT source,search_name,first_seen AS last_discovered_at
                 FROM ranked WHERE position=1"""
        )
    }
    expected = _configured_attributions(cfg)
    keys = expected | set(aggregates) | set(last_seen)
    items = []
    for source, search_name in keys:
        values = aggregates.get((source, search_name), {})
        items.append({
            "source": source,
            "search_name": search_name,
            "configured": (source, search_name) in expected,
            "postings": values.get("postings", 0),
            "roles": values.get("roles", 0),
            "strong_roles": values.get("strong_roles", 0),
            "applied_roles": values.get("applied_roles", 0),
            "last_discovered_at": last_seen.get((source, search_name)),
        })
    items.sort(key=lambda item: (
        not item["configured"], -item["roles"], item["source"], item["search_name"]
    ))
    total = len(items)
    return {
        "items": items[:MAX_EFFECTIVENESS_ROWS], "total": total,
        "truncated": total > MAX_EFFECTIVENESS_ROWS,
    }


def _duration_seconds(started_at, ended_at):
    if not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        if (start.tzinfo is None) != (end.tzinfo is None):
            return None
        return max(0, int((end - start).total_seconds()))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _run_history(conn, limit):
    runs = conn.execute(
        "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    sources_by_run = {row["id"]: [] for row in runs}
    attempts_by_run = {row["id"]: [] for row in runs}
    attempt_totals_by_run = {row["id"]: 0 for row in runs}
    if runs:
        placeholders = ",".join("?" for _ in runs)
        for row in conn.execute(
            f"""SELECT run_id,source_family,
                        SUM(CASE WHEN status<>'skipped' THEN 1 ELSE 0 END) AS attempted,
                        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                        SUM(CASE WHEN status='success' THEN COALESCE(inserted_count,0)
                                 ELSE 0 END) AS inserted,
                        GROUP_CONCAT(DISTINCT error_kind) AS error_kinds
                   FROM pipeline_fetch_attempts
                  WHERE run_id IN ({placeholders})
                  GROUP BY run_id,source_family ORDER BY run_id DESC,source_family""",
            tuple(sources_by_run),
        ):
            if row["failed"] and row["succeeded"]:
                status = "partial"
            elif row["failed"]:
                status = "failed"
            elif row["succeeded"]:
                status = "completed"
            else:
                status = "skipped"
            sources_by_run[row["run_id"]].append({
                "source": row["source_family"], "status": status,
                "attempted": int(row["attempted"]), "succeeded": int(row["succeeded"]),
                "failed": int(row["failed"]), "skipped": int(row["skipped"]),
                "inserted": int(row["inserted"]),
                "error_kinds": sorted((row["error_kinds"] or "").split(","))
                               if row["error_kinds"] else [],
            })
            attempt_totals_by_run[row["run_id"]] += (
                int(row["attempted"]) + int(row["skipped"])
            )
        for row in conn.execute(
            f"""SELECT id,run_id,source_family,target_kind,target_label,started_at,ended_at,
                        status,skip_reason,error_kind,returned_count,eligible_count,
                        inserted_count,repost_count
                   FROM pipeline_fetch_attempts
                  WHERE run_id IN ({placeholders})
                  ORDER BY id DESC LIMIT ?""",
            (*tuple(attempts_by_run), MAX_ATTEMPTS_IN_RESPONSE),
        ):
            attempts_by_run[row["run_id"]].append({
                key: row[key] for key in (
                    "id", "source_family", "target_kind", "target_label", "started_at",
                    "ended_at", "status", "skip_reason", "error_kind",
                    "returned_count", "eligible_count", "inserted_count", "repost_count",
                )
            })
    return [{
        "id": row["id"], "started_at": row["started_at"],
        "ended_at": row["ended_at"], "trigger": row["trigger"],
        "run_date": row["run_date"], "status": row["status"],
        "error_stage": row["error_stage"], "error_type": row["error_type"],
        # A completed cycle that skipped the paid eval stage on purpose — the run is
        # 'succeeded', so without this flag it reads as a fully evaluated slot.
        "eval_deferred": bool(row["eval_deferred"]),
        "completion_recorded": row["ended_at"] is not None,
        "duration_seconds": _duration_seconds(row["started_at"], row["ended_at"]),
        "sources": sources_by_run[row["id"]],
        "attempts": attempts_by_run[row["id"]],
        "attempts_total": attempt_totals_by_run[row["id"]],
        "attempts_truncated": (
            len(attempts_by_run[row["id"]]) < attempt_totals_by_run[row["id"]]
        ),
    } for row in runs]


def _wall_clock(value):
    """One stored stamp as naive local wall time, or None when unreadable.

    Every stamp this sentinel reads is naive local (`meta.last_run_ok_ended` and
    `second_opinions.collected_at` are both written by datetime.now()) EXCEPT the canary
    history's `ts`, which is aware UTC. Aware values are a valid spelling of a real
    instant, so they normalize into the naive-local frame the others already use — the
    same direction as pipeline._cooldown_active, and the inverse of abandon_stale_runs
    (whose column is aware UTC). Comparing the two frames unnormalized would silently
    shift every canary age by the UTC offset.
    """
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp


def _latest_canary_entry(path):
    """Raw `ts` of the newest readable canary history entry, or None.

    Tolerant on purpose: an absent file, an unreadable or undecodable file, and garbage
    lines all degrade to "no readable evidence", never to a crash in the report/UI read
    path — and a readable entry behind a garbage line still counts.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    # UnicodeDecodeError is a ValueError, not an OSError: a partially flushed or
    # corruptly read history file must degrade like a missing one, not crash the
    # deliberately unguarded report stage.
    except (OSError, UnicodeDecodeError):
        return None
    newest_raw, newest = None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        raw = entry.get("ts") if isinstance(entry, dict) else None
        stamp = _wall_clock(raw)
        if stamp is not None and (newest is None or stamp > newest):
            newest_raw, newest = raw, stamp
    return newest_raw


def _silence_reading(signal, raw_stamp, reference, threshold_hours):
    """One reading from one raw stored stamp — parsing lives HERE, so a caller can never
    pair a `last_at` with an `age_hours` computed from a different stamp."""
    stamp = _wall_clock(raw_stamp)
    if stamp is None:
        # An absent or unreadable stamp surfaces as stale-with-no-timestamp, never as
        # quiet health: this sentinel's failure direction is "surface" — the deliberate
        # inverse of the cooldown guard's fail-open — because a swallowed read error IS
        # the blind spot it exists to close.
        return {"signal": signal, "last_at": None, "age_hours": None,
                "threshold_hours": threshold_hours, "stale": True}
    age = (reference - stamp).total_seconds() / 3600.0
    # age_hours is clamped for display — a future-dated stamp (clock rollback) would
    # otherwise surface as "-3h ago". Staleness compares the RAW age: a future stamp is
    # simply not stale yet, matching _cooldown_active's tolerance of clock skew.
    return {"signal": signal, "last_at": raw_stamp, "age_hours": round(max(age, 0.0), 1),
            "threshold_hours": threshold_hours, "stale": age > threshold_hours}


def failed_fetch_targets(conn, run_date):
    """The day's distinct failed fetch targets: (source_family, target_label) rows.

    Day-scoped through pipeline_runs.run_date, and deliberately without recovery
    awareness — a target that failed at one slot and succeeded at the next still failed
    that day (the per-run source view above is where recovery detail lives). Lives here,
    not in its consumer: every production read of pipeline_fetch_attempts belongs to
    this module, so an attempt-status vocabulary change can't miss a stray copy.
    """
    return conn.execute(
        """SELECT DISTINCT a.source_family, a.target_label
             FROM pipeline_fetch_attempts a JOIN pipeline_runs r ON r.id=a.run_id
            WHERE r.run_date=? AND a.status='failed'""", (str(run_date),)
    ).fetchall()


def staleness_readings(conn, *, now=None, canary_history_path=None):
    """How long since each scheduled producer last proved it ran — a read-only sentinel.

    Three durable proofs of life, each against a threshold sized to its schedule (the
    module constants above): the cooldown stamp `meta.last_run_ok_ended` (only a full
    successful pipeline cycle writes it), the newest entry of the canary drift history,
    and the newest `second_opinions.collected_at` (any collection activity, success or
    error). Purely descriptive: it mutates nothing, registers or repairs no schedule,
    and a stale reading is a fact to investigate, never an automatic action.

    `now` is injectable for tests (ISO string or datetime; aware values normalize to
    naive local exactly like the stamps — see _wall_clock). Stale is strictly "older
    than the threshold", so a reading exactly at its bar has not yet alerted. This ships
    numbers and signal names only; the human wording per signal lives with each surface
    (report._SILENCE_PHRASES and index.html's staleBits label map — a new signal added
    here degrades to a generic phrase there until both are extended).
    """
    if now is None:
        reference = datetime.now()
    else:
        reference = _wall_clock(now)
        if reference is None:
            raise ValueError("now must be an ISO timestamp or datetime")
    last_ok = conn.execute(
        "SELECT value FROM meta WHERE key='last_run_ok_ended'"
    ).fetchone()
    canary_raw = _latest_canary_entry(
        CANARY_HISTORY_PATH if canary_history_path is None else canary_history_path)
    # MAX() is an aggregate: fetchone() always returns exactly one row (NULL inside when
    # the table is empty), unlike the keyed meta lookup above.
    judge_raw = conn.execute("SELECT MAX(collected_at) FROM second_opinions").fetchone()[0]
    return {
        "checked_at": reference.isoformat(timespec="seconds"),
        "readings": [
            _silence_reading("pipeline_run", last_ok[0] if last_ok else None,
                             reference, RUN_SILENCE_HOURS),
            _silence_reading("canary", canary_raw, reference, CANARY_SILENCE_HOURS),
            _silence_reading("second_judge", judge_raw,
                             reference, SECOND_JUDGE_SILENCE_HOURS),
        ],
    }


def health_snapshot(conn, cfg, *, today=None, days=30, run_limit=20):
    """Return bounded run facts and chain-deduped first-storage attribution metrics."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_HEALTH_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_HEALTH_DAYS}")
    if (isinstance(run_limit, bool) or not isinstance(run_limit, int)
            or not 1 <= run_limit <= MAX_RUN_HISTORY):
        raise ValueError(f"run_limit must be an integer from 1 to {MAX_RUN_HISTORY}")
    today = today or date.today()
    if not isinstance(today, date):
        raise ValueError("today must be a date")
    cutoff = today - timedelta(days=days - 1)
    # Staleness runs BEFORE the read transaction opens: it touches the canary history
    # FILE, and on this machine's drive a wedged file read can stall for minutes — that
    # stall must not hold the WAL read snapshot open under it. Its readings make no
    # snapshot-consistency claim anyway (the sentinel is descriptive, not transactional).
    staleness = staleness_readings(conn)
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        last_ok = conn.execute(
            "SELECT value FROM meta WHERE key='last_run_ok_ended'"
        ).fetchone()
        result = {
            "range": {"days": days, "start": cutoff.isoformat(), "end": today.isoformat()},
            "last_successful_run_ended": last_ok[0] if last_ok else None,
            "staleness": staleness,
            "runs": _run_history(conn, run_limit),
            "search_effectiveness": _search_effectiveness(conn, cfg, cutoff.isoformat()),
            "definitions": {
                "health": "Source status records configured units that completed, failed, or were intentionally skipped; zero new postings alone is not a failure. A run without ended_at means completion was not recorded (it may still be active or may have been interrupted externally).",
                "staleness": "Staleness compares now against each scheduled producer's last durable stamp: the successful-cycle cooldown stamp, the newest canary history entry, and the newest second-opinion collection. Readings are descriptive sentinel facts with schedule-sized thresholds; nothing is retried, registered, or repaired automatically.",
                "attribution": "Posting counts use each stored row's own source/search. Role, strong, and applied counts use one current chain assigned to its earliest stored posting; only chains first seen inside the cohort qualify. Merge/unlink can change those current-chain counts, so the view is descriptive rather than causal.",
                "privacy": "Run records retain counts and exception class names, never exception messages, URLs, credentials, or posting text.",
            },
        }
        if owns_snapshot:
            conn.commit()
        return result
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise
