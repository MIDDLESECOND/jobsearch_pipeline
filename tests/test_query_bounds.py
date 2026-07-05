"""Query-count regression guard.

The chain-decision question has one implementation (chain.effective_decision / effective_decisions),
which is correct but, as a per-ROW primitive, invites an N+1: looping it fires one query per row.
That regression shipped once (the web UI's backlog view, ~5s on the real DB) and lived in the report
render loops too. These tests pin the contract — building a list view / a report fetches decisions in
a BOUNDED number of queries, independent of row count — so reverting to a per-row loop fails CI.

Counting uses sqlite3's native trace callback (conn.set_trace_callback); no Connection subclass.
Synthetic in-memory-style temp DB via the shared `conn` fixture + `make_job` (tests never touch the
real jobs.db)."""

import math
from contextlib import contextmanager

import chain
import report
from conftest import make_job


@contextmanager
def count_queries(conn):
    """Count SQL statements executed on `conn` within the block. Returns a one-element list whose
    [0] holds the running count (readable after the block)."""
    n = [0]

    def _trace(_sql):
        n[0] += 1

    conn.set_trace_callback(_trace)
    try:
        yield n
    finally:
        conn.set_trace_callback(None)


def _seed_chains(conn, n_canonicals, relistings_each):
    """Insert n_canonicals chains, each a canonical + `relistings_each` reposts pointing at it.
    Decide a fraction of the chains (applied/passed/reject) so the reduction has real work to do."""
    for i in range(n_canonicals):
        canon = f"c{i}"
        # Spread decisions across chains so effective_decision/-s actually reduce something.
        app = "applied" if i % 5 == 0 else ("passed" if i % 5 == 1 else None)
        filt = "manual" if i % 5 == 2 else None
        make_job(
            conn, job_url=canon, title=f"Analyst {i}", company=f"Co {i}",
            app_status=app, status_date="2026-06-02" if app else None,
            filter_source=filt, filter_gate="comp" if filt else None,
            filter_date="2026-06-02" if filt else None,
        )
        for j in range(relistings_each):
            make_job(conn, job_url=f"{canon}-r{j}", title=f"Analyst {i}",
                     company=f"Co {i}", repost_of=canon)


def test_effective_decisions_query_count_is_bounded(conn):
    """The batched primitive issues O(chunks) queries, not O(rows). A per-row loop would issue ~N."""
    n_canon, per = 80, 3
    _seed_chains(conn, n_canon, per)
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == n_canon * (1 + per)  # sanity: 320 rows

    with count_queries(conn) as nq:
        decisions = chain.effective_decisions(conn, rows)
    # All canonicals fit in one CHUNK (400), so the batch is a single SELECT. Allow chunk math + 1.
    bound = math.ceil(n_canon / 400) + 1
    assert nq[0] <= bound, f"effective_decisions issued {nq[0]} queries for {len(rows)} rows (bound {bound})"
    assert len(decisions) == len(rows)  # every row got a decision

    # Contrast: the per-row path this replaced fires ~one query per row — documents what we guard against.
    with count_queries(conn) as nq_perrow:
        for r in rows:
            chain.effective_decision(conn, r)
    assert nq_perrow[0] >= len(rows), "per-row path should be O(N) — if not, this guard is mis-measuring"


def test_generate_report_query_count_is_bounded(conn, tmp_path):
    """Building the daily report fetches decisions once (batched), not per posting rendered.
    Fails before report.py was switched to effective_decisions; passes after."""
    # A day spanning every rendered section, plus reposts of decided canonicals.
    for i in range(25):
        make_job(conn, first_seen="2026-06-15T00:00:00", verdict="PASS", status="evaluated", fit_score=15)
    for i in range(25):
        make_job(conn, first_seen="2026-06-15T00:00:00", verdict="RECRUITER_ONLY", status="evaluated", bucket=1)
    for i in range(25):
        make_job(conn, first_seen="2026-06-15T00:00:00", verdict="GATE_FAIL", status="evaluated",
                 failed_gate="comp", eval_json='{"gate_notes": "below floor"}')
    for i in range(15):
        make_job(conn, first_seen="2026-06-15T00:00:00", status="needs_manual", description=None)
    for i in range(10):
        make_job(conn, first_seen="2026-06-15T00:00:00", filter_source="rule:x", filter_gate="comp")
    # A few reposts whose canonical (seen earlier) is already decided — exercises the repost banners.
    canon = make_job(conn, job_url="canon-applied", first_seen="2026-06-01T00:00:00",
                     app_status="applied", status_date="2026-06-05")
    for j in range(5):
        make_job(conn, job_url=f"rep{j}", first_seen="2026-06-15T00:00:00",
                 verdict="PASS", status="evaluated", repost_of="canon-applied")

    cfg = {"settings": {"reports_dir": str(tmp_path)}}
    with count_queries(conn) as nq:
        report.generate_report(cfg, conn, for_date="2026-06-15")
    # One SELECT for the day's rows + one batched effective_decisions chunk = ~2; allow slack.
    assert nq[0] <= 5, f"generate_report issued {nq[0]} queries — N+1 regression (should be ~2)"
