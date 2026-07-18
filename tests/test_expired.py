"""The expired/dead-posting disposition — chain.mark_expired (CLI `expired`, UI Expired
button). Adzuna especially keeps listing delisted jobs; this is the manual triage exit
for one.

The invariants pinned here:
  * forward = chain-wide 'passed' + ONE canonical-keyed EXPIRED_NOTE 'note' event, in one
    operation — the WHY survives in event history, the mark drives the existing skip
    machinery, and the outcome cache stays untouched (a note never sets it);
  * refused in BOTH directions on an applied chain (a dead posting you applied to is an
    outcome event, not a triage disposition — and an unguarded undo after expired→applied
    would clear the applied mark);
  * undo verifies it is unwinding its OWN marker (the chain's last event), then restores
    the chain to fully undecided — no stray note, skip labels released;
  * a relisting fetched AFTER the expired mark is auto-skipped by the existing
    skip_decided_reposts forward pass — no repost-machinery changes were needed.
"""

from datetime import date

import chain
from conftest import make_job, job_status
from states import STATUS_NEW, STATUS_REPOST_DECIDED

TODAY = date.today().isoformat()


def _all_events(conn):
    return conn.execute(
        "SELECT job_url, event_type, note FROM app_events ORDER BY id"
    ).fetchall()


def test_expired_marks_chain_passed_with_canonical_marker_note(conn):
    make_job(conn, job_url="canon", company="Chain Co")
    # A still-pending relisting: the mark must skip it out of the eval queue immediately.
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon",
                      status="new", verdict=None, fit_score=None, bucket=None)
    ok, msg, affected, exempt = chain.mark_expired(conn, relist)
    assert ok and "expired" in msg
    assert set(affected) == {"canon", "relist"}
    assert exempt == ["canon", "relist"]  # chain displayed undecided → whole chain exempt
    rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM jobs").fetchall()}
    assert rows["canon"]["app_status"] == "passed" and rows["canon"]["status_date"] == TODAY
    assert rows["relist"]["app_status"] == "passed"
    # ONE marker note, keyed to the canonical even though the relisting's card was clicked.
    assert [(e["job_url"], e["event_type"], e["note"]) for e in _all_events(conn)] == \
        [("canon", "note", chain.EXPIRED_NOTE)]
    # A note never sets the outcome cache — the follow-up predicate stays clean.
    assert rows["canon"]["outcome_status"] is None and rows["relist"]["outcome_status"] is None
    # The pending relisting left the eval queue at once (chain-scoped reconcile).
    assert job_status(conn, "relist") == STATUS_REPOST_DECIDED


def test_expired_refused_on_applied_chain_both_directions(conn):
    make_job(conn, job_url="canon", company="Chain Co", app_status="applied",
             status_date="2026-06-20")
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon",
                      app_status="applied", status_date="2026-06-20")
    ok, msg, affected, _ = chain.mark_expired(conn, relist)
    assert not ok and "applied" in msg and affected == []
    assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0
    row = conn.execute("SELECT app_status FROM jobs WHERE job_url='canon'").fetchone()
    assert row["app_status"] == "applied"
    # Undo direction: expired → applied leaves the marker as the latest event; the guard
    # must refuse rather than clear the applied mark.
    ok, msg, _, _ = chain.mark_expired(conn, relist, undo=True)
    assert not ok and "applied" in msg


def test_expired_undo_restores_fully(conn):
    make_job(conn, job_url="canon", company="Chain Co")
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon",
                      status="new", verdict=None, fit_score=None, bucket=None)
    chain.mark_expired(conn, relist)
    assert job_status(conn, "relist") == STATUS_REPOST_DECIDED
    ok, msg, affected, exempt = chain.mark_expired(conn, relist, undo=True)
    assert ok and "unmarked" in msg and set(affected) == {"canon", "relist"}
    assert exempt == []  # chain fully undecided again — nothing to keep visible
    rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM jobs").fetchall()}
    assert rows["canon"]["app_status"] is None and rows["canon"]["status_date"] is None
    assert rows["relist"]["app_status"] is None
    assert _all_events(conn) == []          # the marker itself is gone
    assert job_status(conn, "relist") == STATUS_NEW  # released back to the eval queue


def test_expired_undo_refused_when_marker_is_not_last_event(conn):
    row = make_job(conn, job_url="u1")
    chain.mark_expired(conn, row)
    chain.record_event(conn, row, "note", note="user note after the marker")
    ok, msg, _, _ = chain.mark_expired(conn, row, undo=True)
    assert not ok and "last event" in msg
    # Nothing deleted, mark still standing.
    assert len(_all_events(conn)) == 2
    assert conn.execute("SELECT app_status FROM jobs").fetchone()["app_status"] == "passed"
    # ...and with no events at all the same refusal path answers (no marker to unwind).
    bare = make_job(conn, job_url="u2", app_status="passed", status_date=TODAY)
    ok, msg, _, _ = chain.mark_expired(conn, bare, undo=True)
    assert not ok and "last event" in msg


def test_relisting_fetched_after_expired_mark_auto_skips(conn):
    # Pins "no repost-machinery changes needed": the expired mark is app_status-shaped, so
    # the existing decided-skip forward pass claims a LATER-fetched relisting on the next run.
    row = make_job(conn, job_url="canon", company="Chain Co")
    chain.mark_expired(conn, row)
    make_job(conn, job_url="late-relist", company="Chain Co", repost_of="canon",
             status="new", verdict=None, fit_score=None, bucket=None)
    chain.skip_decided_reposts(conn, restore=False)
    assert job_status(conn, "late-relist") == STATUS_REPOST_DECIDED
