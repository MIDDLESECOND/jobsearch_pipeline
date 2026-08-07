"""Portable, spreadsheet-safe role-summary export."""

import csv
from datetime import datetime, timezone
from io import StringIO

import interviews
import tasks
from conftest import make_job
import exports
import watchlist


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def _rows(text):
    assert text.startswith("\ufeff")
    return list(csv.DictReader(StringIO(text.removeprefix("\ufeff"))))


def test_role_summary_is_one_row_per_current_chain_with_local_work(conn):
    root = make_job(
        conn, job_url="root", title="AI Lead", company="Acme", source="linkedin",
        first_seen="2026-08-01T10:00:00", app_status="applied",
        status_date="2026-08-02", outcome_status="recruiter_screen",
        outcome_date="2026-08-05", resume_variant="AI-v2", channel="referral",
    )
    relist = make_job(
        conn, job_url="relist", title="AI Lead", company="Acme", source="greenhouse",
        repost_of="root", first_seen="2026-08-03T10:00:00", app_status="applied",
        status_date="2026-08-02", outcome_status="recruiter_screen",
        outcome_date="2026-08-05", resume_variant="AI-v2", channel="referral",
    )
    tasks.add_task(conn, relist, title="Prepare examples", due_date="2026-08-07")
    interviews.add_interview(
        conn, root, title="Technical round", starts_at="2026-08-08T15:00:00+00:00",
        duration_minutes=60, mode="video",
    )
    watchlist.set_starred(
        conn, relist, True, expected_starred=False, expected_version=0,
    )

    rows = _rows(exports.roles_csv(conn, now=NOW))

    assert len(rows) == 1
    item = rows[0]
    assert item["chain_root"] == "root"
    assert item["sources"] == "greenhouse | linkedin"
    assert item["posting_urls"] == "root | relist"
    assert item["application_status"] == "applied"
    assert item["starred"] == "True" and item["starred_at"]
    assert item["outcome_status"] == "recruiter_screen"
    assert item["resume_variant"] == "AI-v2" and item["channel"] == "referral"
    assert item["open_tasks"] == "1" and item["next_task_due"] == "2026-08-07"
    assert item["upcoming_interviews"] == "1"
    assert item["next_interview_at"] == "2026-08-08T15:00:00+00:00"


def test_export_neutralizes_spreadsheet_formulas_and_quotes_unicode(conn):
    make_job(
        conn, job_url="https://example.test/formula", title="=HYPERLINK(\"bad\")",
        company="+cmd|' /C calc'!A0", location="東京, Japan\nRemote",
    )

    item = _rows(exports.roles_csv(conn, now=NOW))[0]

    assert item["title"].startswith("'=")
    assert item["company"].startswith("'+")
    assert item["location"] == "東京, Japan\nRemote"


def test_export_has_stable_header_and_no_descriptions_or_private_evidence(conn):
    make_job(conn, job_url="root", description="PRIVATE JD TEXT")
    text = exports.roles_csv(conn, now=NOW)
    header = text.removeprefix("\ufeff").splitlines()[0]

    assert header == ",".join(exports.ROLE_EXPORT_FIELDS)
    assert "PRIVATE JD TEXT" not in text
    assert "description" not in exports.ROLE_EXPORT_FIELDS
    assert "note" not in exports.ROLE_EXPORT_FIELDS
