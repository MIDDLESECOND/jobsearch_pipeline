"""Unified role activity is chain-scoped, factual, bounded, and read-only."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_job


def _seed_activity(conn):
    root = make_job(
        conn,
        job_url="root",
        title="AI Lead",
        company="Example",
        source="linkedin",
        first_seen="2026-08-01T09:00:00",
        app_status="applied",
        status_date="2026-08-02",
    )
    make_job(
        conn,
        job_url="relist",
        title="AI Lead",
        company="Example",
        source="manual",
        repost_of="root",
        first_seen="2026-08-03T10:00:00",
        app_status="applied",
        status_date="2026-08-02",
    )
    conn.execute(
        """INSERT INTO app_events(job_url,event_type,event_date,note,created_at)
           VALUES ('root','recruiter_screen','2026-08-04','Discussed scope',
                   '2026-08-04T18:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO application_materials
           (job_url,interaction_url,kind,object_sha256,original_name,attached_at)
           VALUES ('root','relist','resume',?,'resume-v3.pdf','2026-08-02T15:00:00+00:00')""",
        ("a" * 64,),
    )
    conn.execute(
        """INSERT INTO job_contacts
           (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
           VALUES ('root','relist','Jane Doe','Recruiter','recruiter',NULL,NULL,NULL,
                   '2026-08-03T12:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO job_tasks
           (job_url,interaction_url,title,note,due_date,status,created_at,closed_at,version)
           VALUES ('root','root','Prepare examples',NULL,'2026-08-04','completed',
                   '2026-08-02T16:00:00+00:00','2026-08-04T17:00:00+00:00',1)"""
    )
    conn.execute(
        """INSERT INTO job_interviews
           (job_url,interaction_url,title,starts_at,duration_minutes,mode,location,
            meeting_url,note,status,created_at,updated_at,version)
           VALUES ('root','relist','Panel','2026-08-10T15:00:00+00:00',60,'video',NULL,
                   NULL,NULL,'cancelled','2026-08-03T13:00:00+00:00',
                   '2026-08-05T14:00:00+00:00',1)"""
    )
    conn.execute(
        """INSERT INTO role_stars(job_url,starred_at,starred,version)
           VALUES ('root','2026-08-02T14:00:00+00:00',1,1)"""
    )
    conn.commit()
    return root


def test_role_timeline_unifies_current_chain_activity_without_private_payloads(conn):
    from timeline import _time_key, role_timeline

    root = _seed_activity(conn)
    result = role_timeline(conn, root)

    assert result["total"] == 11
    assert result["truncated"] is False
    items = result["items"]
    instants = [_time_key(item["occurred_at"]) for item in items]
    assert instants == sorted(instants, reverse=True)
    assert {item["kind"] for item in items} == {
        "posting", "decision", "event", "material", "contact", "task_created",
        "task_closed", "interview_created", "interview_updated", "star",
    }
    assert sum(item["kind"] == "posting" for item in items) == 2
    by_kind = {item["kind"]: item for item in items if item["kind"] != "posting"}
    assert by_kind["decision"]["title"] == "Marked applied"
    assert by_kind["event"]["detail"] == "Discussed scope"
    assert by_kind["material"]["detail"] == "resume-v3.pdf"
    assert by_kind["contact"]["detail"] == "Jane Doe · recruiter · Recruiter"
    assert by_kind["task_closed"]["title"] == "Task completed"
    assert by_kind["interview_updated"]["title"] == "Interview cancelled"
    assert by_kind["star"]["title"] == "Role starred"
    serialized = repr(items)
    assert "email" not in serialized and "profile_url" not in serialized
    assert "object_sha256" not in serialized
    assert not conn.in_transaction


def test_timeline_maps_former_roots_through_current_chain_and_unlink_separates(conn):
    from timeline import role_timeline

    early = make_job(conn, job_url="early", first_seen="2026-08-01T00:00:00")
    late = make_job(conn, job_url="late", first_seen="2026-08-02T00:00:00")
    conn.execute(
        """INSERT INTO job_contacts
           (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
           VALUES ('late','late','Late Contact',NULL,'other',NULL,NULL,NULL,
                   '2026-08-02T01:00:00+00:00')"""
    )
    conn.execute("UPDATE jobs SET repost_of='early',repost_source='manual' WHERE job_url='late'")
    conn.commit()

    merged = role_timeline(conn, early)
    assert any(item["detail"].startswith("Late Contact") for item in merged["items"])

    conn.execute("UPDATE jobs SET repost_of=NULL,repost_source=NULL WHERE job_url='late'")
    conn.commit()
    split = role_timeline(conn, early)
    assert not any(item["detail"].startswith("Late Contact") for item in split["items"])


def test_timeline_does_not_invent_history_the_tables_do_not_retain(conn):
    from timeline import role_timeline

    row = make_job(conn, job_url="root", first_seen="2026-08-01T00:00:00")
    conn.execute(
        """INSERT INTO job_tasks
           (job_url,interaction_url,title,note,due_date,status,created_at,closed_at,version)
           VALUES ('root','root','Open task',NULL,'2026-08-10','open',
                   '2026-08-02T00:00:00+00:00',NULL,3)"""
    )
    conn.execute(
        """INSERT INTO role_stars(job_url,starred_at,starred,version)
           VALUES ('root','2026-08-03T00:00:00+00:00',0,2)"""
    )
    conn.commit()

    items = role_timeline(conn, row)["items"]

    assert [item["kind"] for item in items].count("task_created") == 1
    assert not any(item["kind"] == "task_closed" for item in items)
    assert not any(item["kind"] == "star" for item in items)
    assert not any(item["kind"] == "decision" for item in items)


def test_timeline_is_bounded_and_preserves_caller_transaction(conn):
    from timeline import role_timeline

    row = make_job(conn, job_url="root", first_seen="2026-08-01T00:00:00")
    for index in range(5):
        conn.execute(
            """INSERT INTO app_events(job_url,event_type,event_date,note,created_at)
               VALUES ('root','note','2026-08-02',?,?)""",
            (f"note {index}", f"2026-08-02T00:00:0{index}+00:00"),
        )
    conn.commit()
    conn.execute("INSERT INTO meta(key,value) VALUES ('pending','caller')")

    traced = []
    conn.set_trace_callback(traced.append)
    result = role_timeline(conn, row, limit=3)
    conn.set_trace_callback(None)

    assert result["total"] == 6
    assert len(result["items"]) == 3 and result["truncated"] is True
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM meta WHERE key='pending'").fetchone()[0] == "caller"
    assert any(
        "FROM APP_EVENTS" in statement.upper() and "LIMIT 3" in statement.upper()
        for statement in traced
    )
    conn.rollback()

    with pytest.raises(ValueError, match="limit"):
        role_timeline(conn, row, limit=0)


def test_timeline_orders_offset_timestamps_before_limit_by_actual_instant(conn):
    from timeline import role_timeline

    row = make_job(conn, job_url="root", first_seen="2026-08-01T00:00:00")
    for name, created_at in (
        ("Earlier instant", "2026-08-02T10:00:00+05:00"),
        ("Later instant", "2026-08-02T08:00:00+00:00"),
    ):
        conn.execute(
            """INSERT INTO job_contacts
               (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
               VALUES ('root','root',?,NULL,'other',NULL,NULL,NULL,?)""",
            (name, created_at),
        )
    conn.commit()

    contacts = [
        item for item in role_timeline(conn, row, limit=1)["items"]
        if item["kind"] == "contact"
    ]

    assert len(contacts) == 1
    assert contacts[0]["detail"].startswith("Later instant")


def test_timeline_interprets_legacy_naive_timestamps_as_local_instants(conn, monkeypatch):
    import timeline

    real_time_key = timeline._time_key
    local_zone = timezone(timedelta(hours=-6))
    monkeypatch.setattr(
        timeline, "_time_key",
        lambda value: real_time_key(value, naive_timezone=local_zone),
    )
    local_naive = datetime(2026, 1, 15, 10, 0, 0)
    other_utc = datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc)

    row = make_job(conn, job_url="root", first_seen="2025-01-01T00:00:00")
    for name, created_at in (
        ("Local instant", local_naive.isoformat()),
        ("Aware instant", other_utc.isoformat()),
    ):
        conn.execute(
            """INSERT INTO job_contacts
               (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
               VALUES ('root','root',?,NULL,'other',NULL,NULL,NULL,?)""",
            (name, created_at),
        )
    conn.commit()

    result = timeline.role_timeline(conn, row, limit=1)

    assert result["items"][0]["kind"] == "contact"
    assert result["items"][0]["detail"].startswith("Local instant")


def test_timeline_time_key_can_apply_an_explicit_local_zone():
    from timeline import _time_key

    chicago_winter = timezone(timedelta(hours=-6))
    assert _time_key(
        "2026-01-15T10:00:00", naive_timezone=chicago_winter,
    ) > _time_key("2026-01-15T15:30:00+00:00")


def test_timeline_rejects_abnormally_large_role_chain_before_loading_it(conn, monkeypatch):
    import timeline

    root = make_job(conn, job_url="root", first_seen="2026-08-01T00:00:00")
    make_job(
        conn,
        job_url="relist",
        repost_of="root",
        first_seen="2026-08-02T00:00:00",
    )
    conn.commit()
    monkeypatch.setattr(timeline, "MAX_TIMELINE_CHAIN_MEMBERS", 1, raising=False)
    monkeypatch.setattr(
        timeline,
        "effective_decision",
        lambda *_args, **_kwargs: pytest.fail("collection started after chain overflow"),
    )
    traced = []
    conn.set_trace_callback(traced.append)

    try:
        with pytest.raises(ValueError, match="chain exceeds"):
            timeline.role_timeline(conn, root, limit=1)
    finally:
        conn.set_trace_callback(None)

    assert not any(
        "ORDER BY FIRST_SEEN,JOB_URL" in statement.upper() for statement in traced
    )
