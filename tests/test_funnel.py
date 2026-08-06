"""Chain-scoped application-funnel aggregation."""

from datetime import date
from typing import Any, cast

import chain
import funnel
from conftest import make_job
from states import (EVENT_GHOSTED, EVENT_INTERVIEW, EVENT_OFFER,
                    EVENT_REJECTED_BY_EMPLOYER, EVENT_WITHDREW)


TODAY = date(2026, 8, 5)


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_funnel_counts_chains_once_and_infers_reached_stages(conn):
    root = make_job(
        conn, job_url="direct-root", company="Direct Co", app_status="applied",
        status_date="2026-07-10", channel="direct", resume_variant="ai-v2",
    )
    relist = make_job(
        conn, job_url="direct-relist", company="Direct Co", repost_of=root["job_url"],
        app_status="applied", status_date="2026-07-10", channel="direct",
        resume_variant="ai-v2",
    )
    # Simulate an event written before this row became a former canonical: history stays
    # put, but the funnel must map it through the row's current repost_of.
    ok, _, _, _ = chain.record_event(conn, relist, EVENT_INTERVIEW, "2026-07-20")
    assert ok
    conn.execute("UPDATE app_events SET job_url='direct-relist'")
    conn.commit()

    agency = make_job(
        conn, job_url="agency", company="Agency Co", app_status="applied",
        status_date="2026-07-15", channel="agency",
    )
    assert chain.record_event(conn, agency, EVENT_OFFER, "2026-07-30")[0]

    referral = make_job(
        conn, job_url="referral", company="Referral Co", app_status="applied",
        status_date="2026-07-18", channel="referral",
    )
    assert chain.record_event(conn, referral, EVENT_GHOSTED, "2026-08-01")[0]
    make_job(
        conn, job_url="unknown", company="Unknown Co", app_status="applied",
        status_date="2026-08-01", channel=None,
    )
    make_job(
        conn, job_url="old", company="Old Co", app_status="applied",
        status_date="2026-01-01", channel="direct",
    )

    got = funnel.funnel_snapshot(conn, days=90, today=TODAY)
    stages = _by_id(got["stages"])
    assert stages["applied"]["count"] == 4  # repost member is not a second application
    assert stages["response"]["count"] == 2  # ghosted is not an employer response
    assert stages["interview"]["count"] == 2  # offer implies interview
    assert stages["offer"]["count"] == 1
    assert stages["response"]["rate_from_applied"] == 50.0
    assert stages["offer"]["rate_from_previous"] == 50.0

    channels = _by_id(got["by_channel"])
    assert channels["direct"]["applied"] == 1
    assert channels["direct"]["interviews"] == 1
    assert channels["agency"]["offer_rate"] == 100.0
    assert channels["referral"]["response_rate"] == 0.0
    assert channels["unknown"]["applied"] == 1
    assert got["quality"] == {
        "channel_recorded": 3, "resume_recorded": 1, "applications": 4,
    }

    outcomes = _by_id(got["outcomes"])
    assert outcomes["interview"]["count"] == 1
    assert outcomes["offer"]["count"] == 1
    assert outcomes["ghosted"]["count"] == 1
    assert outcomes["no_outcome"]["count"] == 1


def test_funnel_all_time_and_calendar_window(conn):
    make_job(conn, job_url="today", app_status="applied", status_date="2026-08-05")
    make_job(conn, job_url="boundary", app_status="applied", status_date="2026-08-03")
    make_job(conn, job_url="outside", app_status="applied", status_date="2026-08-02")
    make_job(conn, job_url="future", app_status="applied", status_date="2026-08-06")
    make_job(conn, job_url="undone", app_status=None, status_date=None)

    last_three = funnel.funnel_snapshot(conn, days=3, today=TODAY)
    assert last_three["quality"]["applications"] == 2
    assert last_three["range"]["start"] == "2026-08-03"
    assert last_three["range"]["end"] == "2026-08-05"

    all_time = funnel.funnel_snapshot(conn, days=None, today=TODAY)
    assert all_time["quality"]["applications"] == 3
    assert all_time["range"]["start"] is None


def test_response_semantics_include_rejection_but_not_ghosted_or_withdrew(conn):
    rejected = make_job(conn, job_url="rejected", app_status="applied",
                        status_date="2026-08-01")
    ghosted = make_job(conn, job_url="ghosted", app_status="applied",
                       status_date="2026-08-01")
    withdrew = make_job(conn, job_url="withdrew", app_status="applied",
                        status_date="2026-08-01")
    assert chain.record_event(
        conn, rejected, EVENT_REJECTED_BY_EMPLOYER, "2026-08-02"
    )[0]
    assert chain.record_event(conn, ghosted, EVENT_GHOSTED, "2026-08-02")[0]
    assert chain.record_event(conn, withdrew, EVENT_WITHDREW, "2026-08-02")[0]

    stages = _by_id(funnel.funnel_snapshot(conn, today=TODAY)["stages"])
    assert stages["applied"]["count"] == 3
    assert stages["response"]["count"] == 1
    assert stages["interview"]["count"] == 0


def test_funnel_empty_rates_and_validation(conn):
    got = funnel.funnel_snapshot(conn, today=TODAY)
    assert got["quality"]["applications"] == 0
    assert all(stage["rate_from_applied"] is None for stage in got["stages"])

    for bad in (True, 0, 3651, "90"):
        try:
            funnel.funnel_snapshot(conn, days=cast(Any, bad), today=TODAY)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid days={bad!r}")


def test_parse_funnel_days():
    assert funnel.parse_funnel_days(" 90 ") == 90
    assert funnel.parse_funnel_days("ALL") is None
    for bad in ("", "0", "3651", "recent"):
        try:
            funnel.parse_funnel_days(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid days={bad!r}")
