"""The scheduled-run cooldown guard: `run --scheduled` no-ops when the last SUCCESSFUL
run ended < COOLDOWN_MINUTES ago (pipeline._cooldown_active over meta 'last_run_ok_ended').

The load-bearing property is fail-OPEN: missing, garbage, or future stamps must never
suppress a run — the cost of a wrong False is one redundant ~$0.19 run; the cost of a
wrong True is a pipeline that silently stops running (today's hardest-to-debug failure
shape). That includes the shapes that raise TypeError rather than ValueError (bytes
value; and aware stamps, which are normalized rather than rejected — they name a real
instant). The wiring tests below pin the other halves of the contract against main():
--scheduled skips inside the window and does NOT re-stamp; a bare run never skips; a
completed run stamps; a run whose EVERY fetcher crashed does not stamp (so an
all-sources-down catch-up run can't suppress the next slot).
"""

import contextlib
import sys
from datetime import datetime, timedelta, timezone

from core import meta_get, meta_set
from pipeline import _cooldown_active, COOLDOWN_MINUTES
import pipeline

NOW = datetime(2026, 7, 20, 20, 0, 0)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------- the predicate

def test_no_stamp_runs():
    assert _cooldown_active(None, NOW) is False


def test_recent_success_skips():
    assert _cooldown_active(_iso(NOW - timedelta(minutes=30)), NOW) is True


def test_old_success_runs():
    assert _cooldown_active(_iso(NOW - timedelta(minutes=90)), NOW) is False


def test_boundary_exactly_cooldown_runs():
    # The window is half-open [0, COOLDOWN_MINUTES): a run ending exactly the cooldown
    # ago is out of the window, so the slot runs.
    assert _cooldown_active(_iso(NOW - timedelta(minutes=COOLDOWN_MINUTES)), NOW) is False


def test_garbage_stamp_fails_open():
    assert _cooldown_active("not a timestamp", NOW) is False
    assert _cooldown_active("", NOW) is False


def test_bytes_stamp_fails_open():
    # A BLOB-typed meta row reaches fromisoformat as bytes → TypeError, not ValueError.
    # Must fail open, not crash the scheduled run.
    assert _cooldown_active(b"2026-07-20T19:30:00", NOW) is False


def test_aware_stamp_is_normalized_not_garbage():
    # An offset-suffixed stamp (hand-restored row, external tooling) parses to an AWARE
    # datetime; naive-minus-aware raises TypeError. It must neither crash NOR be treated
    # as garbage — it names a real instant, so the window math must still hold. Build the
    # aware stamps from local naive times so the assertion is timezone-independent.
    recent = (NOW - timedelta(minutes=30)).astimezone()   # local tz attached
    old = (NOW - timedelta(minutes=90)).astimezone(timezone.utc)
    assert _cooldown_active(recent.isoformat(), NOW) is True
    assert _cooldown_active(old.isoformat(), NOW) is False


def test_future_stamp_fails_open():
    # A stamp ahead of `now` (clock change, DST weirdness) must not suppress runs.
    assert _cooldown_active(_iso(NOW + timedelta(minutes=5)), NOW) is False


# --------------------------------------------------------- meta table plumbing

def test_meta_roundtrip_and_replace(conn):
    assert meta_get(conn, "last_run_ok_ended") is None
    meta_set(conn, "last_run_ok_ended", "2026-07-20T15:07:53")
    assert meta_get(conn, "last_run_ok_ended") == "2026-07-20T15:07:53"
    meta_set(conn, "last_run_ok_ended", "2026-07-20T20:31:02")  # INSERT OR REPLACE, one row
    assert meta_get(conn, "last_run_ok_ended") == "2026-07-20T20:31:02"
    assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == 1


# ------------------------------------------------------------------ the wiring
# Drive the real main() run branch (same harness as test_pipeline_run.py): every stage
# stubbed, fetchers return ints on success — the stamp condition distinguishes a
# fetcher's own 0 from _run_fetch_stage's None-on-crash, so stubs must not return None.

def _drive_run(conn, monkeypatch, argv, fetch_crashes=False):
    calls = []

    def fetcher(label):
        def fn(cfg, c):
            calls.append(label)
            if fetch_crashes:
                raise RuntimeError(f"{label} outage")
            return 0
        return fn

    monkeypatch.setattr(pipeline, "load_config", lambda: {"settings": {}, "searches": []})
    monkeypatch.setattr(pipeline, "get_db", lambda cfg: conn)
    monkeypatch.setattr(pipeline, "run_log", lambda label="run": contextlib.nullcontext())
    monkeypatch.setattr(pipeline, "fetch_new_jobs", fetcher("linkedin"))
    monkeypatch.setattr(pipeline, "fetch_adzuna", fetcher("adzuna"))
    monkeypatch.setattr(pipeline, "fetch_ats", fetcher("ats"))
    for name in ("apply_salary_filter", "apply_hard_filters", "evaluate_new_jobs"):
        monkeypatch.setattr(pipeline, name, lambda c, cn, _n=name: calls.append(_n))
    for name in ("skip_decided_reposts", "skip_evaluated_reposts"):
        monkeypatch.setattr(pipeline, name,
                            lambda cn, forward=True, restore=True, _n=name: calls.append(_n))
    monkeypatch.setattr(pipeline, "generate_report", lambda c, cn, d: calls.append("report"))
    monkeypatch.setattr(sys, "argv", ["pipeline.py"] + argv)
    pipeline.main()
    return calls


def test_scheduled_run_skips_and_does_not_restamp(conn, monkeypatch, capsys):
    fresh = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    meta_set(conn, "last_run_ok_ended", fresh)
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"])
    assert calls == []                                     # no stage ran, fetchers included
    assert "[cooldown]" in capsys.readouterr().out
    assert meta_get(conn, "last_run_ok_ended") == fresh    # a skip never re-stamps


def test_bare_run_ignores_fresh_stamp_and_restamps(conn, monkeypatch):
    fresh = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    meta_set(conn, "last_run_ok_ended", fresh)
    calls = _drive_run(conn, monkeypatch, ["run"])
    assert "report" in calls and calls[:3] == ["linkedin", "adzuna", "ats"]
    assert meta_get(conn, "last_run_ok_ended") != fresh    # completed run wrote a new stamp


def test_scheduled_run_past_cooldown_executes(conn, monkeypatch):
    stale = (datetime.now() - timedelta(minutes=COOLDOWN_MINUTES + 30)).isoformat(
        timespec="seconds")
    meta_set(conn, "last_run_ok_ended", stale)
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"])
    assert "report" in calls
    assert meta_get(conn, "last_run_ok_ended") != stale


def test_all_fetchers_crashed_does_not_stamp(conn, monkeypatch, capsys):
    # The wake-before-Wi-Fi catch-up: every source crashes (swallowed by
    # _run_fetch_stage), the cycle still completes — but it fetched nothing, so it must
    # NOT stamp, else the guard would suppress the first slot that CAN fetch.
    calls = _drive_run(conn, monkeypatch, ["run"], fetch_crashes=True)
    assert "report" in calls                               # the run itself still completes
    assert meta_get(conn, "last_run_ok_ended") is None
    assert "all fetch sources failed — not stamping" in capsys.readouterr().out
