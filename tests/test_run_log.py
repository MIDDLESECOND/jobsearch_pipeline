"""run_log — the per-invocation stdout/stderr tee into logs/pipeline-YYYY-MM-DD.log,
bracketed by the scheduler-style ===== session markers, plus the best-effort 30-day
retention sweep that replaced size rotation. run_log reads core.LOGS_DIR at CALL time,
so the tests monkeypatch it to tmp_path — no production knob needed, and the repo's real
logs/ is never touched. (core's import-time stdout reconfigure is out of scope here.)"""

import sys
from datetime import date
from pathlib import Path

import pytest

import core


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setattr(core, "LOGS_DIR", d)
    return d


def _only_day_file(logs_dir):
    """The single dated log a fresh directory holds after one run_log invocation.
    Located by glob, not by recomputing today's name — immune to a midnight cross
    between the invocation and the assertion."""
    files = list(logs_dir.glob("pipeline-????-??-??.log"))
    assert len(files) == 1, files
    return files[0]


def _dated_name(days_ago):
    # Same arithmetic run_log's cutoff uses, so the planted names hit the exact boundary.
    return f"pipeline-{date.fromordinal(date.today().toordinal() - days_ago):%Y-%m-%d}.log"


def test_prints_are_teed_into_the_dated_file_with_session_markers(logs_dir):
    saved_out, saved_err = sys.stdout, sys.stderr
    with core.run_log("unittest"):
        print("stdout line reaches the log")
        print("stderr line reaches the log too", file=sys.stderr)
    assert sys.stdout is saved_out and sys.stderr is saved_err  # streams restored
    text = _only_day_file(logs_dir).read_text(encoding="utf-8")
    assert "stdout line reaches the log" in text
    assert "stderr line reaches the log too" in text
    assert "===== unittest started " in text
    assert "===== unittest ended " in text
    assert "(ok)" in text


def test_uncaught_exception_lands_in_the_log_and_stamps_the_end_marker(logs_dir):
    # The interpreter prints the traceback only AFTER the context exits — to the restored
    # stderr, which a scheduled run discards — so the log itself must capture it, and the
    # end marker must name the exception class instead of 'ok'. The exception propagates.
    saved_out, saved_err = sys.stdout, sys.stderr
    with pytest.raises(ValueError, match="boom"):
        with core.run_log("crash"):
            raise ValueError("boom")
    assert sys.stdout is saved_out and sys.stderr is saved_err
    text = _only_day_file(logs_dir).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "ValueError: boom" in text
    assert "===== crash ended " in text
    assert "(ValueError)" in text


def test_retention_sweeps_only_files_beyond_the_keep_window(logs_dir):
    logs_dir.mkdir()
    too_old = logs_dir / _dated_name(31)   # strictly older than the 30-day cutoff name
    boundary = logs_dir / _dated_name(30)  # equals the cutoff name — kept (< is strict)
    fresh = logs_dir / _dated_name(29)
    legacy = logs_dir / "pipeline.log"     # pre-retention fixed name: never matches the glob
    for f in (too_old, boundary, fresh, legacy):
        f.write_text("x", encoding="utf-8")
    with core.run_log("sweep"):
        pass
    assert not too_old.exists()
    assert boundary.exists()
    assert fresh.exists()
    assert legacy.exists()


def test_sweep_skips_an_undeletable_file_and_still_runs(logs_dir, monkeypatch):
    # Best-effort contract: a locked old log (editor, AV scan) costs only itself — the
    # try sits INSIDE the loop, so the sweep continues past it and the run still logs.
    # The failure is pinned to the FIRST unlink call rather than to a chosen filename:
    # glob order isn't guaranteed, and a test that failed on whichever file happens to
    # be swept last would pass without ever proving the loop continued.
    logs_dir.mkdir()
    old = [logs_dir / "pipeline-2020-01-01.log", logs_dir / "pipeline-2020-01-02.log"]
    for f in old:
        f.write_text("x", encoding="utf-8")
    calls = []
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        calls.append(self.name)
        if len(calls) == 1:
            raise PermissionError(13, "file in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with core.run_log("locked"):
        print("run went ahead")
    assert len(calls) == 2                          # the sweep reached both old logs
    assert [f.exists() for f in old].count(True) == 1  # the stuck one survives, alone
    survivor = next(f for f in old if f.exists())
    day_files = [f for f in logs_dir.glob("pipeline-????-??-??.log") if f != survivor]
    assert len(day_files) == 1, day_files
    assert "run went ahead" in day_files[0].read_text(encoding="utf-8")


def test_unopenable_sink_degrades_to_no_file_capture(logs_dir, capsys):
    # "Logging must never block the actual work": when the day's log can't be opened
    # (the documented case is an overlapping run holding it on Windows), run_log warns
    # and yields WITHOUT capture instead of aborting the pipeline. Made unopenable by
    # planting a DIRECTORY at the log path — open(dir, "a") raises an OSError subclass
    # on both Windows (PermissionError) and POSIX (IsADirectoryError), so no builtin
    # needs patching.
    logs_dir.mkdir()
    (logs_dir / f"pipeline-{date.today():%Y-%m-%d}.log").mkdir()
    saved_out, saved_err = sys.stdout, sys.stderr
    ran = False
    with core.run_log("blocked"):
        ran = True
        assert sys.stdout is saved_out   # no tee installed on the degraded path
    assert ran
    assert sys.stdout is saved_out and sys.stderr is saved_err
    assert "could not open" in capsys.readouterr().err
