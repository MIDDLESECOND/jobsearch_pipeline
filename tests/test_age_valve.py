"""Corpus mode (CHANGELOG 2026-09-09): `settings.evaluate: false` + the age valve
(filters.apply_age_valve, status 'aged_out').

The valve's contract has three edges worth pinning with different outcomes on each side:
the N-day boundary itself, "only 'new' rows", and its PLACEMENT — after the forward skip
passes, so a stale relisting of an applied role still becomes repost_decided (the re-apply
guard) instead of being parked by age first. The evaluate switch is pinned both ways too:
false skips the paid stage, absent/true still runs it.
"""

import contextlib
import sys
from datetime import datetime

import filters
import pipeline
import report
from conftest import job_status, make_job
from states import STATUS_AGED_OUT, STATUS_NEW, STATUS_REPOST_DECIDED

NOW = datetime(2026, 9, 20, 12, 0, 0)
CFG14 = {"settings": {"eval_max_age_days": 14}}


def _new(conn, url, first_seen, **kw):
    return make_job(conn, job_url=url, status="new", verdict=None, fit_score=None,
                    bucket=None, first_seen=first_seen, **kw)


def test_no_setting_means_no_valve(conn):
    _new(conn, "old", "2026-01-01T00:00:00")
    assert filters.apply_age_valve({"settings": {}}, conn, now=NOW) == 0
    assert filters.apply_age_valve({"settings": {"eval_max_age_days": None}}, conn, now=NOW) == 0
    assert job_status(conn, "old") == STATUS_NEW


def test_boundary_is_strictly_older_than_n_days(conn):
    # cutoff = NOW - 14d = 2026-09-06T12:00:00. One second older ages out; exactly the
    # cutoff and one second younger stay 'new' — a >=/> mutation flips the middle row.
    _new(conn, "older", "2026-09-06T11:59:59")
    _new(conn, "exact", "2026-09-06T12:00:00")
    _new(conn, "younger", "2026-09-06T12:00:01")
    assert filters.apply_age_valve(CFG14, conn, now=NOW) == 1
    assert job_status(conn, "older") == STATUS_AGED_OUT
    assert job_status(conn, "exact") == STATUS_NEW
    assert job_status(conn, "younger") == STATUS_NEW


def test_only_new_rows_are_touched(conn):
    parked = (("ev", "evaluated"), ("sal", "salary_filtered"), ("rd", "repost_decided"))
    for url, st in parked:
        make_job(conn, job_url=url, status=st, first_seen="2026-01-01T00:00:00")
    assert filters.apply_age_valve(CFG14, conn, now=NOW) == 0
    for url, st in parked:
        assert job_status(conn, url) == st


def _wire_run(monkeypatch, conn, settings, calls):
    """The real `run` block with every fetcher stubbed (nothing inserted), the paid eval
    and the report recorded by name, and everything else running for REAL unless a test
    overrides it."""
    monkeypatch.setattr(pipeline, "load_config", lambda: {"settings": settings, "searches": []})
    monkeypatch.setattr(pipeline, "get_db", lambda cfg: conn)
    monkeypatch.setattr(pipeline, "run_log", lambda label="run": contextlib.nullcontext())
    for name in ("fetch_new_jobs", "fetch_adzuna", "fetch_ats", "fetch_dice"):
        monkeypatch.setattr(pipeline, name, lambda cfg, c: None)
    monkeypatch.setattr(pipeline, "evaluate_new_jobs", lambda c, cn: calls.append("eval"))
    monkeypatch.setattr(pipeline, "generate_report", lambda c, cn, d, **k: calls.append("report"))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "run"])


def test_valve_runs_after_the_forward_skips_and_before_eval(conn, monkeypatch):
    calls = []
    _wire_run(monkeypatch, conn, {}, calls)

    def _skip_stub(name):
        def stub(cn, forward=True, restore=True):
            calls.append(name + {(True, True): "", (True, False): ":fwd",
                                 (False, True): ":restore"}[(forward, restore)])
        return stub
    monkeypatch.setattr(pipeline, "skip_decided_reposts", _skip_stub("skip"))
    monkeypatch.setattr(pipeline, "skip_evaluated_reposts", _skip_stub("skip_eval"))
    monkeypatch.setattr(pipeline, "apply_salary_filter", lambda c, cn: calls.append("salary"))
    monkeypatch.setattr(pipeline, "apply_hard_filters", lambda c, cn: calls.append("hard"))
    monkeypatch.setattr(pipeline, "apply_age_valve", lambda c, cn: calls.append("valve"))
    pipeline.main()
    assert calls == ["skip:restore", "skip_eval:restore", "salary", "hard",
                     "skip:fwd", "skip_eval:fwd", "valve", "eval", "report"]


def test_stale_relisting_of_an_applied_role_becomes_repost_decided_not_aged_out(conn, monkeypatch):
    # Placement made behavioral: both 'new' rows are far older than the 14-day window. The
    # relisting of an applied chain must carry the re-apply guard's status; the standalone
    # row ages out. Move the valve ahead of the skips and the relisting reads 'aged_out'.
    make_job(conn, job_url="canon", status="evaluated", app_status="applied",
             status_date="2026-07-01", first_seen="2026-06-01T00:00:00")
    _new(conn, "relist", "2026-06-20T00:00:00", repost_of="canon", repost_source="fingerprint")
    _new(conn, "lone", "2026-06-20T00:00:00", company="Other Co")
    calls = []
    _wire_run(monkeypatch, conn, {"evaluate": False, "eval_max_age_days": 14}, calls)
    pipeline.main()
    assert job_status(conn, "relist") == STATUS_REPOST_DECIDED
    assert job_status(conn, "lone") == STATUS_AGED_OUT
    assert "eval" not in calls and "report" in calls


def test_evaluate_false_skips_the_paid_stage_and_true_or_absent_runs_it(conn, monkeypatch, capsys):
    _new(conn, "fresh", datetime.now().isoformat(timespec="seconds"))
    calls = []
    _wire_run(monkeypatch, conn, {"evaluate": False}, calls)
    pipeline.main()
    assert calls == ["report"]
    assert "[eval] disabled (settings.evaluate: false)" in capsys.readouterr().out
    assert job_status(conn, "fresh") == STATUS_NEW
    for settings in ({}, {"evaluate": True}):
        calls.clear()
        _wire_run(monkeypatch, conn, settings, calls)
        pipeline.main()
        assert calls == ["eval", "report"], settings


def test_report_counts_aged_out_rows_by_name(conn, tmp_path):
    day = "2026-08-01"
    make_job(conn, job_url="a", status="aged_out", verdict=None, fit_score=None, bucket=None,
             first_seen=f"{day}T09:00:00")
    _new(conn, "b", f"{day}T10:00:00")
    out = tmp_path / "reports"
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=day)
    text = (out / f"report_{day}.md").read_text(encoding="utf-8")
    assert "**2 new postings**" in text
    assert "1 awaiting evaluation" in text
    assert "1 aged out (never evaluated)" in text
