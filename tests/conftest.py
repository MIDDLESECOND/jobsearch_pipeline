"""Shared pytest fixtures.

Tests run against a real SQLite schema (core.get_db builds it) but in a throwaway
temp file — never the user's jobs.db. Pure functions (normalization, eval routing,
filters) need no DB; the chain/dupe tests use the `conn` fixture + `make_job` helper,
which fills the same normalized columns a real fetch would.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chain  # noqa: E402
import core  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    """A fresh DB on every test, with the full schema + migrations applied."""
    cfg = {"settings": {"db_path": str(tmp_path / "test.db")}}
    c = core.get_db(cfg)
    try:
        yield c
    finally:
        c.close()


def job_status(conn, url):
    """The one spelling of "read this row's status" for state-machine tests."""
    return conn.execute(
        "SELECT status FROM jobs WHERE job_url=?", (url,)
    ).fetchone()["status"]


# Monotonic default url so callers that don't care about the url still get unique ones.
_n = [0]


def make_job(conn, *, job_url=None, title="Data Analyst", company="Acme Corp",
             location="New York, NY", search_name="s1", tier="primary",
             first_seen="2026-06-01T00:00:00", date_posted="", status="evaluated", verdict="PASS",
             failed_gate=None, fit_score=12, bucket=2, eval_json=None,
             salary_min=None, salary_max=None, description="a job description",
             source="linkedin", repost_of=None, repost_source=None,
             app_status=None, status_date=None, filter_source=None,
             filter_gate=None, filter_date=None, norm_title=None, fingerprint=None):
    """Insert one jobs row, deriving the normalized/fingerprint columns from
    company/title/location exactly as the real fetchers do (override-able). Returns
    the inserted sqlite3.Row."""
    if job_url is None:
        _n[0] += 1
        job_url = f"https://example.com/job/{_n[0]}"
    norm_company = chain._norm_company(company)
    if norm_title is None:
        norm_title = chain._norm_title(title)
    if fingerprint is None:
        fingerprint = chain._fingerprint(company, location)
    conn.execute(
        """INSERT INTO jobs
           (job_url, title, company, location, search_name, tier, first_seen, date_posted,
            status, verdict, failed_gate, fit_score, bucket, eval_json,
            salary_min, salary_max, description, source, repost_of, repost_source,
            app_status, status_date, filter_source, filter_gate, filter_date,
            norm_company, norm_title, fingerprint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_url, title, company, location, search_name, tier, first_seen, date_posted,
         status, verdict, failed_gate, fit_score, bucket, eval_json,
         salary_min, salary_max, description, source, repost_of, repost_source,
         app_status, status_date, filter_source, filter_gate, filter_date,
         norm_company, norm_title, fingerprint),
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
