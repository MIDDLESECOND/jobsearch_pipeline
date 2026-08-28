"""Scheduled DeepSeek evals sit out the 2x peak-rate windows (evaluation.in_deepseek_peak
+ pipeline._defer_eval_for_peak), effective on DeepSeek's side 2026-08-17; since
2026-08-23 (Beijing) the windows bill 2x on Beijing-calendar weekdays only.

Fail directions differ by half: a wrong False costs one eval batch at 2x price (annoying);
a wrong True merely delays rows to the next off-peak slot — EXCEPT when it fires for a
non-DeepSeek provider or a manual run, where "defer" has no later slot with different
economics and would silently stop evaluation. Those two must stay False, and the wiring
tests pin that the deferred cycle still reports and still stamps the cooldown.

Since 2026-08-27 the gate also looks AHEAD (evaluation.peak_overlap_minutes over the
pending row count): a batch predicted to drag INTO a window defers before its first paid
call — the shape that billed a 938-row batch's ~100-minute tail at 2x when Task Scheduler
replayed a missed 23:00 slot at 00:17. Same fail directions, same manual/provider outs."""

import contextlib
import sys
from datetime import date, datetime, timedelta, timezone

from conftest import make_job
from core import meta_get
import pipeline
import report
from evaluation import deepseek_peak_end, in_deepseek_peak, peak_overlap_minutes
from pipeline import (EVAL_ROWS_PER_MIN, PEAK_CROSS_TOLERANCE_MIN,
                      _defer_eval_for_peak, _peak_cross_note, _peak_price_note)


def _utc(h, m=0):
    # 2026-08-20 is a Thursday — a Beijing weekday, so the windows are live.
    return datetime(2026, 8, 20, h, m, tzinfo=timezone.utc)


# --------------------------------------------------------------- the predicate

def test_peak_windows_half_open():
    # Windows are [1,4) and [6,10) UTC — each edge checked from both sides.
    assert in_deepseek_peak(_utc(0, 59)) is False
    assert in_deepseek_peak(_utc(1)) is True
    assert in_deepseek_peak(_utc(3, 59)) is True
    assert in_deepseek_peak(_utc(4)) is False
    assert in_deepseek_peak(_utc(5, 30)) is False
    assert in_deepseek_peak(_utc(6)) is True
    assert in_deepseek_peak(_utc(9, 59)) is True
    assert in_deepseek_peak(_utc(10)) is False
    assert in_deepseek_peak(_utc(22)) is False


def test_non_utc_aware_input_is_normalized():
    # The whole point of a UTC predicate is DST immunity, so a non-UTC caller must be
    # converted, not read by its wall-clock hour. 20:30 CDT = 01:30 UTC — peak;
    # 23:30 CDT = 04:30 UTC — off-peak (the current 23:00 slot's territory).
    cdt = timezone(timedelta(hours=-5))
    assert in_deepseek_peak(datetime(2026, 8, 20, 20, 30, tzinfo=cdt)) is True
    assert in_deepseek_peak(datetime(2026, 8, 20, 23, 30, tzinfo=cdt)) is False


def test_peak_end_names_the_window_close():
    assert deepseek_peak_end(_utc(2, 30)) == _utc(4)
    assert deepseek_peak_end(_utc(6)) == _utc(10)
    assert deepseek_peak_end(_utc(12)) is None
    assert deepseek_peak_end(_utc(0, 30)) is None


def _on(day, h, m=0):
    return datetime(2026, 8, day, h, m, tzinfo=timezone.utc)


def test_weekends_are_off_peak_on_the_beijing_calendar():
    # Same hour inside the afternoon window (09:00 UTC = 17:00 Beijing) across the first
    # weekend boundary AFTER the rule took effect, both sides pinned: Friday peak,
    # Saturday and Sunday off-peak, Monday peak again.
    assert in_deepseek_peak(_on(28, 9)) is True      # Fri 2026-08-28
    assert in_deepseek_peak(_on(29, 9)) is False     # Sat
    assert in_deepseek_peak(_on(30, 9)) is False     # Sun
    assert in_deepseek_peak(_on(31, 9)) is True      # Mon
    # The morning window too, and through the ONE reading: no window means no end.
    assert in_deepseek_peak(_on(29, 1)) is False
    assert deepseek_peak_end(_on(30, 2, 30)) is None
    assert deepseek_peak_end(_on(31, 2, 30)) == _on(31, 4)


def test_weekend_is_the_beijing_weekend_not_the_local_one():
    # The US evening slot lands at 01:00 UTC = 09:00 Beijing the NEXT calendar day: a
    # local Friday/Saturday evening is a Beijing Saturday/Sunday morning (off-peak),
    # while a local Thursday evening is Beijing Friday and a local Sunday evening is
    # already Beijing Monday (both peak). Local weekday is not the decision.
    cdt = timezone(timedelta(hours=-5))
    assert in_deepseek_peak(datetime(2026, 8, 27, 20, 30, tzinfo=cdt)) is True    # Thu
    assert in_deepseek_peak(datetime(2026, 8, 28, 20, 30, tzinfo=cdt)) is False   # Fri
    assert in_deepseek_peak(datetime(2026, 8, 29, 20, 30, tzinfo=cdt)) is False   # Sat
    assert in_deepseek_peak(datetime(2026, 8, 30, 20, 30, tzinfo=cdt)) is True    # Sun


# ----------------------------------------------------- the look-ahead (2026-08-27)
# peak_overlap_minutes: how much of [start, start+minutes) lands inside a window.

def test_overlap_zero_when_span_stays_clear():
    assert peak_overlap_minutes(_utc(4, 10), 60) == 0.0        # the [4,6) gap


def test_overlap_counts_only_the_in_window_tail():
    # 05:00 + 120 min ends 07:00 — the 00:17-slot shape: off-peak start, 2x tail.
    assert peak_overlap_minutes(_utc(5), 120) == 60.0


def test_overlap_full_span_inside_a_window():
    assert peak_overlap_minutes(_utc(6, 30), 60) == 60.0


def test_overlap_stops_at_the_window_close():
    assert peak_overlap_minutes(_utc(9, 30), 120) == 30.0


def test_overlap_sums_both_windows():
    # 00:30 + 10h spans [1,4) whole (180) and [6,10) whole (240).
    assert peak_overlap_minutes(_utc(0, 30), 600) == 420.0


def test_overlap_crosses_midnight_into_the_next_days_window():
    # Thu 23:30 + 120 min ends Fri 01:30 — Friday's [1,4) is live.
    assert peak_overlap_minutes(_utc(23, 30), 120) == 30.0


def test_overlap_window_weekday_is_the_windows_own_day_not_the_spans():
    # Fri 23:30 → Sat 01:30: the span starts on a weekday but the window it would
    # hit belongs to Beijing Saturday — off-peak, nothing to cross into.
    assert peak_overlap_minutes(_on(28, 23, 30), 120) == 0.0
    # Sun 23:30 → Mon 01:30: span starts on the weekend, the window is Monday's.
    assert peak_overlap_minutes(_on(30, 23, 30), 120) == 30.0


def test_overlap_weekend_window_contributes_nothing():
    assert peak_overlap_minutes(_on(29, 5), 300) == 0.0        # Sat 05:00–10:00


# --------------------------------------------------------------------- the gate

PEAK = _utc(2)
OFF = _utc(12)
WEEKEND_PEAK_HOUR = _on(29, 2)          # Saturday, inside [1,4) — off-peak since 08-23


def _cfg(provider="deepseek"):
    return {"settings": {"provider": provider}}


def test_gate_scheduled_deepseek_peak_defers():
    assert _defer_eval_for_peak(True, _cfg(), PEAK) is True


def test_gate_off_peak_runs():
    assert _defer_eval_for_peak(True, _cfg(), OFF) is False


def test_gate_weekend_window_hour_runs():
    # A scheduled slot inside the window on a Beijing weekend must NOT defer: under the
    # weekend rule there is no cheaper later slot to wait for, only a delayed eval.
    assert _defer_eval_for_peak(True, _cfg(), WEEKEND_PEAK_HOUR) is False


def test_gate_manual_run_always_evaluates():
    assert _defer_eval_for_peak(False, _cfg(), PEAK) is False


def test_gate_other_provider_unaffected():
    assert _defer_eval_for_peak(True, _cfg("anthropic"), PEAK) is False


def test_gate_missing_provider_defaults_anthropic():
    # evaluate_new_jobs defaults a missing provider to "anthropic"; the gate must read
    # the SAME default, or it would defer evals that the eval stage bills on Anthropic.
    assert _defer_eval_for_peak(True, {"settings": {}}, PEAK) is False


# ------------------------------------------------- the gate's look-ahead leg

CLEAR_5AM = _utc(5)                     # off-peak, one hour before the [6,10) window
BIG = 900                               # 150 min at the 6.0 floor — deep crossing
SMALL = 90                              # 15 min — finishes before the window opens


def test_gate_scheduled_crossing_batch_defers():
    assert _defer_eval_for_peak(True, _cfg(), CLEAR_5AM, pending=BIG) is True


def test_gate_batch_that_finishes_before_the_window_runs():
    assert _defer_eval_for_peak(True, _cfg(), CLEAR_5AM, pending=SMALL) is False


def test_gate_crossing_tolerance_boundary():
    # 180 rows = 30 min. Starting 05:45 the span overlaps [6,10) by exactly the
    # 15-min tolerance (run); one minute later it overlaps 16 (defer). The two
    # sides of the boundary must land on DIFFERENT outcomes.
    assert PEAK_CROSS_TOLERANCE_MIN == 15          # the literal the pair encodes
    assert _defer_eval_for_peak(True, _cfg(), _utc(5, 45), pending=180) is False
    assert _defer_eval_for_peak(True, _cfg(), _utc(5, 46), pending=180) is True


def test_gate_crossing_manual_never_defers():
    assert _defer_eval_for_peak(False, _cfg(), CLEAR_5AM, pending=BIG) is False


def test_gate_crossing_other_provider_unaffected():
    assert _defer_eval_for_peak(True, _cfg("anthropic"), CLEAR_5AM, pending=BIG) is False


def test_gate_crossing_into_a_weekend_window_runs():
    # Sat 05:00 + 150 min would sit inside [6,10) — but Saturday bills off-peak,
    # so there is nothing to dodge and deferral would only delay the rows.
    assert _defer_eval_for_peak(True, _cfg(), _on(29, 5), pending=BIG) is False


def test_gate_no_pending_keeps_the_start_instant_meaning():
    # pending defaults to 0: callers without a row count get exactly the old gate.
    assert _defer_eval_for_peak(True, _cfg(), CLEAR_5AM) is False
    assert _defer_eval_for_peak(True, _cfg(), PEAK) is True


# ------------------------------------------------------------- the manual note

def test_note_in_peak_names_price_and_exit():
    note = _peak_price_note(_cfg(), _utc(2, 30))
    assert note is not None
    assert "2x" in note and "~90 more min" in note
    # The rerun time is spelled in the machine's LOCAL clock — that's the clock the
    # human will look at — so assert via the same conversion, not a hardcoded hour.
    assert _utc(4).astimezone().strftime("%H:%M") in note


def test_note_off_peak_is_silent():
    assert _peak_price_note(_cfg(), OFF) is None


def test_note_weekend_window_hour_is_silent():
    # The note reads deepseek_peak_end directly, so the weekend exemption must reach it:
    # a 2x warning on a day billed at 1x would send the human away for nothing.
    assert _peak_price_note(_cfg(), WEEKEND_PEAK_HOUR) is None


def test_note_other_provider_is_silent():
    assert _peak_price_note(_cfg("anthropic"), PEAK) is None
    assert _peak_price_note({"settings": {}}, PEAK) is None


def test_cross_note_manual_crossing_names_the_tail():
    note = _peak_cross_note(_cfg(), BIG, CLEAR_5AM)
    assert note is not None and "[price]" in note
    assert "2x" in note and "cross" in note
    assert f"~{round(BIG / EVAL_ROWS_PER_MIN)} min" in note


def test_cross_note_silent_when_the_span_stays_clear():
    assert _peak_cross_note(_cfg(), SMALL, CLEAR_5AM) is None


def test_cross_note_silent_inside_a_window():
    # In-window is _peak_price_note's job; two [price] lines for one eval would
    # read as two problems.
    assert _peak_cross_note(_cfg(), BIG, PEAK) is None


def test_cross_note_silent_for_other_provider_and_empty_batch():
    assert _peak_cross_note(_cfg("anthropic"), BIG, CLEAR_5AM) is None
    assert _peak_cross_note(_cfg(), 0, CLEAR_5AM) is None


# ------------------------------------------------------------------ the wiring
# Same harness as test_cooldown.py: drive the real main() run branch with every stage
# stubbed; the peak clock is patched, not mocked time.

def _drive_run(conn, monkeypatch, argv, peak, cross=None):
    calls = []

    def fetcher(label):
        def fn(cfg, c):
            calls.append(label)
            return 0
        return fn

    monkeypatch.setattr(pipeline, "load_config",
                        lambda: {"settings": {"provider": "deepseek"}, "searches": []})
    monkeypatch.setattr(pipeline, "get_db", lambda cfg: conn)
    monkeypatch.setattr(pipeline, "run_log", lambda label="run": contextlib.nullcontext())
    monkeypatch.setattr(pipeline, "fetch_new_jobs", fetcher("linkedin"))
    monkeypatch.setattr(pipeline, "fetch_adzuna", fetcher("adzuna"))
    monkeypatch.setattr(pipeline, "fetch_ats", fetcher("ats"))
    monkeypatch.setattr(pipeline, "fetch_dice", fetcher("dice"))
    for name in ("apply_salary_filter", "apply_hard_filters", "evaluate_new_jobs"):
        monkeypatch.setattr(pipeline, name, lambda c, cn, _n=name: calls.append(_n))
    for name in ("skip_decided_reposts", "skip_evaluated_reposts"):
        monkeypatch.setattr(pipeline, name,
                            lambda cn, forward=True, restore=True, _n=name: calls.append(_n))
    # Record the DAY: this run stage no longer rebuilds only run_date (see the report_days
    # set in main) — which day it rebuilds is now the load-bearing part.
    monkeypatch.setattr(pipeline, "generate_report",
                        lambda c, cn, d, **k: calls.append(f"report:{d}"))
    monkeypatch.setattr(pipeline, "in_deepseek_peak", lambda now=None: peak)
    # The note path reads the clock through deepseek_peak_end; keep both patched
    # clocks telling the same story, with a plausible remaining-window span.
    monkeypatch.setattr(
        pipeline, "deepseek_peak_end",
        lambda now=None: (datetime.now(timezone.utc) + timedelta(minutes=30))
        if peak else None)
    if cross is not None:
        # The look-ahead leg, pinned to a constant overlap — the row count still
        # comes from the real pending query, so a cross value only bites when the
        # test actually inserted 'new' rows.
        monkeypatch.setattr(pipeline, "peak_overlap_minutes",
                            lambda start, minutes: cross)
    monkeypatch.setattr(sys, "argv", ["pipeline.py"] + argv)
    pipeline.main()
    return calls


TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def test_wiring_scheduled_peak_defers_eval_but_cycle_completes(conn, monkeypatch, capsys):
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=True)
    out = capsys.readouterr().out
    assert "evaluate_new_jobs" not in calls
    assert f"report:{TODAY}" in calls                     # deferral skips ONLY the paid stage
    assert "[eval] deferred" in out                      # visible in the day's log
    assert "[price]" not in out                          # the human warning is manual-only
    assert meta_get(conn, "last_run_ok_ended")           # still a full cycle: stamp advances


def test_wiring_manual_run_warns_and_evaluates_during_peak(conn, monkeypatch, capsys):
    calls = _drive_run(conn, monkeypatch, ["run"], peak=True)
    out = capsys.readouterr().out
    assert "evaluate_new_jobs" in calls                  # warned, never blocked
    assert out.count("[price]") == 2                     # at run start AND at eval start


def test_wiring_scheduled_off_peak_evaluates(conn, monkeypatch):
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=False)
    assert "evaluate_new_jobs" in calls


def test_wiring_manual_off_peak_is_quiet(conn, monkeypatch, capsys):
    calls = _drive_run(conn, monkeypatch, ["run"], peak=False)
    assert "evaluate_new_jobs" in calls
    assert "[price]" not in capsys.readouterr().out


# ------------------------------------------------- what a waiting row does to reports
#
# Deferral means a row can be fetched on one calendar day and evaluated by a run whose
# run_date is the next one. generate_report rebuilds ONE day, so the report stage has to
# follow the ROWS (their first_seen), not the run's start date — otherwise the older day
# keeps its pre-eval snapshot forever and those rows are in no report at all.


def _waiting_row(conn, day):
    make_job(conn, first_seen=f"{day}T22:00:00", status="new",
             verdict=None, fit_score=None, bucket=None)


def test_wiring_rebuilds_the_report_of_a_day_whose_rows_waited(conn, monkeypatch):
    _waiting_row(conn, YESTERDAY)
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=False)
    assert f"report:{TODAY}" in calls          # the run's own day, as always
    assert f"report:{YESTERDAY}" in calls      # and the day the waiting row belongs to


def test_wiring_deferred_run_rebuilds_only_its_own_day(conn, monkeypatch):
    # Nothing was evaluated, so no OTHER day's report can have changed — collecting days
    # here would rewrite old reports on every peak slot for no reason.
    _waiting_row(conn, YESTERDAY)
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=True)
    assert [c for c in calls if c.startswith("report:")] == [f"report:{TODAY}"]


def test_report_names_the_rows_it_has_not_evaluated(conn, tmp_path):
    # The buckets can't hold a 'new' row, so without this line the headline count exceeds
    # their sum with nothing saying why — twice a day, by design, since the deferral.
    day = "2026-06-01"
    _waiting_row(conn, day)
    make_job(conn, first_seen=f"{day}T09:00:00")          # an evaluated PASS alongside it
    out = tmp_path / "reports"                            # absolute: never the real repo dir
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=day)
    text = (out / f"report_{day}.md").read_text(encoding="utf-8")
    assert "2 new postings" in text
    assert "1 awaiting evaluation" in text


def test_report_is_silent_when_everything_was_evaluated(conn, tmp_path):
    day = "2026-06-01"
    make_job(conn, first_seen=f"{day}T09:00:00")
    out = tmp_path / "reports"
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=day)
    assert "awaiting evaluation" not in (out / f"report_{day}.md").read_text(encoding="utf-8")


# --------------------------------------------------- the deferral as a durable run fact


def test_deferred_run_is_marked_on_its_health_record(conn, monkeypatch):
    _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=True)
    row = conn.execute("SELECT status, eval_deferred FROM pipeline_runs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    # The cycle really did complete — the flag is a separate fact from fetch health.
    assert row["status"] == "succeeded" and row["eval_deferred"] == 1


def test_evaluated_run_is_not_marked_deferred(conn, monkeypatch):
    _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=False)
    row = conn.execute("SELECT eval_deferred FROM pipeline_runs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["eval_deferred"] == 0


# ------------------------------------------------- the look-ahead through the wiring


def test_wiring_scheduled_offpeak_crossing_defers_before_the_first_paid_call(
        conn, monkeypatch, capsys):
    # The 00:17 shape end to end: off-peak clock, rows waiting, predicted overlap
    # far past tolerance — the cycle completes, reports its own day, marks the run
    # deferred, and the log line names the CROSSING mode (not the in-window one).
    _waiting_row(conn, YESTERDAY)
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=False,
                       cross=120.0)
    out = capsys.readouterr().out
    assert "evaluate_new_jobs" not in calls
    assert "[eval] deferred" in out and "cross" in out
    assert "[price]" not in out                      # the human warning is manual-only
    assert [c for c in calls if c.startswith("report:")] == [f"report:{TODAY}"]
    row = conn.execute("SELECT status, eval_deferred FROM pipeline_runs "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "succeeded" and row["eval_deferred"] == 1


def test_wiring_manual_offpeak_crossing_warns_once_and_evaluates(
        conn, monkeypatch, capsys):
    _waiting_row(conn, YESTERDAY)
    calls = _drive_run(conn, monkeypatch, ["run"], peak=False, cross=120.0)
    out = capsys.readouterr().out
    assert "evaluate_new_jobs" in calls              # warned, never blocked
    assert out.count("[price]") == 1 and "cross" in out
