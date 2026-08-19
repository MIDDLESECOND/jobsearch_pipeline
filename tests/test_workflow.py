"""Action/read-model queries for the triage UI.

These tests pin the new concern boundary: workflow.py selects and pages rows, while
app.py remains responsible for flattening them into the card payload.  The queries run
against synthetic fixtures only; no test opens the real jobs.db.
"""

from datetime import date, datetime, timezone
from typing import Any

import chain
from conftest import make_job
from states import EVENT_FOLLOWUP_SENT
import tasks
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


def test_today_and_applied_filters_use_the_card_inherited_chain_judgment(conn):
    make_job(
        conn, job_url="canonical", verdict="PASS", fit_score=17,
        first_seen="2026-08-01T09:00:00", app_status="applied",
        status_date="2026-08-04",
    )
    make_job(
        conn, job_url="relisting", repost_of="canonical", verdict=None,
        fit_score=None, status="repost_evaluated",
        first_seen="2026-08-05T09:00:00", app_status="applied",
        status_date="2026-08-05",
    )
    make_job(
        conn, job_url="other", verdict="RECRUITER_ONLY", fit_score=9,
        first_seen="2026-08-05T10:00:00", app_status="applied",
        status_date="2026-08-05",
    )
    filters = {"verdict": "PASS", "min_score": 10}

    today = workflow.query_job_page(
        conn, "today", for_date="2026-08-05", page=1, page_size=20,
        filters=filters, today=date(2026, 8, 5),
    )
    assert [row["job_url"] for row in today["rows"]] == ["relisting"]
    assert today["total"] == 1 and today["pages"] == 1

    applied = workflow.query_job_page(
        conn, "applied", page=1, page_size=1, filters=filters,
        today=date(2026, 8, 5),
    )
    assert [row["job_url"] for row in applied["rows"]] == ["relisting"]
    assert applied["total"] == 2 and applied["pages"] == 2


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
    assert set(by_id) == {"fresh_strong", "recruiter_route", "possible_duplicates",
                          "starred_roles", "upcoming_interviews", "interview_prep", "tasks_due", "followups_due",
                          "needs_attention"}
    assert [r["job_url"] for r in by_id["fresh_strong"]["rows"]] == ["cold"]
    assert [r["job_url"] for r in by_id["recruiter_route"]["rows"]] == ["route"]
    assert [r["job_url"] for r in by_id["followups_due"]["rows"]] == ["due"]
    assert [r["job_url"] for r in by_id["interview_prep"]["rows"]] == ["answered"]
    assert {r["job_url"] for r in by_id["needs_attention"]["rows"]} == {"err", "manual"}
    assert all(s["total"] == len(s.get("pairs", s["rows"])) for s in sections)


def test_upcoming_interviews_queue_is_chain_scoped_and_paged(conn):
    import interviews

    root = make_job(conn, job_url="root", app_status="applied", status_date="2026-08-01")
    make_job(conn, job_url="relist", repost_of="root", app_status="applied",
             status_date="2026-08-01")
    later = make_job(conn, job_url="later", app_status="applied", status_date="2026-08-01")
    old = make_job(conn, job_url="old", app_status="applied", status_date="2026-08-01")
    interviews.add_interview(
        conn, root, title="Soon", starts_at="2026-08-07T15:00:00+00:00",
        duration_minutes=60, mode="video")
    interviews.add_interview(
        conn, later, title="Later", starts_at="2026-08-08T15:00:00+00:00",
        duration_minutes=60, mode="phone")
    interviews.add_interview(
        conn, old, title="Too far", starts_at="2026-09-01T15:00:00+00:00",
        duration_minutes=60, mode="onsite")

    page = workflow.query_action_page(
        conn, "upcoming_interviews", page=1, page_size=1,
        now=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )
    assert page["title"] == "Upcoming interviews"
    assert page["total"] == 2 and page["pages"] == 2
    assert page["rows"][0]["job_url"] == "root"
    assert page["rows"][0]["next_interview_at"] == "2026-08-07T15:00:00+00:00"


def test_starred_roles_queue_is_chain_scoped_and_paged(conn):
    import watchlist

    root = make_job(conn, job_url="root", first_seen="2026-08-01T00:00:00")
    relist = make_job(conn, job_url="relist", repost_of="root")
    second = make_job(conn, job_url="second", first_seen="2026-08-02T00:00:00")
    watchlist.set_starred(
        conn, relist, True, expected_starred=False, expected_version=0,
    )
    watchlist.set_starred(
        conn, second, True, expected_starred=False, expected_version=0,
    )

    page = workflow.query_action_page(
        conn, "starred_roles", page=1, page_size=1,
    )
    assert page["title"] == "Starred roles"
    assert page["total"] == 2 and page["pages"] == 2
    assert page["rows"][0]["job_url"] == "second"
    assert page["rows"][0]["starred_at"]


def test_recruiter_route_requires_missing_contact_and_uses_own_window(conn):
    import outreach

    make_job(conn, job_url="needs-channel", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen="2026-08-04T10:00:00")
    make_job(conn, job_url="older-window", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen="2026-07-26T10:00:00")
    make_job(conn, job_url="contacted", verdict="RECRUITER_ONLY", fit_score=16,
             first_seen="2026-08-04T11:00:00")
    relist = make_job(conn, job_url="contacted-relist", repost_of="contacted")
    make_job(conn, job_url="below-bar", verdict="RECRUITER_ONLY", fit_score=14,
             first_seen="2026-08-04T12:00:00")
    make_job(conn, job_url="strong-pass", verdict="PASS", fit_score=17,
             first_seen="2026-08-04T13:00:00")
    # add_contact keys the row to the chain's canonical, so recording against a relisting
    # still clears the canonical's card.
    outreach.add_contact(conn, relist, name="Rex Cruiter", kind="recruiter")

    page = workflow.query_action_page(
        conn, "recruiter_route", page=1, page_size=10, today=date(2026, 8, 5),
    )
    assert page["title"] == "Route to a human"
    assert "contact" in page["description"]
    assert {r["job_url"] for r in page["rows"]} == {"needs-channel", "older-window"}

    # Queue exit is not a hidden decision: the contacted chain stays in the backlog.
    backlog = workflow.query_job_page(
        conn, "backlog", page=1, page_size=50, today=date(2026, 8, 5),
    )
    assert "contacted" in {r["job_url"] for r in backlog["rows"]}

    # The cadence is tunable per call.
    narrow = workflow.query_action_page(
        conn, "recruiter_route", page=1, page_size=10, today=date(2026, 8, 5),
        route_days=3, route_min_score=14,
    )
    assert {r["job_url"] for r in narrow["rows"]} == {"needs-channel", "below-bar"}


def test_recruiter_route_reads_contacts_through_the_current_chain_root(conn):
    """A contact written before a merge must still clear the chain it now belongs to.

    outreach.add_contact keys the row to the canonical AT WRITE TIME, so a later merge can
    leave that URL sitting on a repost.  Reading job_contacts.job_url directly would leave
    both members in the queue; the CTE has to map through each contact's CURRENT root.
    """
    import outreach

    make_job(conn, job_url="earlier", company="Acme", location="New York, NY",
             title="Solutions Architect", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen="2026-08-04T09:00:00", source="linkedin")
    later = make_job(conn, job_url="later", company="Acme", location="Manhattan, NY",
                     title="Solutions Architect", verdict="RECRUITER_ONLY", fit_score=15,
                     first_seen="2026-08-04T10:00:00", source="adzuna")
    outreach.add_contact(conn, later, name="Rex Cruiter", kind="recruiter")
    assert conn.execute("SELECT job_url FROM job_contacts").fetchone()[0] == "later"

    page = workflow.query_action_page(
        conn, "recruiter_route", page=1, page_size=10, today=date(2026, 8, 5))
    assert [r["job_url"] for r in page["rows"]] == ["earlier"]

    # The merge makes "later" a repost, so its contact row now sits on a non-root URL.
    plan, error = chain.dupe_resolve(conn, "later", "earlier")
    assert error is None
    chain.dupe_commit(conn, plan)

    page = workflow.query_action_page(
        conn, "recruiter_route", page=1, page_size=10, today=date(2026, 8, 5))
    assert page["total"] == 0 and page["rows"] == []


def test_has_contact_filter_is_backlog_only_and_boolean(conn):
    import outreach
    import pytest

    plain = make_job(conn, job_url="plain", fit_score=15,
                     first_seen="2026-08-04T09:00:00")
    known = make_job(conn, job_url="known", company="Other Co", fit_score=15,
                     first_seen="2026-08-04T10:00:00")
    outreach.add_contact(conn, known, name="Rex Cruiter", kind="recruiter")
    assert plain["job_url"] == "plain"

    for wanted, expected in ((True, {"known"}), (False, {"plain"})):
        page = workflow.query_job_page(
            conn, "backlog", page=1, page_size=50, today=date(2026, 8, 5),
            filters={"has_contact": wanted})
        assert {r["job_url"] for r in page["rows"]} == expected

    with pytest.raises(ValueError):
        workflow.query_job_page(conn, "backlog", filters={"has_contact": 1})
    with pytest.raises(ValueError):
        workflow.query_job_page(conn, "applied", filters={"has_contact": True})


def test_attention_queue_surfaces_flagged_verdicts_without_re_routing_them(conn):
    """A self-contradicting rejection has no other surface: GATE_FAIL rows enter no queue
    and no backlog listing, so without this the diagnostic is report-only."""
    flagged = make_job(conn, job_url="flagged", verdict="GATE_FAIL", failed_gate=None,
                       fit_score=None, bucket=None,
                       eval_issues="gate-results-inconsistent")
    make_job(conn, job_url="clean-fail", verdict="GATE_FAIL", failed_gate="years_floor",
             fit_score=None, bucket=None)
    make_job(conn, job_url="broken", status="error", verdict=None, fit_score=None,
             bucket=None)
    make_job(conn, job_url="decided", verdict="GATE_FAIL", failed_gate=None, fit_score=None,
             bucket=None, eval_issues="gate-results-inconsistent",
             app_status="passed", status_date="2026-08-05")
    make_job(conn, job_url="overridden", verdict="GATE_FAIL", fit_score=None, bucket=None,
             eval_issues="gate-results-inconsistent", filter_source="rule:x")

    page = workflow.query_action_page(conn, "needs_attention", page=1, page_size=10)
    assert {r["job_url"] for r in page["rows"]} == {"flagged", "broken"}

    # Review, not re-routing: the stored verdict is untouched, and the backlog still
    # excludes the row because its verdict is what it always was.
    assert conn.execute("SELECT verdict FROM jobs WHERE job_url='flagged'"
                        ).fetchone()[0] == "GATE_FAIL"
    backlog = workflow.query_job_page(conn, "backlog", page=1, page_size=50,
                                      today=date(2026, 8, 5))
    assert "flagged" not in {r["job_url"] for r in backlog["rows"]}

    # Deciding the role is the exit.
    chain.mark_posting(conn, flagged, "passed", "2026-08-05")
    page = workflow.query_action_page(conn, "needs_attention", page=1, page_size=10)
    assert {r["job_url"] for r in page["rows"]} == {"broken"}


def test_route_cadence_values_are_validated(conn):
    """A half-written config key reads as None; it must fail loudly, not drop the bar."""
    import pytest

    bad_overrides: tuple[dict[str, Any], ...] = (
        {"route_min_score": None}, {"route_min_score": "15"},
        {"route_min_score": 19}, {"route_days": None},
        {"route_days": 14.5}, {"route_days": 0})
    for bad in bad_overrides:
        with pytest.raises(ValueError):
            workflow.query_action_page(conn, "recruiter_route", **bad)


def test_recruiter_route_bar_defaults_mirror_the_states_constant():
    """states.py documents RECRUITER_ROUTE_MIN_FIT as "workflow's recruiter_route
    default", but the defaults are LITERALS — two signature defaults here and the
    config-absent fallback inside app._route_cadence — and nothing tied them to the
    constant: raise the bar in states.py and every suite stays green while the Action
    Center keeps queueing at the old line. Pin all three to the constant (whether
    production should import it instead of repeating 15 is a separate decision)."""
    import inspect

    import app
    from states import RECRUITER_ROUTE_MIN_FIT

    for fn in (workflow.query_action_page, workflow.action_center):
        assert (inspect.signature(fn).parameters["route_min_score"].default
                == RECRUITER_ROUTE_MIN_FIT), fn.__name__
    assert (app._route_cadence({"settings": {}})["route_min_score"]
            == RECRUITER_ROUTE_MIN_FIT)


def test_action_center_includes_pageable_possible_duplicates(conn):
    today = date.today().isoformat()
    make_job(conn, job_url="li", company="Same Co", title="Data Analyst",
             source="linkedin", first_seen=today + "T09:00:00")
    make_job(conn, job_url="adz", company="Same Co", title="Data Analyst",
             source="adzuna", location="Manhattan, NY",
             first_seen=today + "T10:00:00")

    section = workflow.query_action_page(
        conn, "possible_duplicates", page=1, page_size=1,
    )

    assert section["title"] == "Possible duplicates"
    assert section["total"] == 1 and len(section["pairs"]) == 1
    assert section["rows"] == []
    assert section["dismissed_total"] == 0


def test_tasks_due_queue_is_chain_scoped_paged_and_closes_immediately(conn):
    root = make_job(conn, job_url="root")
    relisting = make_job(conn, job_url="relisting", repost_of="root")
    later = make_job(conn, job_url="later")
    second = make_job(conn, job_url="second")
    due = tasks.add_task(
        conn, relisting, title="Prepare questions", due_date="2026-08-04")["task"]
    tasks.add_task(conn, root, title="Send materials", due_date="2026-08-05")
    tasks.add_task(conn, later, title="Future task", due_date="2026-08-06")
    tasks.add_task(conn, second, title="Second role", due_date="2026-08-05")

    page = workflow.query_action_page(
        conn, "tasks_due", page=1, page_size=1, today=date(2026, 8, 5))
    assert page["total"] == 2 and page["pages"] == 2
    assert page["rows"][0]["job_url"] == "root"
    assert page["rows"][0]["next_task_due"] == "2026-08-04"

    tasks.change_task(
        conn, root, due["id"], "complete", expected_version=due["version"])
    still_due = workflow.query_action_page(
        conn, "tasks_due", page=1, page_size=20, today=date(2026, 8, 5))
    assert [row["job_url"] for row in still_due["rows"]] == ["root", "second"]
    remaining = tasks.chain_tasks(conn, root)[0]
    tasks.change_task(
        conn, root, remaining["id"], "snooze",
        expected_version=remaining["version"], due_date="2026-08-08",
    )
    after = workflow.query_action_page(
        conn, "tasks_due", page=1, page_size=20, today=date(2026, 8, 5))
    assert [row["job_url"] for row in after["rows"]] == ["second"]


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


def test_interview_prep_queue_is_recent_chain_scoped_and_terminal_outcomes_leave(conn):
    make_job(conn, job_url="recent", app_status="applied", status_date="2026-07-20",
             outcome_status="interview", outcome_date="2026-08-04")
    make_job(conn, job_url="recent-relist", repost_of="recent", app_status="applied",
             status_date="2026-07-20", outcome_status="interview", outcome_date="2026-08-04")
    make_job(conn, job_url="old", app_status="applied", status_date="2026-06-01",
             outcome_status="recruiter_screen", outcome_date="2026-07-01")
    make_job(conn, job_url="finished", app_status="applied", status_date="2026-07-20",
             outcome_status="offer", outcome_date="2026-08-04")

    page = workflow.query_action_page(
        conn, "interview_prep", page=1, page_size=20, today=date(2026, 8, 5)
    )
    assert [row["job_url"] for row in page["rows"]] == ["recent"]


def test_page_arguments_are_bounded(conn):
    make_job(conn)
    for page, page_size in ((0, 20), (1, 0), (1, workflow.MAX_PAGE_SIZE + 1)):
        try:
            workflow.query_job_page(conn, "backlog", page=page, page_size=page_size)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid paging arguments must be refused")
