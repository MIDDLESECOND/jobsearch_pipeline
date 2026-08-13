"""Scheduled DeepSeek evals sit out the 2x peak-rate windows (evaluation.in_deepseek_peak
+ pipeline._defer_eval_for_peak), effective on DeepSeek's side 2026-08-17.

Fail directions differ by half: a wrong False costs one eval batch at 2x price (annoying);
a wrong True merely delays rows to the next off-peak slot — EXCEPT when it fires for a
non-DeepSeek provider or a manual run, where "defer" has no later slot with different
economics and would silently stop evaluation. Those two must stay False, and the wiring
tests pin that the deferred cycle still reports and still stamps the cooldown."""

import contextlib
import sys
from datetime import datetime, timedelta, timezone

from core import meta_get
import pipeline
from evaluation import deepseek_peak_end, in_deepseek_peak
from pipeline import _defer_eval_for_peak, _peak_price_note


def _utc(h, m=0):
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


# --------------------------------------------------------------------- the gate

PEAK = _utc(2)
OFF = _utc(12)


def _cfg(provider="deepseek"):
    return {"settings": {"provider": provider}}


def test_gate_scheduled_deepseek_peak_defers():
    assert _defer_eval_for_peak(True, _cfg(), PEAK) is True


def test_gate_off_peak_runs():
    assert _defer_eval_for_peak(True, _cfg(), OFF) is False


def test_gate_manual_run_always_evaluates():
    assert _defer_eval_for_peak(False, _cfg(), PEAK) is False


def test_gate_other_provider_unaffected():
    assert _defer_eval_for_peak(True, _cfg("anthropic"), PEAK) is False


def test_gate_missing_provider_defaults_anthropic():
    # evaluate_new_jobs defaults a missing provider to "anthropic"; the gate must read
    # the SAME default, or it would defer evals that the eval stage bills on Anthropic.
    assert _defer_eval_for_peak(True, {"settings": {}}, PEAK) is False


# ------------------------------------------------------------- the manual note

def test_note_in_peak_names_price_and_exit():
    note = _peak_price_note(_cfg(), _utc(2, 30))
    assert "2x" in note and "~90 more min" in note
    # The rerun time is spelled in the machine's LOCAL clock — that's the clock the
    # human will look at — so assert via the same conversion, not a hardcoded hour.
    assert _utc(4).astimezone().strftime("%H:%M") in note


def test_note_off_peak_is_silent():
    assert _peak_price_note(_cfg(), OFF) is None


def test_note_other_provider_is_silent():
    assert _peak_price_note(_cfg("anthropic"), PEAK) is None
    assert _peak_price_note({"settings": {}}, PEAK) is None


# ------------------------------------------------------------------ the wiring
# Same harness as test_cooldown.py: drive the real main() run branch with every stage
# stubbed; the peak clock is patched, not mocked time.

def _drive_run(conn, monkeypatch, argv, peak):
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
    monkeypatch.setattr(pipeline, "generate_report", lambda c, cn, d: calls.append("report"))
    monkeypatch.setattr(pipeline, "in_deepseek_peak", lambda now=None: peak)
    # The note path reads the clock through deepseek_peak_end; keep both patched
    # clocks telling the same story, with a plausible remaining-window span.
    monkeypatch.setattr(
        pipeline, "deepseek_peak_end",
        lambda now=None: (datetime.now(timezone.utc) + timedelta(minutes=30))
        if peak else None)
    monkeypatch.setattr(sys, "argv", ["pipeline.py"] + argv)
    pipeline.main()
    return calls


def test_wiring_scheduled_peak_defers_eval_but_cycle_completes(conn, monkeypatch, capsys):
    calls = _drive_run(conn, monkeypatch, ["run", "--scheduled"], peak=True)
    out = capsys.readouterr().out
    assert "evaluate_new_jobs" not in calls
    assert "report" in calls                             # deferral skips ONLY the paid stage
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
