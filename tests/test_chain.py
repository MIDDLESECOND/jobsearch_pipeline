"""Repost-chain decision logic: targets, effective decision, propagation, and the
manual dupe-link resolve/commit/unlink cores. This is the most-duplicated logic in
the codebase (report / UI / dupe each touch it) — these tests pin its behavior so the
planned chain.py extraction is safe.
"""

import pipeline
from conftest import make_job


# ----- _chain_targets / _chain_members ----------------------------------------

def test_chain_targets_covers_canonical_and_all_relistings(conn):
    canon = make_job(conn, job_url="c")
    make_job(conn, job_url="r1", repost_of="c")
    make_job(conn, job_url="r2", repost_of="c")
    # Resolving from a relisting still returns the whole chain.
    relisting = conn.execute("SELECT * FROM jobs WHERE job_url='r1'").fetchone()
    assert pipeline._chain_targets(conn, relisting) == {"c", "r1", "r2"}
    assert pipeline._chain_targets(conn, canon) == {"c", "r1", "r2"}


# ----- _chain_decision / _decision_sig ----------------------------------------

def test_chain_decision_applied_outranks_passed(conn):
    make_job(conn, job_url="c", app_status="passed", status_date="2026-06-02")
    make_job(conn, job_url="r1", repost_of="c", app_status="applied",
             status_date="2026-06-03")
    dec = pipeline._chain_decision(conn, {"c", "r1"})
    assert dec["app_status"] == "applied"


def test_chain_decision_none_when_undecided(conn):
    make_job(conn, job_url="c")
    assert pipeline._chain_decision(conn, {"c"}) is None


def test_decision_sig_distinguishes_applied_from_passed(conn):
    make_job(conn, job_url="a", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="b", app_status="passed", status_date="2026-06-01")
    da = pipeline._chain_decision(conn, {"a"})
    db = pipeline._chain_decision(conn, {"b"})
    assert pipeline._decision_sig(da) != pipeline._decision_sig(db)


# ----- decision propagation across a chain (cmd_mark) --------------------------

def test_cmd_mark_propagates_across_chain(conn):
    make_job(conn, job_url="c")
    make_job(conn, job_url="r1", repost_of="c")
    # Mark the RELISTING; the decision must land on the canonical and the sibling too.
    pipeline.cmd_mark(conn, "r1", "applied")
    rows = {r["job_url"]: r["app_status"]
            for r in conn.execute("SELECT job_url, app_status FROM jobs")}
    assert rows == {"c": "applied", "r1": "applied"}


def test_cmd_mark_undo_clears_whole_chain(conn):
    make_job(conn, job_url="c", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="r1", repost_of="c", app_status="applied",
             status_date="2026-06-01")
    pipeline.cmd_mark(conn, "c", None)
    statuses = [r["app_status"] for r in conn.execute("SELECT app_status FROM jobs")]
    assert statuses == [None, None]


def test_cmd_reject_does_not_clobber_a_rule_sibling(conn):
    # Canonical is undecided; a relisting was auto-failed by a filters.yaml rule.
    make_job(conn, job_url="c", filter_source=None)
    make_job(conn, job_url="r1", repost_of="c", status="rule_filtered",
             verdict="GATE_FAIL", filter_source="rule:clearance",
             filter_gate="work_auth")
    pipeline.cmd_reject(conn, "c", "domain_requirement", None, None, False)
    rows = {r["job_url"]: r["filter_source"]
            for r in conn.execute("SELECT job_url, filter_source FROM jobs")}
    assert rows["c"] == "manual"            # the explicitly rejected row
    assert rows["r1"] == "rule:clearance"   # sibling's rule attribution preserved


# ----- skip_decided_reposts (forward + reverse reconcile) ----------------------

def test_skip_decided_reposts_skips_new_relisting_of_decided_role(conn):
    make_job(conn, job_url="c", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="r1", repost_of="c", status="new", verdict=None)
    pipeline.skip_decided_reposts(conn)
    r1 = conn.execute("SELECT status FROM jobs WHERE job_url='r1'").fetchone()
    assert r1["status"] == "repost_decided"


def test_skip_decided_reposts_reverses_when_decision_undone(conn):
    # A relisting parked at 'repost_decided', but the canonical is now undecided —
    # it must return to 'new' so it isn't stranded, never re-evaluated.
    make_job(conn, job_url="c", app_status=None)
    make_job(conn, job_url="r1", repost_of="c", status="repost_decided", verdict=None)
    pipeline.skip_decided_reposts(conn)
    r1 = conn.execute("SELECT status FROM jobs WHERE job_url='r1'").fetchone()
    assert r1["status"] == "new"


# ----- effective_decision (the shared report/UI/dupe primitive) ----------------

def test_effective_decision_surfaces_canonical_for_fresh_relisting(conn):
    # The canonical was applied; a relisting fetched LATER has app_status NULL of its own,
    # but effective_decision must report the chain-wide 'applied' so report+UI show the banner.
    make_job(conn, job_url="c", app_status="applied", status_date="2026-06-01")
    relist = make_job(conn, job_url="r1", repost_of="c", app_status=None)
    dec = pipeline.effective_decision(conn, relist)
    assert dec["app_status"] == "applied"
    assert dec["status_date"] == "2026-06-01"
    assert dec["is_repost"] is True
    assert dec["original_first_seen"] is not None


def test_effective_decision_undecided_chain(conn):
    canon = make_job(conn, job_url="c", app_status=None)
    dec = pipeline.effective_decision(conn, canon)
    assert dec["app_status"] is None
    assert dec["reject"] is False
    assert dec["is_repost"] is False


def test_effective_decision_reports_chain_reject(conn):
    make_job(conn, job_url="c", filter_source="manual", filter_gate="work_auth",
             filter_date="2026-06-01")
    relist = make_job(conn, job_url="r1", repost_of="c")
    dec = pipeline.effective_decision(conn, relist)
    assert dec["reject"] is True
    assert dec["filter_gate"] == "work_auth"


# ----- _dupe_resolve guards ----------------------------------------------------

def test_dupe_resolve_picks_earliest_first_seen_as_canonical(conn):
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00")
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00")
    plan, err = pipeline._dupe_resolve(conn, "late", "early")
    assert err is None
    assert plan["winner"]["job_url"] == "early"
    assert plan["loser"]["job_url"] == "late"


def test_dupe_resolve_rejects_same_role(conn):
    make_job(conn, job_url="c")
    make_job(conn, job_url="r1", repost_of="c")
    plan, err = pipeline._dupe_resolve(conn, "c", "r1")
    assert plan is None
    assert "already the same role" in err


def test_dupe_resolve_blocks_conflicting_decisions(conn):
    make_job(conn, job_url="a", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="b", app_status="passed", status_date="2026-06-02")
    plan, err = pipeline._dupe_resolve(conn, "a", "b")
    assert plan is None
    assert "decided differently" in err


# ----- _dupe_commit / _dupe_unlink round trip ----------------------------------

def test_dupe_commit_links_and_propagates_then_unlink_restores(conn):
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00",
             app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00", app_status=None)

    plan, err = pipeline._dupe_resolve(conn, "late", "early")
    assert err is None
    pipeline._dupe_commit(conn, plan)

    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["repost_of"] == "early"
    assert late["repost_source"] == "manual"
    # The winner's decision propagated onto the newly-linked loser.
    assert late["app_status"] == "applied"

    # Undo splits them back into independent chains.
    ok, msg, _ = pipeline._dupe_unlink(conn, late)
    assert ok
    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["repost_of"] is None
    assert late["repost_source"] is None
