"""Manual external-role intake enters the same guarded posting pipeline as fetchers."""

from datetime import datetime

import pytest

from chain import _fingerprint, _norm_company, _norm_title
from conftest import make_job


NOW = datetime(2026, 8, 7, 14, 5, 6)
SEARCHES = [{"name": "AI leadership", "tier": "primary", "min_salary": 80_000}]


def _payload(**overrides):
    values = {
        "job_url": " https://careers.example.test/jobs/123?source=referral ",
        "title": " Staff AI Engineer ",
        "company": " Example, Inc. ",
        "location": " Chicago, IL ",
        "date_posted": "2026-08-06",
        "salary_min": "180000",
        "salary_max": 220000,
        "description": " Build reliable AI products. ",
        "search_name": "AI leadership",
    }
    values.update(overrides)
    return values


def test_add_manual_posting_normalizes_and_enters_new_pipeline_state(conn):
    from intake import add_manual_posting

    row = add_manual_posting(
        conn, _payload(), searches=SEARCHES, max_description_chars=10_000, now=NOW
    )

    assert row["job_url"] == "https://careers.example.test/jobs/123?source=referral"
    assert row["title"] == "Staff AI Engineer"
    assert row["company"] == "Example, Inc."
    assert row["location"] == "Chicago, IL"
    assert row["date_posted"] == "2026-08-06"
    assert row["first_seen"] == "2026-08-07T14:05:06"
    assert row["salary_min"] == 180000
    assert row["salary_max"] == 220000
    assert row["description"] == "Build reliable AI products."
    assert row["source"] == "manual"
    assert row["search_name"] == "AI leadership"
    assert row["tier"] == "primary"
    assert row["status"] == "new"
    assert row["verdict"] is None and row["eval_json"] is None
    assert row["norm_company"] == _norm_company(row["company"])
    assert row["norm_title"] == _norm_title(row["title"])
    assert row["fingerprint"] == _fingerprint(row["company"], row["location"])
    assert not conn.in_transaction


def test_add_manual_posting_joins_existing_exact_fingerprint_chain(conn):
    from intake import add_manual_posting

    existing = make_job(
        conn,
        job_url="https://linkedin.test/old",
        title="Staff AI Engineer",
        company="Example Inc",
        location="Chicago, IL",
        first_seen="2026-08-01T09:00:00",
    )

    row = add_manual_posting(
        conn, _payload(), searches=SEARCHES, max_description_chars=10_000, now=NOW
    )

    assert row["repost_of"] == existing["job_url"]
    assert row["repost_source"] is None


def test_add_manual_posting_refuses_exact_url_without_overwriting(conn):
    from intake import PostingAlreadyExists, add_manual_posting

    existing = make_job(
        conn,
        job_url="https://careers.example.test/jobs/123?source=referral",
        title="Original title",
        description="original evidence",
    )

    with pytest.raises(PostingAlreadyExists, match="already exists"):
        add_manual_posting(
            conn, _payload(), searches=SEARCHES,
            max_description_chars=10_000, now=NOW
        )

    stored = conn.execute(
        "SELECT title,description FROM jobs WHERE job_url=?", (existing["job_url"],)
    ).fetchone()
    assert tuple(stored) == ("Original title", "original evidence")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"job_url": "javascript:alert(1)"}, "http"),
        ({"job_url": "https://user:secret@example.test/job"}, "credentials"),
        ({"job_url": "https://example.test\\@evil.test/job"}, "well-formed"),
        ({"job_url": "https://example.test:bad/job"}, "well-formed"),
        ({"job_url": ["https://example.test"]}, "job_url must be a string"),
        ({"title": "  "}, "title is required"),
        ({"company": None}, "company is required"),
        ({"search_name": "Unknown track"}, "configured search"),
        ({"date_posted": "08/06/2026"}, "date_posted must be"),
        ({"date_posted": "2026-08-08"}, "cannot be in the future"),
        ({"salary_min": True}, "salary_min must be"),
        ({"salary_min": -1}, "salary_min must be"),
        ({"salary_min": 250000, "salary_max": 200000}, "salary_min cannot exceed"),
    ],
)
def test_add_manual_posting_validates_untrusted_fields(conn, overrides, message):
    from intake import add_manual_posting

    with pytest.raises(ValueError, match=message):
        add_manual_posting(
            conn, _payload(**overrides), searches=SEARCHES,
            max_description_chars=10_000, now=NOW
        )
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_add_manual_posting_refuses_description_loss_and_caller_transaction(conn):
    from intake import add_manual_posting

    with pytest.raises(ValueError, match="description exceeds 10 characters"):
        add_manual_posting(
            conn, _payload(description="x" * 11), searches=SEARCHES,
            max_description_chars=10, now=NOW
        )
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    conn.execute("INSERT INTO meta(key,value) VALUES ('pending','caller work')")
    with pytest.raises(RuntimeError, match="clean database connection"):
        add_manual_posting(
            conn, _payload(), searches=SEARCHES,
            max_description_chars=10_000, now=NOW
        )
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM meta WHERE key='pending'").fetchone()[0] == \
        "caller work"
    conn.rollback()


def test_manual_posting_really_enters_configured_salary_filter(conn):
    from filters import apply_salary_filter
    from intake import add_manual_posting

    row = add_manual_posting(
        conn,
        _payload(salary_min=50_000, salary_max=70_000),
        searches=SEARCHES,
        max_description_chars=10_000,
        now=NOW,
    )

    apply_salary_filter({"searches": SEARCHES}, conn)

    status = conn.execute(
        "SELECT status FROM jobs WHERE job_url=?", (row["job_url"],)
    ).fetchone()[0]
    assert status == "salary_filtered"


def test_manual_intake_handles_valid_ats_only_config_without_a_search_track(conn):
    from intake import add_manual_posting

    row = add_manual_posting(
        conn,
        _payload(search_name="manual intake"),
        searches=[],
        max_description_chars=10_000,
        now=NOW,
    )

    assert row["search_name"] == "manual intake"
    assert row["tier"] == "manual"


def test_manual_intake_rejects_ambiguous_duplicate_search_names(conn):
    from intake import add_manual_posting

    with pytest.raises(ValueError, match="ambiguous"):
        add_manual_posting(
            conn,
            _payload(),
            searches=[*SEARCHES, {"name": "AI leadership", "tier": "secondary"}],
            max_description_chars=10_000,
            now=NOW,
        )


def test_manual_intake_preserves_configured_search_key_used_by_salary_filter(conn):
    from filters import apply_salary_filter
    from intake import add_manual_posting

    configured = [{"name": " AI leadership ", "tier": "primary", "min_salary": 80_000}]
    row = add_manual_posting(
        conn,
        _payload(search_name=" AI leadership ", salary_min=50_000, salary_max=70_000),
        searches=configured,
        max_description_chars=10_000,
        now=NOW,
    )

    assert row["search_name"] == " AI leadership "
    apply_salary_filter({"searches": configured}, conn)
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_url=?", (row["job_url"],)
    ).fetchone()[0] == "salary_filtered"
