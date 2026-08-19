"""Pipeline health records facts; search metrics stay bounded and descriptive."""

import contextlib
import sys
from datetime import date

import pytest

from conftest import make_job


def _cfg():
    return {
        "settings": {
            "max_description_chars": 12000,
            "ats": {
                "companies": [
                    {"slug": "example", "board": "greenhouse", "name": "Example"}
                ]
            },
        },
        "searches": [
            {"name": "ai_lead", "term": "AI lead", "adzuna": {"what": "AI lead"}},
            {"name": "quiet_track", "term": "rare title"},
        ],
    }


def test_fetch_summary_preserves_int_contract_and_classifies_partial_failure():
    from health import FetchSummary

    result = FetchSummary(3, units=4, successes=3, failures=1)

    assert result == 3 and int(result) == 3
    assert result.status == "partial"
    assert (result.units, result.successes, result.failures) == (4, 3, 1)
    assert FetchSummary.skipped("not configured").status == "skipped"
    assert FetchSummary.failed("TimeoutError").status == "failed"
    with pytest.raises(ValueError, match="sum"):
        FetchSummary(0, units=2, successes=1, failures=0)


def test_run_lifecycle_persists_bounded_structured_source_facts(conn):
    from health import (
        finish_pipeline_run, health_snapshot, record_fetch_attempt, start_pipeline_run,
    )

    run_id = start_pipeline_run(
        conn, trigger="scheduled", run_date="2026-08-07",
        started_at="2026-08-07T09:00:00+00:00",
    )
    record_fetch_attempt(
        conn, run_id=run_id, source_family="linkedin", target_kind="search",
        target_label="ai_lead", definition_hash="a" * 64, status="success",
        returned_count=6, eligible_count=6, inserted_count=4, repost_count=1,
        started_at="2026-08-07T09:00:00+00:00",
        ended_at="2026-08-07T09:01:00+00:00",
    )
    record_fetch_attempt(
        conn, run_id=run_id, source_family="adzuna", target_kind="query",
        target_label="ai_lead", definition_hash="b" * 64, status="failed",
        error_kind="timeout", started_at="2026-08-07T09:01:00+00:00",
        ended_at="2026-08-07T09:01:30+00:00",
    )
    finish_pipeline_run(
        conn, run_id, status="degraded", ended_at="2026-08-07T09:05:00+00:00"
    )

    snapshot = health_snapshot(
        conn, _cfg(), today=date(2026, 8, 7), days=30, run_limit=10
    )

    run = snapshot["runs"][0]
    assert run["id"] == run_id and run["status"] == "degraded"
    assert run["duration_seconds"] == 300
    sources = {item["source"]: item for item in run["sources"]}
    assert sources["linkedin"]["status"] == "completed"
    assert sources["linkedin"]["inserted"] == 4
    assert (sources["linkedin"]["attempted"], sources["linkedin"]["succeeded"],
            sources["linkedin"]["failed"]) == (1, 1, 0)
    assert sources["adzuna"]["status"] == "failed"
    assert sources["adzuna"]["error_kinds"] == ["timeout"]
    assert run["attempts"][0]["target_label"] == "ai_lead"
    # Exception messages/URLs are deliberately not part of the storage or response schema.
    assert "error_message" not in repr(snapshot)
    with pytest.raises(ValueError, match="already finished"):
        record_fetch_attempt(
            conn, run_id=run_id, source_family="linkedin", target_kind="search",
            target_label="late", definition_hash=None, status="success",
        )


def _running_rows(conn, *started_at):
    """Insert 'running' rows with exact start times, bypassing start_pipeline_run's own reaper."""
    ids = []
    for stamp in started_at:
        cur = conn.execute(
            """INSERT INTO pipeline_runs (started_at,ended_at,trigger,run_date,status)
               VALUES (?,NULL,'scheduled',?,'running')""", (stamp, stamp[:10]))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def test_stale_running_rows_are_abandoned_but_a_live_long_run_is_not(conn):
    # A killed process never writes its own terminal status, so its row sits 'running' forever
    # (measured 2026-08-16: 5 of 44 runs, oldest 8 days). The reaper retires those — but eval
    # backlogs after a peak-deferral have measured 85-194 minutes and concurrent runs are
    # deliberately unguarded, so a run that is merely SLOW must survive another one starting.
    from health import abandon_stale_runs

    # Inserted directly, not through start_pipeline_run: that entry point reaps against the REAL
    # clock, which would retire these fixtures before the assertions ever run.
    dead, slow = _running_rows(conn, "2026-08-08T22:00:00+00:00", "2026-08-17T04:00:00+00:00")
    now = "2026-08-17T07:30:00+00:00"          # `slow` is 3.5h in — legitimately long, not dead

    assert abandon_stale_runs(conn, now=now) == 1
    states = dict(conn.execute("SELECT id, status FROM pipeline_runs").fetchall())
    assert states[dead] == "abandoned"
    assert states[slow] == "running"
    # ended_at stays NULL: the row's real end time is unknown and stamping "now" would invent a
    # duration for a run that stopped hours earlier.
    assert conn.execute("SELECT ended_at FROM pipeline_runs WHERE id=?", (dead,)).fetchone()[0] is None
    # Idempotent — a second pass finds nothing left to retire.
    assert abandon_stale_runs(conn, now=now) == 0


def test_abandoned_run_cannot_be_finished_or_accrue_attempts(conn):
    # Both guarded UPDATEs in health.py require status='running', so retiring a row must close
    # it to later writes; a dead run acquiring a terminal status or new attempts would be a lie.
    import pytest

    from health import abandon_stale_runs, finish_pipeline_run, record_fetch_attempt

    (dead,) = _running_rows(conn, "2026-08-08T22:00:00+00:00")
    assert abandon_stale_runs(conn, now="2026-08-17T07:30:00+00:00") == 1
    with pytest.raises(ValueError):
        finish_pipeline_run(conn, dead, status="succeeded")
    with pytest.raises(ValueError):
        record_fetch_attempt(conn, run_id=dead, source_family="linkedin", target_kind="search",
                             target_label="x", definition_hash="d" * 16, status="success")


def test_starting_a_run_reaps_stale_rows(conn):
    # The reaper hangs off start_pipeline_run because a starting run is the one moment the
    # pipeline is certainly executing — no separate scheduled sweep to forget to wire up.
    from health import start_pipeline_run

    conn.execute(
        """INSERT INTO pipeline_runs (started_at,ended_at,trigger,run_date,status)
           VALUES ('2026-01-01T00:00:00+00:00',NULL,'scheduled','2026-01-01','running')""")
    conn.commit()
    start_pipeline_run(conn, trigger="manual", run_date="2026-08-17")
    assert conn.execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE status='abandoned'").fetchone()[0] == 1


def test_run_history_marks_globally_bounded_attempt_details_as_partial(conn, monkeypatch):
    import health
    from health import finish_pipeline_run, health_snapshot, start_pipeline_run

    older = start_pipeline_run(conn, trigger="manual", run_date="2026-08-06")
    finish_pipeline_run(conn, older, status="succeeded")
    newer = start_pipeline_run(conn, trigger="manual", run_date="2026-08-07")
    finish_pipeline_run(conn, newer, status="succeeded")
    rows = []
    for run_id, prefix in ((older, "old"), (newer, "new")):
        for index in range(2):
            rows.append((
                run_id, "linkedin", "search", f"{prefix}-{index}",
                "2026-08-07T09:00:00+00:00", "2026-08-07T09:01:00+00:00",
                "success", 0, 0, 0, 0,
            ))
    conn.executemany(
        """INSERT INTO pipeline_fetch_attempts
           (run_id,source_family,target_kind,target_label,started_at,ended_at,status,
            returned_count,eligible_count,inserted_count,repost_count)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    monkeypatch.setattr(health, "MAX_ATTEMPTS_IN_RESPONSE", 3)

    runs = health_snapshot(
        conn, _cfg(), today=date(2026, 8, 7), days=30, run_limit=2,
    )["runs"]

    assert [(run["id"], len(run["attempts"])) for run in runs] == [
        (newer, 2), (older, 1),
    ]
    assert [(run["attempts_total"], run["attempts_truncated"]) for run in runs] == [
        (2, False), (2, True),
    ]


def test_search_effectiveness_dedupes_chains_and_includes_configured_zero_rows(conn):
    from health import health_snapshot

    make_job(
        conn, job_url="root", source="linkedin", search_name="ai_lead",
        first_seen="2026-08-01T09:00:00", status="evaluated", verdict="PASS",
        fit_score=17, app_status="applied", status_date="2026-08-02",
    )
    make_job(
        conn, job_url="relist", repost_of="root", source="linkedin",
        search_name="ai_lead", first_seen="2026-08-02T09:00:00",
        status="repost_decided", app_status="applied", status_date="2026-08-02",
    )
    make_job(
        conn, job_url="cross-source-relist", repost_of="root", source="adzuna",
        search_name="other_track", first_seen="2026-08-02T10:00:00",
        status="repost_decided", app_status="applied", status_date="2026-08-02",
    )
    make_job(
        conn, job_url="manual", source="manual", search_name="ai_lead",
        first_seen="2026-08-03T09:00:00", status="new",
    )
    make_job(
        conn, job_url="old", source="linkedin", search_name="quiet_track",
        first_seen="2026-06-01T09:00:00", status="evaluated",
        verdict="RECRUITER_ONLY", fit_score=14,
    )
    make_job(
        conn, job_url="old-relist", repost_of="old", source="adzuna",
        search_name="quiet_track", first_seen="2026-08-05T09:00:00",
        status="repost_evaluated",
    )
    make_job(
        conn, job_url="offset-earlier", source="ashby", search_name="offset_track",
        first_seen="2026-08-04T10:00:00+05:00", status="new",
    )
    make_job(
        conn, job_url="offset-later", source="ashby", search_name="offset_track",
        first_seen="2026-08-04T08:00:00+00:00", status="new",
    )
    conn.commit()

    snapshot = health_snapshot(
        conn, _cfg(), today=date(2026, 8, 7), days=30, run_limit=10
    )
    rows = {(row["source"], row["search_name"]): row
            for row in snapshot["search_effectiveness"]["items"]}

    linkedin = rows[("linkedin", "ai_lead")]
    assert linkedin["postings"] == 2
    assert linkedin["roles"] == 1
    assert linkedin["strong_roles"] == 1 and linkedin["applied_roles"] == 1
    assert linkedin["configured"] is True
    assert rows[("adzuna", "ai_lead")]["postings"] == 0
    assert rows[("adzuna", "ai_lead")]["roles"] == 0
    assert rows[("adzuna", "ai_lead")]["strong_roles"] == 0
    assert rows[("adzuna", "ai_lead")]["applied_roles"] == 0
    assert rows[("adzuna", "other_track")]["postings"] == 1
    assert rows[("adzuna", "other_track")]["roles"] == 0
    assert rows[("linkedin", "quiet_track")]["roles"] == 0
    assert rows[("linkedin", "quiet_track")]["last_discovered_at"] == \
        "2026-06-01T09:00:00"
    assert rows[("adzuna", "quiet_track")]["postings"] == 1
    assert rows[("adzuna", "quiet_track")]["roles"] == 0
    assert rows[("greenhouse", "ats:example")]["configured"] is True
    assert rows[("manual", "ai_lead")]["configured"] is False
    assert rows[("ashby", "offset_track")]["last_discovered_at"] == \
        "2026-08-04T08:00:00+00:00"
    assert "earliest stored posting" in snapshot["definitions"]["attribution"]
    assert snapshot["search_effectiveness"]["truncated"] is False


def test_health_snapshot_validates_response_bounds(conn):
    from health import health_snapshot

    with pytest.raises(ValueError, match="days"):
        health_snapshot(conn, _cfg(), days=0)
    with pytest.raises(ValueError, match="run_limit"):
        health_snapshot(conn, _cfg(), run_limit=101)


def test_real_ats_targets_record_zero_result_success_and_failure(conn, monkeypatch):
    import fetch
    from health import (reset_active_pipeline_run, set_active_pipeline_run,
                        start_pipeline_run)

    cfg = {
        "settings": {
            "max_description_chars": 12000,
            "ats": {
                "title_any": ["analyst"], "delay_between_calls": 0,
                "companies": [
                    {"slug": "good", "board": "greenhouse"},
                    {"slug": "bad", "board": "greenhouse"},
                ],
            },
        }
    }

    def fake_get(url):
        if "/good/" in url:
            return {"jobs": []}
        raise TimeoutError("private URL and token must stay out of SQLite")

    monkeypatch.setattr(fetch, "_ats_get", fake_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    run_id = start_pipeline_run(conn, trigger="manual", run_date="2026-08-07")
    token = set_active_pipeline_run(run_id)
    try:
        result = fetch.fetch_ats(cfg, conn)
    finally:
        reset_active_pipeline_run(token)

    assert result == 0 and result.status == "partial"
    attempts = conn.execute(
        "SELECT * FROM pipeline_fetch_attempts WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    assert [(row["target_label"], row["status"], row["returned_count"], row["error_kind"])
            for row in attempts] == [
        ("good", "success", 0, None), ("bad", "failed", None, "timeout")
    ]
    assert all(row["definition_hash"] and len(row["definition_hash"]) == 64
               for row in attempts)
    assert "private URL" not in repr([dict(row) for row in attempts])


def test_unconfigured_adzuna_is_skipped_without_requesting_credentials(conn, monkeypatch):
    import fetch
    from health import (reset_active_pipeline_run, set_active_pipeline_run,
                        start_pipeline_run)

    monkeypatch.setattr(
        fetch, "_ensure_api_key",
        lambda *_args, **_kwargs: pytest.fail("credentials should not be read when unconfigured"),
    )
    run_id = start_pipeline_run(conn, trigger="manual", run_date="2026-08-07")
    token = set_active_pipeline_run(run_id)
    try:
        result = fetch.fetch_adzuna({"settings": {}, "searches": []}, conn)
    finally:
        reset_active_pipeline_run(token)

    assert result.status == "skipped"
    attempt = conn.execute(
        "SELECT status,skip_reason FROM pipeline_fetch_attempts WHERE run_id=?", (run_id,)
    ).fetchone()
    assert (attempt["status"], attempt["skip_reason"]) == \
        ("skipped", "no configured queries")


def test_failed_target_rolls_back_partial_jobs_before_committing_failure_fact(conn, monkeypatch):
    import fetch
    from health import (reset_active_pipeline_run, set_active_pipeline_run,
                        start_pipeline_run)

    payload = {"jobs": [
        {"absolute_url": "https://example.test/1", "title": "Data Analyst",
         "location": {"name": "New York"}, "content": "one"},
        {"absolute_url": "https://example.test/2", "title": "Data Analyst",
         "location": {"name": "New York"}, "content": "two"},
    ]}
    cfg = {"settings": {"max_description_chars": 12000, "ats": {
        "title_any": ["analyst"], "delay_between_calls": 0,
        "companies": [{"slug": "example", "board": "greenhouse"}],
    }}}
    real_insert = fetch._insert_posting

    def fail_second(conn_, **kwargs):
        if kwargs["url"].endswith("/2"):
            raise RuntimeError("row failed")
        return real_insert(conn_, **kwargs)

    monkeypatch.setattr(fetch, "_ats_get", lambda _url: payload)
    monkeypatch.setattr(fetch, "_insert_posting", fail_second)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    run_id = start_pipeline_run(conn, trigger="manual", run_date="2026-08-07")
    token = set_active_pipeline_run(run_id)
    try:
        result = fetch.fetch_ats(cfg, conn)
    finally:
        reset_active_pipeline_run(token)

    assert result.status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    attempt = conn.execute(
        "SELECT * FROM pipeline_fetch_attempts WHERE run_id=?", (run_id,)
    ).fetchone()
    assert attempt["status"] == "failed" and attempt["error_kind"] == "unexpected"
    assert attempt["returned_count"] is None and attempt["inserted_count"] is None


def test_health_schema_is_idempotent_and_preserves_run_history(tmp_path):
    import core
    from health import finish_pipeline_run, start_pipeline_run

    cfg = {"settings": {"db_path": str(tmp_path / "health.db")}}
    first = core.get_db(cfg)
    run_id = start_pipeline_run(first, trigger="manual", run_date="2026-08-07")
    finish_pipeline_run(first, run_id, status="succeeded")
    first.close()

    second = core.get_db(cfg)
    try:
        assert second.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
        assert {row[0] for row in second.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} >= {"pipeline_runs", "pipeline_fetch_attempts"}
    finally:
        second.close()


def test_nested_run_contexts_do_not_cross_assign_attempts(conn):
    from health import (record_active_fetch_attempt, reset_active_pipeline_run,
                        set_active_pipeline_run, start_pipeline_run)

    first = start_pipeline_run(conn, trigger="manual", run_date="2026-08-07")
    second = start_pipeline_run(conn, trigger="scheduled", run_date="2026-08-07")
    first_token = set_active_pipeline_run(first)
    try:
        record_active_fetch_attempt(
            conn, source_family="linkedin", target_kind="search", target_label="first-a",
            definition_hash=None, status="success",
        )
        second_token = set_active_pipeline_run(second)
        try:
            record_active_fetch_attempt(
                conn, source_family="ats", target_kind="board", target_label="second",
                definition_hash=None, status="success",
            )
        finally:
            reset_active_pipeline_run(second_token)
        record_active_fetch_attempt(
            conn, source_family="linkedin", target_kind="search", target_label="first-b",
            definition_hash=None, status="success",
        )
    finally:
        reset_active_pipeline_run(first_token)

    assigned = conn.execute(
        "SELECT run_id,target_label FROM pipeline_fetch_attempts ORDER BY id"
    ).fetchall()
    assert [(row["run_id"], row["target_label"]) for row in assigned] == [
        (first, "first-a"), (second, "second"), (first, "first-b")
    ]


def _drive_pipeline(conn, monkeypatch, *, downstream_error=False, interrupted=False,
                    all_fetch_failed=False):
    import pipeline
    from health import current_pipeline_run_id, record_fetch_attempt

    def target(source, status, inserted=0):
        def fetcher(cfg, c):
            record_fetch_attempt(
                c, run_id=current_pipeline_run_id(), source_family=source,
                target_kind="family", target_label=source, definition_hash=None,
                status=status, inserted_count=inserted if status == "success" else None,
                returned_count=inserted if status == "success" else None,
                eligible_count=inserted if status == "success" else None,
                repost_count=0 if status == "success" else None,
                error_kind="timeout" if status == "failed" else None,
                skip_reason="not configured" if status == "skipped" else None,
            )
            return inserted
        return fetcher

    monkeypatch.setattr(pipeline, "load_config", _cfg)
    monkeypatch.setattr(pipeline, "get_db", lambda cfg: conn)
    monkeypatch.setattr(pipeline, "run_log", lambda label="run": contextlib.nullcontext())
    if all_fetch_failed:
        monkeypatch.setattr(pipeline, "fetch_new_jobs", target("linkedin", "failed"))
        monkeypatch.setattr(pipeline, "fetch_adzuna", target("adzuna", "failed"))
        monkeypatch.setattr(pipeline, "fetch_ats", target("ats", "failed"))
        monkeypatch.setattr(pipeline, "fetch_dice", target("dice", "failed"))
    else:
        monkeypatch.setattr(
            pipeline, "fetch_new_jobs", target("linkedin", "success", 2)
        )
        monkeypatch.setattr(
            pipeline, "fetch_adzuna",
            lambda cfg, c: (
                target("adzuna", "success", 0)(cfg, c),
                target("adzuna", "failed", 0)(cfg, c),
            )[0],
        )
        monkeypatch.setattr(pipeline, "fetch_ats", target("ats", "skipped"))
        monkeypatch.setattr(pipeline, "fetch_dice", target("dice", "skipped"))
    monkeypatch.setattr(pipeline, "requeue_error_rows", lambda c: None)
    monkeypatch.setattr(pipeline, "skip_decided_reposts", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "skip_evaluated_reposts", lambda *a, **k: None)
    if interrupted:
        def interrupt_after_write(*_args, **_kwargs):
            conn.execute(
                "INSERT INTO meta(key,value) VALUES ('unfinished_stage_write','interrupt')"
            )
            raise KeyboardInterrupt
        monkeypatch.setattr(
            pipeline, "apply_salary_filter", interrupt_after_write,
        )
    elif downstream_error:
        def fail_after_write(*_args, **_kwargs):
            conn.execute(
                "INSERT INTO meta(key,value) VALUES ('unfinished_stage_write','failure')"
            )
            raise RuntimeError("salary broke secret=VALUE")
        monkeypatch.setattr(
            pipeline, "apply_salary_filter", fail_after_write,
        )
    else:
        monkeypatch.setattr(pipeline, "apply_salary_filter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "apply_hard_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "evaluate_new_jobs", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "run"])
    pipeline.main()


def test_run_command_records_partial_sources_as_degraded(conn, monkeypatch):
    _drive_pipeline(conn, monkeypatch)

    run = conn.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()
    attempts = conn.execute(
        """SELECT source_family,status,error_kind FROM pipeline_fetch_attempts
            WHERE run_id=? ORDER BY id""",
        (run["id"],),
    ).fetchall()
    assert run["status"] == "degraded" and run["ended_at"]
    assert [(row["source_family"], row["status"], row["error_kind"])
            for row in attempts] == [
        ("linkedin", "success", None), ("adzuna", "success", None),
        ("adzuna", "failed", "timeout"), ("ats", "skipped", None),
        ("dice", "skipped", None),
    ]


def test_run_command_records_downstream_failure_without_storing_message(conn, monkeypatch):
    with pytest.raises(RuntimeError, match="salary broke"):
        _drive_pipeline(conn, monkeypatch, downstream_error=True)

    run = conn.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed"
    assert run["error_stage"] == "salary_filter"
    assert run["error_type"] == "RuntimeError" and run["ended_at"]
    assert "secret=VALUE" not in repr(dict(run))
    assert conn.execute(
        "SELECT value FROM meta WHERE key='unfinished_stage_write'"
    ).fetchone() is None


def test_run_command_records_interrupt_and_reraises_it(conn, monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        _drive_pipeline(conn, monkeypatch, interrupted=True)

    run = conn.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "interrupted"
    assert run["error_stage"] == "salary_filter"
    assert run["error_type"] == "KeyboardInterrupt" and run["ended_at"]
    assert conn.execute(
        "SELECT value FROM meta WHERE key='unfinished_stage_write'"
    ).fetchone() is None


def test_all_internal_target_failures_do_not_advance_cooldown(conn, monkeypatch):
    _drive_pipeline(conn, monkeypatch, all_fetch_failed=True)

    assert conn.execute(
        "SELECT value FROM meta WHERE key='last_run_ok_ended'"
    ).fetchone() is None
    run = conn.execute("SELECT status FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "degraded"
