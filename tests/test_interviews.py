"""Chain-scoped interview scheduling and local calendar export."""

from datetime import datetime, timezone
import re
import sqlite3

import pytest

import chain
import interviews
from conftest import make_job


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def _add(conn, row, **overrides):
    fields = {
        "title": "Technical interview",
        "starts_at": "2026-08-10T15:00:00-05:00",
        "duration_minutes": 60,
        "mode": "video",
        "location": "",
        "meeting_url": "https://meet.example.com/room",
        "note": "Prepare architecture story",
    }
    fields.update(overrides)
    return interviews.add_interview(conn, row, **fields)


def test_add_update_cancel_and_ics_export(conn):
    row = make_job(conn, job_url="root", title="AI Lead", company="Acme",
                   app_status="applied", status_date="2026-08-01")
    added = _add(conn, row)
    item = added["interview"]

    assert item["starts_at"] == "2026-08-10T20:00:00+00:00"
    assert item["status"] == interviews.INTERVIEW_SCHEDULED
    assert item["version"] == 0 and added["interviews"] == [item]

    update_result = interviews.change_interview(
        conn, row, item["id"], "update", expected_version=item["version"],
        title="System design; final", starts_at="2026-08-11T16:30:00-05:00",
        duration_minutes=75, mode="video", location="",
        meeting_url="https://meet.example.com/final", note="Line one\nLine two",
    )
    assert update_result is not None
    updated = update_result["interview"]
    assert updated["starts_at"] == "2026-08-11T21:30:00+00:00"
    assert updated["duration_minutes"] == 75 and updated["version"] == 1

    calendar = interviews.interview_ics(conn, row, updated["id"], now=NOW)
    assert calendar is not None
    assert "DTSTART:20260811T213000Z" in calendar
    assert "DTEND:20260811T224500Z" in calendar
    assert "SUMMARY:System design\\; final — AI Lead at Acme" in calendar
    assert "Line one\\nLine two" in calendar
    assert calendar.endswith("\r\n")

    cancel_result = interviews.change_interview(
        conn, row, updated["id"], "cancel", expected_version=updated["version"],
    )
    assert cancel_result is not None
    cancelled = cancel_result["interview"]
    assert cancelled["status"] == interviews.INTERVIEW_CANCELLED
    assert cancelled["version"] == 2
    assert interviews.chain_interviews(conn, row) == []
    assert interviews.chain_interviews(conn, row, include_cancelled=True) == [cancelled]
    with pytest.raises(ValueError, match="scheduled interview"):
        interviews.interview_ics(conn, row, cancelled["id"], now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "", "title is required"),
        ("starts_at", "2026-08-10T15:00:00", "timezone"),
        ("starts_at", "not-a-date", "ISO 8601"),
        ("duration_minutes", 0, "15 to 480"),
        ("duration_minutes", True, "integer"),
        ("mode", "telepathy", "mode must be one of"),
        ("meeting_url", "javascript:alert(1)", "http(s) URL"),
        ("note", "x" * 4001, "note exceeds 4000"),
    ],
)
def test_validation_and_applied_guard(conn, field, value, message):
    row = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    with pytest.raises(ValueError, match=re.escape(message)):
        _add(conn, row, **{field: value})

    passed = make_job(conn, job_url="passed", app_status="passed", status_date="2026-08-01")
    with pytest.raises(ValueError, match="applied chain"):
        _add(conn, passed)


def test_merge_unlink_and_stale_rows_follow_current_chain(conn):
    early = make_job(conn, job_url="early", first_seen="2026-08-01T00:00:00",
                     app_status="applied", status_date="2026-08-01")
    stale_late = make_job(conn, job_url="late", first_seen="2026-08-02T00:00:00",
                          app_status="applied", status_date="2026-08-01")
    early_item = _add(conn, early, title="Early round")["interview"]

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)
    # Reads refresh a row fetched before the merge and authorize the now-current chain.
    assert [item["title"] for item in interviews.chain_interviews(conn, stale_late)] == [
        "Early round",
    ]
    late_item = _add(conn, stale_late, title="Merged round",
                     starts_at="2026-08-12T15:00:00-05:00")["interview"]
    owner = conn.execute(
        "SELECT job_url FROM job_interviews WHERE id=?", (late_item["id"],)
    ).fetchone()[0]
    assert owner == "early"
    assert {item["title"] for item in interviews.chain_interviews(conn, early)} == {
        "Early round", "Merged round",
    }

    merged_late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert chain.dupe_unlink(conn, merged_late)[0]
    assert interviews.chain_interviews(conn, merged_late) == []
    assert interviews.interview_ics(conn, merged_late, early_item["id"], now=NOW) is None
    assert interviews.change_interview(
        conn, merged_late, early_item["id"], "cancel",
        expected_version=early_item["version"],
    ) is None


def test_stale_version_and_caller_transaction_are_refused(conn):
    row = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    original = _add(conn, row)["interview"]
    update_result = interviews.change_interview(
        conn, row, original["id"], "update", expected_version=0,
        title="Updated", starts_at="2026-08-12T15:00:00-05:00",
        duration_minutes=60, mode="phone", location="", meeting_url="", note="",
    )
    assert update_result is not None
    updated = update_result["interview"]
    with pytest.raises(ValueError, match="changed; refresh and retry"):
        interviews.change_interview(
            conn, row, original["id"], "cancel", expected_version=0,
        )
    assert interviews.chain_interviews(conn, row)[0]["version"] == updated["version"]

    conn.execute("INSERT INTO meta(key,value) VALUES ('caller','pending')")
    with pytest.raises(RuntimeError, match="clean database connection"):
        _add(conn, row, title="Blocked")
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM meta WHERE key='caller'").fetchone()[0] == "pending"
    conn.rollback()


def test_reads_hold_one_snapshot_across_a_concurrent_unlink(conn, monkeypatch):
    root = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    relist = make_job(conn, job_url="relist", repost_of="root", app_status="applied",
                      status_date="2026-08-01")
    item = _add(conn, root, title="Private meeting")["interview"]
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    writer = sqlite3.connect(db_path)
    original_chain_urls = interviews._chain_urls

    def unlink_after_snapshot_starts(read_conn, current):
        writer.execute("UPDATE jobs SET repost_of=NULL WHERE job_url='relist'")
        writer.commit()
        return original_chain_urls(read_conn, current)

    monkeypatch.setattr(interviews, "_chain_urls", unlink_after_snapshot_starts)
    try:
        # Each response is consistent at its first read: it may finish on the old chain, but
        # cannot mix old authorization with new membership inside one response.
        assert interviews.chain_interviews(conn, relist)[0]["id"] == item["id"]
        assert interviews.chain_interviews(conn, relist) == []

        conn.execute("UPDATE jobs SET repost_of='root' WHERE job_url='relist'")
        conn.commit()
        calendar = interviews.interview_ics(conn, relist, item["id"], now=NOW)
        assert calendar is not None and "Private meeting" in calendar
        assert interviews.interview_ics(conn, relist, item["id"], now=NOW) is None
    finally:
        writer.close()


def test_upcoming_summaries_are_batched_bounded_and_chain_scoped(conn):
    root = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    relist = make_job(conn, job_url="relist", repost_of="root", app_status="applied",
                      status_date="2026-08-01")
    other = make_job(conn, job_url="other", app_status="applied", status_date="2026-08-01")
    _add(conn, relist, title="Soon", starts_at="2026-08-07T10:00:00+00:00")
    _add(conn, root, title="Later", starts_at="2026-08-09T10:00:00+00:00")
    old = _add(conn, root, title="Past", starts_at="2026-08-05T10:00:00+00:00")["interview"]
    cancelled = _add(conn, other, title="Cancelled", starts_at="2026-08-08T10:00:00+00:00")["interview"]
    interviews.change_interview(
        conn, other, cancelled["id"], "cancel", expected_version=cancelled["version"])

    got = interviews.interview_summaries(conn, [root, relist, other], now=NOW)
    assert [item["title"] for item in got["root"]] == ["Soon", "Later"]
    assert [item["title"] for item in got["relist"]] == ["Soon", "Later"]
    assert got["other"] == []
    assert all(item["id"] != old["id"] for item in got["root"])


def test_schema_is_local_and_indexed(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_interviews)")}
    assert columns == {
        "id", "job_url", "interaction_url", "title", "starts_at",
        "duration_minutes", "mode", "location", "meeting_url", "note",
        "status", "created_at", "updated_at", "version",
    }
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(job_interviews)")}
    assert "idx_job_interviews_chain_time" in indexes
    assert "idx_job_interviews_upcoming" in indexes


def test_ics_folds_long_unicode_content_at_75_octets(conn):
    row = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    note = "准备系统设计案例" * 20
    item = _add(conn, row, note=note)["interview"]

    calendar = interviews.interview_ics(conn, row, item["id"], now=NOW)
    assert calendar is not None
    physical_lines = calendar.split("\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in physical_lines)
    assert note in calendar.replace("\r\n ", "")
