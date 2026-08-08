"""Conservative cross-source duplicate suggestions and dismissal state."""

from datetime import date
import sqlite3

import pytest

import core
import dupe_candidates
from conftest import make_job


TODAY = date(2026, 8, 6)


def _pair_urls(pair):
    return {pair["left"]["job_url"], pair["right"]["job_url"]}


def test_candidate_page_only_suggests_recent_cross_source_exact_company_title(conn):
    make_job(conn, job_url="linkedin", company="Acme, Inc.", title="Sr Data Analyst",
             location="New York, NY", source="linkedin",
             first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="adzuna", company="Acme", title="Senior Data Analyst",
             location="Grand Central, Manhattan", source="adzuna",
             first_seen="2026-08-03T09:00:00")
    make_job(conn, job_url="same-source-a", company="Solo", title="Senior Data Analyst",
             location="Albany, NY", source="linkedin",
             first_seen="2026-08-02T09:00:00")
    make_job(conn, job_url="same-source-b", company="Solo", title="Senior Data Analyst",
             location="Buffalo, NY", source="linkedin",
             first_seen="2026-08-03T09:00:00")
    make_job(conn, job_url="different-title", company="Acme", title="Data Engineer",
             source="greenhouse", first_seen="2026-08-02T09:00:00")
    make_job(conn, job_url="too-old", company="Acme", title="Senior Data Analyst",
             source="ashby", first_seen="2026-02-01T09:00:00")

    page = dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY,
    )

    assert page["total"] == 1
    pair = page["pairs"][0]
    assert _pair_urls(pair) == {"linkedin", "adzuna"}
    expected_side_columns = {
        "job_url", "title", "company", "location", "source",
        "first_seen", "date_posted", "description",
    }
    assert set(pair["left"].keys()) == expected_side_columns
    assert set(pair["right"].keys()) == expected_side_columns
    assert pair["same_location"] is False
    assert pair["first_seen_gap_days"] == 2
    assert page["dismissed_total"] == 0


def test_candidate_page_collapses_physical_matches_to_one_current_chain_pair(conn):
    make_job(conn, job_url="left-root", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="left-relist", company="Acme", title="Analyst",
             source="linkedin", repost_of="left-root",
             first_seen="2026-08-04T09:00:00")
    make_job(conn, job_url="right-root", company="Acme", title="Analyst",
             source="adzuna", first_seen="2026-08-03T09:00:00")

    page = dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY,
    )

    assert page["total"] == 1
    pair = page["pairs"][0]
    assert pair["left_root"] == "left-root"
    assert pair["right_root"] == "right-root"
    assert _pair_urls(pair) == {"left-relist", "right-root"}


def test_mass_posted_company_title_key_is_dropped_whole_and_counted(conn):
    """One requisition posted across many cities must not bury real suggestions.

    The blocking key has no location, so a mass-posting employer yields a full
    LinkedIn x Adzuna cross product under a single key.  Past MAX_BUCKET_PAIRS the key
    carries no duplication signal, so it is dropped entirely -- and reported, never
    silently trimmed.
    """
    # 2 x 2 = 4 cross-source pairs: one over the cap, so the whole key goes.
    for i, (source, city) in enumerate([("linkedin", "Chicago, IL"),
                                        ("linkedin", "Atlanta, GA"),
                                        ("adzuna", "Chicago"),
                                        ("adzuna", "Atlanta")]):
        make_job(conn, job_url=f"mass-{i}", company="MassCo", title="Dynamics Consultant",
                 location=city, source=source, first_seen="2026-08-02T09:00:00")
    # 3 x 1 = 3 pairs: exactly at the cap, so the key is kept.
    for i, (source, city) in enumerate([("linkedin", "Boston, MA"),
                                        ("linkedin", "Austin, TX"),
                                        ("linkedin", "Denver, CO"),
                                        ("adzuna", "Boston")]):
        make_job(conn, job_url=f"small-{i}", company="SmallCo", title="Data Analyst",
                 location=city, source=source, first_seen="2026-08-03T09:00:00")

    page = dupe_candidates.query_candidate_page(conn, page=1, page_size=50, today=TODAY)

    assert page["total"] == 3
    assert all(pair["left_root"].startswith("small-") for pair in page["pairs"])
    assert page["suppressed_keys"] == 1
    assert page["suppressed_pairs"] == 4


def test_suppressed_key_pairs_are_not_confirmable_through_the_queue(conn):
    """The queue's eligibility gate and its listing must agree on what was dropped."""
    for i, (source, city) in enumerate([("linkedin", "Chicago, IL"),
                                        ("linkedin", "Atlanta, GA"),
                                        ("adzuna", "Chicago"),
                                        ("adzuna", "Atlanta")]):
        make_job(conn, job_url=f"mass-{i}", company="MassCo", title="Dynamics Consultant",
                 location=city, source=source, first_seen="2026-08-02T09:00:00")

    with pytest.raises(ValueError, match="no longer an eligible duplicate suggestion"):
        dupe_candidates.set_candidate_dismissed(
            conn, "mass-0", "mass-2", True, expected_roots=["mass-0", "mass-2"],
            expected_dismissed=False, expected_review_version=0, today=TODAY,
        )


def test_candidate_window_includes_45_day_gap_but_excludes_46_days_and_future(conn):
    make_job(conn, job_url="anchor", company="Boundary", title="Analyst",
             source="linkedin", first_seen="2026-06-22T09:00:00")
    make_job(conn, job_url="day-45", company="Boundary", title="Analyst",
             source="adzuna", first_seen="2026-08-06T09:00:00")
    make_job(conn, job_url="old-46", company="Outside", title="Analyst",
             source="linkedin", first_seen="2026-06-21T09:00:00")
    make_job(conn, job_url="new-46", company="Outside", title="Analyst",
             source="ashby", first_seen="2026-08-06T09:00:00")
    make_job(conn, job_url="future", company="Future", title="Analyst",
             source="linkedin", first_seen="2026-08-07T09:00:00")
    make_job(conn, job_url="future-other", company="Future", title="Analyst",
             source="adzuna", first_seen="2026-08-07T10:00:00")

    page = dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY,
    )

    assert page["total"] == 1
    assert _pair_urls(page["pairs"][0]) == {"anchor", "day-45"}


def test_dismiss_and_restore_are_persistent_and_transaction_owned(conn):
    make_job(conn, job_url="left", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="right", company="Acme", title="Analyst",
             source="adzuna", first_seen="2026-08-02T09:00:00")

    result = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", True, expected_roots=["left", "right"],
        expected_dismissed=False, expected_review_version=0, today=TODAY,
    )
    assert result["dismissed"] is True
    active = dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY,
    )
    ignored = dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY, dismissed=True,
    )
    assert active["pairs"] == [] and active["dismissed_total"] == 1
    assert _pair_urls(ignored["pairs"][0]) == {"left", "right"}

    restored = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", False, expected_roots=["left", "right"],
        expected_dismissed=True, expected_review_version=1, today=TODAY,
    )
    assert restored["dismissed"] is False
    assert dupe_candidates.query_candidate_page(
        conn, page=1, page_size=20, today=TODAY,
    )["total"] == 1

    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="clean database connection"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", True, expected_roots=["left", "right"],
            expected_dismissed=False, expected_review_version=2, today=TODAY,
        )
    assert conn.in_transaction
    conn.rollback()


def test_dismiss_refreshes_chain_membership_and_refuses_stale_or_ineligible_pair(conn):
    make_job(conn, job_url="left", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="right", company="Acme", title="Analyst",
             source="adzuna", first_seen="2026-08-02T09:00:00")
    conn.execute("UPDATE jobs SET repost_of='left' WHERE job_url='right'")
    conn.commit()

    with pytest.raises(ValueError, match="chains changed since preview"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", True, expected_roots=["left", "right"],
            expected_dismissed=False, expected_review_version=0, today=TODAY,
        )

    make_job(conn, job_url="other", company="Other", title="Engineer",
             source="ashby", first_seen="2026-08-02T09:00:00")
    with pytest.raises(ValueError, match="no longer an eligible duplicate suggestion"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "other", True, expected_roots=["left", "other"],
            expected_dismissed=False, expected_review_version=0, today=TODAY,
        )


def test_dismiss_refuses_stale_review_state_and_changed_preview_roots(conn):
    make_job(conn, job_url="left", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="right", company="Acme", title="Analyst",
             source="adzuna", first_seen="2026-08-02T09:00:00")
    make_job(conn, job_url="new-root", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-07-01T09:00:00")

    dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", True, expected_roots=["left", "right"],
        expected_dismissed=False, expected_review_version=0, today=TODAY,
    )
    with pytest.raises(ValueError, match="review changed; refresh and retry"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", False, expected_roots=["left", "right"],
            expected_dismissed=False, expected_review_version=0, today=TODAY,
        )

    dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", False, expected_roots=["left", "right"],
        expected_dismissed=True, expected_review_version=1, today=TODAY,
    )
    conn.execute("UPDATE jobs SET repost_of='new-root' WHERE job_url='left'")
    conn.commit()
    with pytest.raises(ValueError, match="chains changed since preview"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", True, expected_roots=["left", "right"],
            expected_dismissed=False, expected_review_version=2, today=TODAY,
        )


def test_review_version_rejects_both_directions_of_aba(conn):
    make_job(conn, job_url="left", company="Acme", title="Analyst",
             source="linkedin", first_seen="2026-08-01T09:00:00")
    make_job(conn, job_url="right", company="Acme", title="Analyst",
             source="adzuna", first_seen="2026-08-02T09:00:00")
    roots = ["left", "right"]

    first_dismiss = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", True, expected_roots=roots,
        expected_dismissed=False, expected_review_version=0, today=TODAY,
    )
    first_restore = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", False, expected_roots=roots,
        expected_dismissed=True, expected_review_version=first_dismiss["review_version"],
        today=TODAY,
    )
    second_dismiss = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", True, expected_roots=roots,
        expected_dismissed=False, expected_review_version=first_restore["review_version"],
        today=TODAY,
    )
    with pytest.raises(ValueError, match="review changed; refresh and retry"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", False, expected_roots=roots,
            expected_dismissed=True,
            expected_review_version=first_dismiss["review_version"], today=TODAY,
        )

    second_restore = dupe_candidates.set_candidate_dismissed(
        conn, "left", "right", False, expected_roots=roots,
        expected_dismissed=True, expected_review_version=second_dismiss["review_version"],
        today=TODAY,
    )
    with pytest.raises(ValueError, match="review changed; refresh and retry"):
        dupe_candidates.set_candidate_dismissed(
            conn, "left", "right", True, expected_roots=roots,
            expected_dismissed=False,
            expected_review_version=first_restore["review_version"], today=TODAY,
        )
    assert second_restore["review_version"] == 4


def test_schema_contains_dismissal_table(conn):
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(dupe_candidate_dismissals)"
    )}
    assert columns == {
        "left_root", "right_root", "dismissed_at", "dismissed", "version",
    }


def test_legacy_dismissal_rows_migrate_to_version_one():
    legacy = sqlite3.connect(":memory:")
    try:
        legacy.execute(
            "CREATE TABLE dupe_candidate_dismissals ("
            "left_root TEXT NOT NULL,right_root TEXT NOT NULL,dismissed_at TEXT NOT NULL,"
            "PRIMARY KEY(left_root,right_root))"
        )
        legacy.execute(
            "INSERT INTO dupe_candidate_dismissals VALUES ('left','right','2026-08-01')"
        )

        core._migrate_dupe_candidate_dismissals(legacy)

        columns = {row[1] for row in legacy.execute(
            "PRAGMA table_info(dupe_candidate_dismissals)"
        )}
        assert {"dismissed", "version"} <= columns
        assert legacy.execute(
            "SELECT dismissed,version FROM dupe_candidate_dismissals"
        ).fetchone() == (1, 1)
    finally:
        legacy.close()


def test_candidate_confirmation_does_not_rollback_caller_transaction(conn):
    make_job(conn, job_url="left", company="Acme", title="Analyst", source="linkedin")
    make_job(conn, job_url="right", company="Acme", title="Analyst", source="adzuna")
    conn.execute("BEGIN")

    with pytest.raises(RuntimeError, match="clean database connection"):
        dupe_candidates.confirm_candidate(
            conn, "left", "right", ["left", "right"],
        )

    assert conn.in_transaction
    conn.rollback()
