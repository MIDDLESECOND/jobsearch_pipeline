"""Recency triage: posting_age labels, the recency_sort_key two-band order, and their wiring
into the report and the web UI. All pure-function tests inject `now`; the integration tests use
the conn fixture + make_job (synthetic rows, never the real jobs.db)."""

from datetime import datetime

import pipeline
from conftest import make_job

NOW = datetime(2026, 7, 4, 9, 0, 0)


# ------------------------------------------------------------------ posting_age

def test_age_full_timestamp_hours():
    assert pipeline.posting_age("2026-07-04T06:00:00", "2026-07-04T08:00:00", now=NOW) == "3h ago"


def test_age_full_timestamp_days():
    assert pipeline.posting_age("2026-07-02T09:00:00", "2026-07-04T08:00:00", now=NOW) == "2d ago"


def test_age_months():
    assert pipeline.posting_age("2026-01-04T09:00:00", "2026-07-04T08:00:00", now=NOW) == "6mo ago"


def test_age_under_one_hour_is_just_now():
    assert pipeline.posting_age("2026-07-04T08:30:00", "2026-07-04T08:31:00", now=NOW) == "just now"


def test_age_date_only_today_falls_back_to_seen_hedged():
    # Date-only date_posted from the same day we fetched it: first_seen stands in — but it is a
    # LOWER BOUND, not the posting time, so the label must carry the "seen" hedge.
    assert pipeline.posting_age("2026-07-04", "2026-07-04T06:00:00", now=NOW) == "seen 3h ago"


def test_age_date_only_day_ahead_of_seen_is_hedged_not_future():
    # A board publishing UTC calendar dates can be a day ahead of local time; that must fall
    # back to first_seen (hedged), not become a future-midnight instant.
    assert pipeline.posting_age("2026-07-05", "2026-07-04T21:05:00",
                                now=datetime(2026, 7, 4, 21, 10)) == "seen just now"


def test_age_date_only_past_stays_day_granularity():
    # Seen today but posted (per the source) two days ago: day granularity, never fake hours.
    assert pipeline.posting_age("2026-07-02", "2026-07-04T08:00:00", now=NOW) == "2d ago"


def test_age_date_only_ats_backlog_months_old():
    assert pipeline.posting_age("2026-04-01", "2026-07-04T08:00:00", now=NOW) == "3mo ago"


def test_age_empty_falls_back_to_seen():
    assert pipeline.posting_age("", "2026-07-04T06:00:00", now=NOW) == "seen 3h ago"


def test_age_garbage_falls_back_to_seen():
    assert pipeline.posting_age("not-a-date", "2026-07-04T06:00:00", now=NOW) == "seen 3h ago"


def test_age_adzuna_utc_z_suffix_parses():
    # Adzuna `created` is UTC with a Z; must parse and convert without crashing. The exact hour
    # count depends on the local UTC offset, so only assert the shape.
    label = pipeline.posting_age("2026-07-03T07:00:00Z", "2026-07-04T08:00:00", now=NOW)
    assert label.endswith("ago") and not label.startswith("seen")


def test_age_future_date_clamps_to_just_now():
    assert pipeline.posting_age("2026-07-04T11:00:00", "2026-07-04T08:00:00", now=NOW) == "just now"


def test_age_nothing_usable_is_empty():
    assert pipeline.posting_age("", "", now=NOW) == ""


# ------------------------------------------------------------- recency_sort_key

def _row(fit, date_posted, first_seen):
    return {"fit_score": fit, "date_posted": date_posted, "first_seen": first_seen}


def test_above_line_fresh_beats_above_line_stronger_but_older():
    fresh_ok = _row(11, "2026-07-04T08:00:00", "2026-07-04T08:10:00")
    old_strong = _row(17, "2026-07-04T02:00:00", "2026-07-04T02:10:00")
    ordered = sorted([old_strong, fresh_ok], key=pipeline.recency_sort_key)
    assert ordered == [fresh_ok, old_strong]


def test_below_line_never_outranks_above_line():
    fresh_weak = _row(pipeline.APPLY_LINE - 1, "2026-07-04T08:59:00", "2026-07-04T08:59:00")
    old_ok = _row(pipeline.APPLY_LINE, "2026-06-01T00:00:00", "2026-06-01T00:00:00")
    ordered = sorted([fresh_weak, old_ok], key=pipeline.recency_sort_key)
    assert ordered == [old_ok, fresh_weak]


def test_below_line_ordered_by_fit_then_recency():
    weak_old = _row(8, "2026-07-01T00:00:00", "2026-07-01T00:00:00")
    weaker_fresh = _row(5, "2026-07-04T08:00:00", "2026-07-04T08:00:00")
    weak_fresh = _row(8, "2026-07-04T08:00:00", "2026-07-04T08:00:00")
    ordered = sorted([weaker_fresh, weak_old, weak_fresh], key=pipeline.recency_sort_key)
    assert ordered == [weak_fresh, weak_old, weaker_fresh]


def test_ats_backlog_ranks_by_posted_date_not_seen_date():
    # Seen five minutes ago, but the board says it was posted in April — must sort below a
    # posting actually made yesterday.
    backlog = _row(15, "2026-04-01", "2026-07-04T08:55:00")
    yesterday = _row(12, "2026-07-03", "2026-07-03T10:00:00")
    ordered = sorted([backlog, yesterday], key=pipeline.recency_sort_key)
    assert ordered == [yesterday, backlog]


def test_no_timestamps_sorts_last_in_band():
    dated = _row(12, "", "2026-06-01T00:00:00")
    undated = _row(12, "", "")
    ordered = sorted([undated, dated], key=pipeline.recency_sort_key)
    assert ordered == [dated, undated]


def test_far_future_date_neither_crashes_nor_pins_top():
    # Windows mktime raises OSError past ~year 3000, and a clamped-but-accepted future date
    # would permanently pin the row to the top of the apply band labeled "just now". A
    # placeholder date must instead degrade to the honest first_seen fallback: no crash, and
    # ranked by when we actually saw it — never above a genuinely fresher posting.
    day_only = _row(12, "9999-12-31", "2026-07-04T08:00:00")
    full_ts = _row(12, "9999-12-31T00:00:00", "2026-07-04T08:00:00")
    aware = _row(12, "9999-12-31T00:00:00Z", "2026-07-04T08:00:00")
    fresh = _row(12, "2026-07-04T09:30:00", "2026-07-04T09:35:00")
    ordered = sorted([day_only, full_ts, aware, fresh], key=pipeline.recency_sort_key)
    assert ordered[0] is fresh
    # And the label hedges rather than claiming the garbage date as real:
    assert pipeline.posting_age("9999-12-31", "2026-07-04T08:00:00", now=NOW) == "seen 1h ago"


def test_pre_2000_date_treated_as_garbage():
    # A zeroed epoch or corrupted value stored as a 1970 date must not crash .timestamp()
    # (east-of-UTC Windows) nor render as a confident "~679mo ago".
    key = pipeline.recency_sort_key(_row(12, "1970-01-01T09:00:00", "2026-07-04T08:00:00"))
    assert key[0] == 0  # above the line, no crash
    assert pipeline.posting_age("1970-01-01T09:00:00", "2026-07-04T08:00:00", now=NOW) == "seen 1h ago"


def test_day_ahead_date_sorts_by_first_seen_not_future_midnight():
    # UTC-ahead board date must rank by its (honest) first_seen, identically to a row with no
    # posting date at all — not get pinned to the top with a future-midnight instant.
    utc_ahead = _row(12, "2026-07-05", "2026-07-04T21:05:00")
    no_date = _row(12, "", "2026-07-04T21:05:00")
    assert pipeline.recency_sort_key(utc_ahead) == pipeline.recency_sort_key(no_date)


def test_none_fit_score_treated_as_zero():
    key = pipeline.recency_sort_key(_row(None, "", ""))
    assert key[0] == 1  # below the line, no crash


# ------------------------------------------------------- report / UI integration

def test_report_orders_pass_section_freshest_first(tmp_path, conn):
    d = "2026-07-04"
    make_job(conn, job_url="u-old-strong", title="Old Strong", fit_score=17,
             first_seen=f"{d}T02:10:00", date_posted=f"{d}T02:00:00")
    make_job(conn, job_url="u-fresh-ok", title="Fresh Acceptable", fit_score=11,
             first_seen=f"{d}T08:10:00", date_posted=f"{d}T08:00:00")
    make_job(conn, job_url="u-weak-fresh", title="Weak Fresh", fit_score=6,
             first_seen=f"{d}T08:30:00", date_posted=f"{d}T08:20:00")
    cfg = {"settings": {"reports_dir": str(tmp_path)}}
    pipeline.generate_report(cfg, conn, for_date=d)
    text = (tmp_path / f"report_{d}.md").read_text(encoding="utf-8")
    fresh, old, weak = (text.index("### Fresh Acceptable"), text.index("### Old Strong"),
                        text.index("### Weak Fresh"))
    assert fresh < old < weak
    assert "🕐" in text  # age tag rendered


def test_past_report_ages_anchor_to_report_date_not_wall_clock(tmp_path, conn):
    # Rebuilding a past day's report must reproduce it: labels anchor to that date's end,
    # not to whenever the rebuild happens to run.
    d = "2026-07-01"  # fixed past date; the wall clock at test time is irrelevant
    make_job(conn, job_url="u-anchored", title="Anchored Role", fit_score=15,
             first_seen=f"{d}T23:00:00", date_posted=f"{d}T22:00:00")
    cfg = {"settings": {"reports_dir": str(tmp_path)}}
    pipeline.generate_report(cfg, conn, for_date=d)
    text = (tmp_path / f"report_{d}.md").read_text(encoding="utf-8")
    assert "🕐 1h ago" in text  # 22:00 posted vs the 23:59:59 anchor — not "Nd ago"


def test_report_malformed_date_fails_fast(tmp_path, conn):
    # generate_report owns its date contract: a malformed for_date raises immediately at
    # entry (clear ValueError, no work done, no partial file) — it must not depend on the
    # CLI's validation, and must never crash mid-render at the age-anchor line.
    import pytest
    cfg = {"settings": {"reports_dir": str(tmp_path)}}
    with pytest.raises(ValueError):
        pipeline.generate_report(cfg, conn, for_date="2026-7-4")
    assert not list(tmp_path.glob("report_*.md"))  # no report written


def test_ui_today_view_two_band_order_and_age_label(conn):
    import app as webapp
    d = "2026-07-04"
    make_job(conn, job_url="u1", fit_score=17, first_seen=f"{d}T02:10:00",
             date_posted=f"{d}T02:00:00")
    make_job(conn, job_url="u2", fit_score=11, first_seen=f"{d}T08:10:00",
             date_posted=f"{d}T08:00:00")
    make_job(conn, job_url="u3", fit_score=6, first_seen=f"{d}T08:30:00",
             date_posted=f"{d}T08:20:00")
    jobs = webapp.jobs_for_view(conn, "today", d, cap=12000)
    assert [j["job_url"] for j in jobs] == ["u2", "u1", "u3"]
    assert all("age_label" in j and "date_posted" in j for j in jobs)
