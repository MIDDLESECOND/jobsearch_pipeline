"""_find_repost — the fetch-time content dedup (company+location AND exact title)."""

import chain
from conftest import make_job


def _find(conn, company, location, title, exclude_url=None):
    return chain._find_repost(
        conn,
        chain._fingerprint(company, location),
        chain._norm_title(title),
        exclude_url=exclude_url,
    )


def test_same_role_relisting_links_to_original(conn):
    orig = make_job(conn, job_url="u1", company="Acme Corp",
                    location="New York, NY", title="Data Analyst",
                    first_seen="2026-06-01T00:00:00")
    # A relisting under a fresh url, same normalized company+location+title.
    found = _find(conn, "Acme, Inc.", "New York, NY", "Data Analyst", exclude_url="u2")
    assert found == orig["job_url"]


def test_different_title_is_not_a_repost(conn):
    make_job(conn, job_url="u1", company="Acme Corp", location="New York, NY",
             title="Workday Business Analyst")
    found = _find(conn, "Acme Corp", "New York, NY", "SalesForce Business Analyst")
    assert found is None


def test_chain_points_at_canonical_not_intermediate(conn):
    # orig <- repost1; a new match should resolve to orig (the canonical), not repost1.
    orig = make_job(conn, job_url="u1", first_seen="2026-06-01T00:00:00")
    make_job(conn, job_url="u2", first_seen="2026-06-02T00:00:00", repost_of="u1")
    found = _find(conn, "Acme Corp", "New York, NY", "Data Analyst", exclude_url="u3")
    assert found == orig["job_url"]


def test_exclude_url_skips_self(conn):
    make_job(conn, job_url="u1")
    # Excluding the only matching row leaves nothing to link to.
    found = _find(conn, "Acme Corp", "New York, NY", "Data Analyst", exclude_url="u1")
    assert found is None


def test_empty_fingerprint_or_title_returns_none(conn):
    make_job(conn, job_url="u1")
    assert chain._find_repost(conn, "", "data analyst") is None
    assert chain._find_repost(conn, "acme|new york ny", "") is None
