"""Action/read-model queries for the triage UI.

These tests pin the new concern boundary: workflow.py selects and pages rows, while
app.py remains responsible for flattening them into the card payload.  The queries run
against synthetic fixtures only; no test opens the real jobs.db.
"""

from datetime import date

import chain
from conftest import make_job
from states import EVENT_FOLLOWUP_SENT
import workflow


def test_backlog_page_is_bounded_sorted_and_filterable(conn):
    make_job(conn, job_url="fresh17", title="AI Architect", fit_score=17,
             first_seen="2026-08-04T10:00:00", source="ashby")
    make_job(conn, job_url="older18", title="AI Architect", fit_score=18,
             first_seen="2026-08-03T10:00:00", source="greenhouse")
    make_job(conn, job_url="fresh15", title="Data Architect", fit_score=15,
             first_seen="2026-08-04T11:00:00", source="ashby")
    make_job(conn, job_url="low9", title="AI Architect", fit_score=9,
             first_seen="2026-08-04T12:00:00", source="ashby")

    page = workflow.query_job_page(
        conn, "backlog", page=1, page_size=2,
        filters={"q": "architect", "min_score": 10},
        today=date(2026, 8, 5),
    )
    assert page["total"] == 3
    assert page["pages"] == 2
    # Both are above the apply line, so the established triage contract is
    # freshest-first (fit is only the tiebreak).
    assert [r["job_url"] for r in page["rows"]] == ["fresh15", "fresh17"]

    page2 = workflow.query_job_page(
        conn, "backlog", page=2, page_size=2,
        filters={"q": "architect", "min_score": 10},
        today=date(2026, 8, 5),
    )
    assert [r["job_url"] for r in page2["rows"]] == ["older18"]

    ashby = workflow.query_job_page(
        conn, "backlog", page=1, page_size=20,
        filters={"source": "ashby", "verdict": "PASS", "days": 2},
        today=date(2026, 8, 5),
    )
    assert {r["job_url"] for r in ashby["rows"]} == {"fresh17", "fresh15", "low9"}

    # Two calendar days means Aug 4-5, not an inclusive Aug 3-5 window.
    two_days = workflow.query_job_page(
        conn, "backlog", page=1, page_size=20, filters={"days": 2},
        today=date(2026, 8, 5),
    )
    assert "older18" not in {r["job_url"] for r in two_days["rows"]}


def test_backlog_page_excludes_any_chain_with_a_user_decision(conn):
    make_job(conn, job_url="canon", company="Chain Co", app_status="applied",
             status_date="2026-08-01")
    # Simulate an old/out-of-sync relisting whose own app_status was not propagated.  The
    # read model must still honor the chain decision, like app.jobs_for_view does.
    make_job(conn, job_url="relist", company="Chain Co", repost_of="canon")
    make_job(conn, job_url="open")

    page = workflow.query_job_page(conn, "backlog", page=1, page_size=20)
    assert [r["job_url"] for r in page["rows"]] == ["open"]
    assert page["total"] == 1


def test_applied_page_keeps_status_date_order_and_search(conn):
    make_job(conn, job_url="old", company="Acme", app_status="applied",
             status_date="2026-07-01")
    make_job(conn, job_url="new", company="Acme", app_status="applied",
             status_date="2026-08-01")
    make_job(conn, job_url="other", company="Elsewhere", app_status="applied",
             status_date="2026-08-02")

    page = workflow.query_job_page(
        conn, "applied", page=1, page_size=20, filters={"q": "acme"}
    )
    assert [r["job_url"] for r in page["rows"]] == ["new", "old"]
    assert page["total"] == 2


def test_action_center_returns_bounded_disjoint_work_queues(conn):
    make_job(conn, job_url="cold", verdict="PASS", fit_score=17,
             first_seen="2026-08-04T09:00:00")
    make_job(conn, job_url="route", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen="2026-08-04T10:00:00")
    make_job(conn, job_url="weak", verdict="PASS", fit_score=12,
             first_seen="2026-08-04T11:00:00")
    make_job(conn, job_url="due", app_status="applied", status_date="2026-07-20",
             outcome_status=None)
    make_job(conn, job_url="not-due", app_status="applied", status_date="2026-08-02",
             outcome_status=None)
    make_job(conn, job_url="answered", app_status="applied", status_date="2026-07-20",
             outcome_status="recruiter_screen", outcome_date="2026-07-25")
    make_job(conn, job_url="err", status="error", verdict=None, fit_score=None, bucket=None)
    make_job(conn, job_url="manual", status="needs_manual", verdict=None,
             fit_score=None, bucket=None)

    sections = workflow.action_center(
        conn, today=date(2026, 8, 5), fresh_days=3, followup_days=7, limit=10
    )
    by_id = {s["id"]: s for s in sections}
    assert set(by_id) == {"fresh_strong", "recruiter_route", "followups_due",
                          "needs_attention"}
    assert [r["job_url"] for r in by_id["fresh_strong"]["rows"]] == ["cold"]
    assert [r["job_url"] for r in by_id["recruiter_route"]["rows"]] == ["route"]
    assert [r["job_url"] for r in by_id["followups_due"]["rows"]] == ["due"]
    assert {r["job_url"] for r in by_id["needs_attention"]["rows"]} == {"err", "manual"}
    assert all(s["total"] == len(s["rows"]) for s in sections)


def test_followup_queue_advances_cadence_and_stops_after_two(conn):
    due = make_job(conn, job_url="due", app_status="applied", status_date="2026-07-01")
    waiting = make_job(conn, job_url="waiting", app_status="applied",
                       status_date="2026-07-01")
    cold = make_job(conn, job_url="cold-app", app_status="applied",
                    status_date="2026-07-01")
    chain.record_event(conn, waiting, EVENT_FOLLOWUP_SENT, "2026-08-01")
    chain.record_event(conn, cold, EVENT_FOLLOWUP_SENT, "2026-07-10")
    chain.record_event(conn, cold, EVENT_FOLLOWUP_SENT, "2026-07-20")

    page = workflow.query_action_page(
        conn, "followups_due", page=1, page_size=20, today=date(2026, 8, 5)
    )
    assert [r["job_url"] for r in page["rows"]] == [due["job_url"]]
    row = page["rows"][0]
    assert row["followup_count"] == 0
    assert row["next_followup_date"] == "2026-07-08"


def test_each_action_section_has_a_paged_view(conn):
    make_job(conn, job_url="fresh-a", fit_score=17, first_seen="2026-08-05T09:00:00")
    make_job(conn, job_url="fresh-b", fit_score=16, first_seen="2026-08-05T08:00:00")
    page = workflow.query_action_page(
        conn, "fresh_strong", page=1, page_size=1, today=date(2026, 8, 5)
    )
    assert page["total"] == 2 and page["pages"] == 2
    assert page["title"] == "Fresh strong matches"


def test_page_arguments_are_bounded(conn):
    make_job(conn)
    for page, page_size in ((0, 20), (1, 0), (1, workflow.MAX_PAGE_SIZE + 1)):
        try:
            workflow.query_job_page(conn, "backlog", page=page, page_size=page_size)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid paging arguments must be refused")
