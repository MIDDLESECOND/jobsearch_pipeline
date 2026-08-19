"""The daily report's one conditional health line.

The report is the one surface a human reads every day, and before this line it carried no
health information at all (the 2026-08-18 seam audit: a missing log day and a never-executed
canary schedule went unnoticed). The warning must appear when something is wrong and — just
as load-bearing — must NOT appear on a healthy day, so it keeps its signal value.

These tests use a past `for_date` on purpose: generate_report suppresses the live staleness
readings for past-date rebuilds (a now-fact stamped into a rebuilt old report would be an
anachronism), which both pins that suppression and keeps the tests off the real clock and the
real canary history. Staleness phrasing is covered through the pure _health_warning_line.
"""

import report
from conftest import make_job
from health import finish_pipeline_run, record_fetch_attempt, start_pipeline_run

DAY = "2026-08-07"


def _report(conn, tmp_path):
    # reports_dir is joined onto BASE_DIR; an absolute path wins that join, which keeps
    # the test out of the repo's real reports/ directory.
    out = tmp_path / "reports"
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=DAY)
    return (out / f"report_{DAY}.md").read_text(encoding="utf-8")


def _failed_target(conn, *, day=DAY, source="adzuna", label="ai_lead"):
    run_id = start_pipeline_run(conn, trigger="scheduled", run_date=day,
                                started_at=f"{day}T09:00:00+00:00")
    record_fetch_attempt(conn, run_id=run_id, source_family=source, target_kind="query",
                         target_label=label, definition_hash=None, status="failed",
                         error_kind="timeout", started_at=f"{day}T09:00:00+00:00",
                         ended_at=f"{day}T09:01:00+00:00")
    finish_pipeline_run(conn, run_id, status="degraded", ended_at=f"{day}T09:05:00+00:00")


def test_failed_fetch_targets_surface_at_the_top_of_the_report(conn, tmp_path):
    make_job(conn, first_seen=f"{DAY}T08:00:00")
    _failed_target(conn)

    out = _report(conn, tmp_path)

    # Header placement: the warning sits above the summary line, where it cannot be missed.
    assert any(line.startswith("⚠ pipeline health:") for line in out.splitlines()[:4])
    assert "1 fetch target failed (adzuna)" in out


def test_clean_day_report_carries_no_health_warning(conn, tmp_path):
    # A successful run with zero failed targets is the quiet state: no warning line at all.
    # This also pins the past-date staleness suppression — if today's live readings leaked
    # into a `--date` rebuild, this healthy-day report would grow a warning on any machine
    # whose real stamps happen to be stale.
    make_job(conn, first_seen=f"{DAY}T08:00:00")
    run_id = start_pipeline_run(conn, trigger="scheduled", run_date=DAY,
                                started_at=f"{DAY}T09:00:00+00:00")
    record_fetch_attempt(conn, run_id=run_id, source_family="linkedin", target_kind="search",
                         target_label="ai_lead", definition_hash=None, status="success",
                         returned_count=1, eligible_count=1, inserted_count=1, repost_count=0,
                         started_at=f"{DAY}T09:00:00+00:00", ended_at=f"{DAY}T09:01:00+00:00")
    finish_pipeline_run(conn, run_id, status="succeeded", ended_at=f"{DAY}T09:05:00+00:00")

    out = _report(conn, tmp_path)

    assert "pipeline health" not in out


def test_another_days_failures_stay_out_of_this_days_report(conn, tmp_path):
    # Failed-target facts are day-scoped through pipeline_runs.run_date: yesterday's outage
    # must not re-alert on today's page.
    _failed_target(conn, day="2026-08-06")

    out = _report(conn, tmp_path)

    assert "pipeline health" not in out


def test_warning_line_phrases_staleness_and_failed_targets():
    readings = {"readings": [
        {"signal": "pipeline_run", "last_at": None, "age_hours": None,
         "threshold_hours": 26, "stale": True},
        {"signal": "canary", "last_at": "2026-08-09T00:00:00+00:00", "age_hours": 216.0,
         "threshold_hours": 192, "stale": True},
        {"signal": "second_judge", "last_at": "2026-08-17T00:00:00", "age_hours": 24.0,
         "threshold_hours": 48, "stale": False},
    ]}

    line = report._health_warning_line(readings, 2, ["adzuna", "linkedin"])

    # A never-recorded stamp reads as a neutral fact; a fresh reading stays out entirely.
    assert line == ("⚠ pipeline health: no successful run on record; "
                    "canary last ran 9d ago; 2 fetch targets failed (adzuna, linkedin)")


def test_warning_line_is_none_when_everything_is_healthy():
    healthy = {"readings": [
        {"signal": "pipeline_run", "last_at": "2026-08-17T22:00:00", "age_hours": 2.0,
         "threshold_hours": 26, "stale": False},
    ]}

    assert report._health_warning_line(healthy, 0, []) is None
    # Suppressed staleness (a past-date rebuild) with a clean day is also fully quiet.
    assert report._health_warning_line(None, 0, []) is None
