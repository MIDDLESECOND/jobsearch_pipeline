"""Repost-chain decision logic: targets, effective decision, propagation, and the
manual dupe-link resolve/commit/unlink cores. This is the most-duplicated logic in
the codebase (report / UI / dupe each touch it) — these tests pin its behavior so the
planned chain.py extraction is safe.
"""

import chain
import pipeline
from conftest import make_job, job_status


# ----- _chain_targets / _chain_members ----------------------------------------

def test_chain_targets_covers_canonical_and_all_relistings(conn):
    canon = make_job(conn, job_url="c")
    make_job(conn, job_url="r1", repost_of="c")
    make_job(conn, job_url="r2", repost_of="c")
    # Resolving from a relisting still returns the whole chain.
    relisting = conn.execute("SELECT * FROM jobs WHERE job_url='r1'").fetchone()
    assert chain._chain_targets(conn, relisting) == {"c", "r1", "r2"}
    assert chain._chain_targets(conn, canon) == {"c", "r1", "r2"}


# ----- _chain_decision / _decision_sig ----------------------------------------

def test_chain_decision_applied_outranks_passed(conn):
    make_job(conn, job_url="c", app_status="passed", status_date="2026-06-02")
    make_job(conn, job_url="r1", repost_of="c", app_status="applied",
             status_date="2026-06-03")
    dec = chain._chain_decision(conn, {"c", "r1"})
    assert dec["app_status"] == "applied"


def test_chain_decision_none_when_undecided(conn):
    make_job(conn, job_url="c")
    assert chain._chain_decision(conn, {"c"}) is None


def test_decision_sig_distinguishes_applied_from_passed(conn):
    make_job(conn, job_url="a", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="b", app_status="passed", status_date="2026-06-01")
    da = chain._chain_decision(conn, {"a"})
    db = chain._chain_decision(conn, {"b"})
    assert chain._decision_sig(da) != chain._decision_sig(db)


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


def test_cmd_reject_lifts_new_row_and_undo_restores_it(conn):
    # A still-'new' row rejected before eval is lifted to 'rule_filtered'; undo returns it to 'new'
    # so it isn't permanently excluded from the eval stage.
    make_job(conn, job_url="c", status="new", verdict=None, filter_source=None)
    pipeline.cmd_reject(conn, "c", "work_auth", None, None, False)
    row = conn.execute("SELECT status, filter_source FROM jobs WHERE job_url='c'").fetchone()
    assert (row["status"], row["filter_source"]) == ("rule_filtered", "manual")
    pipeline.cmd_reject(conn, "c", "work_auth", None, None, True)  # undo
    row = conn.execute("SELECT status, filter_source FROM jobs WHERE job_url='c'").fetchone()
    assert (row["status"], row["filter_source"]) == ("new", None)


# ----- skip_decided_reposts (forward + reverse reconcile) ----------------------

def test_skip_decided_reposts_skips_new_relisting_of_decided_role(conn):
    make_job(conn, job_url="c", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="r1", repost_of="c", status="new", verdict=None)
    chain.skip_decided_reposts(conn)
    r1 = conn.execute("SELECT status FROM jobs WHERE job_url='r1'").fetchone()
    assert r1["status"] == "repost_decided"


def test_skip_decided_reposts_reverses_when_decision_undone(conn):
    # A relisting parked at 'repost_decided', but the canonical is now undecided —
    # it must return to 'new' so it isn't stranded, never re-evaluated.
    make_job(conn, job_url="c", app_status=None)
    make_job(conn, job_url="r1", repost_of="c", status="repost_decided", verdict=None)
    chain.skip_decided_reposts(conn)
    r1 = conn.execute("SELECT status FROM jobs WHERE job_url='r1'").fetchone()
    assert r1["status"] == "new"


# ----- skip_evaluated_reposts (one eval per chain) ------------------------------


def test_skip_evaluated_reposts_skips_relisting_of_evaluated_role(conn):
    make_job(conn, job_url="c", status="evaluated", verdict="PASS")
    make_job(conn, job_url="r1", repost_of="c", status="new", verdict=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r1") == "repost_evaluated"
    # The skipped row's own verdict stays NULL — readers use chain_verdict, never a copy.
    r1 = conn.execute("SELECT verdict FROM jobs WHERE job_url='r1'").fetchone()
    assert r1["verdict"] is None


def test_skip_evaluated_reposts_matches_verdict_on_sibling_not_canonical(conn):
    # Verdicts don't propagate chain-wide: the chain's only verdict may sit on a sibling
    # (canonical salary-filtered, a later relisting evaluated). The skip must still fire.
    make_job(conn, job_url="c", status="salary_filtered", verdict=None)
    make_job(conn, job_url="r1", repost_of="c", status="evaluated", verdict="GATE_FAIL")
    make_job(conn, job_url="r2", repost_of="c", status="new", verdict=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r2") == "repost_evaluated"


def test_skip_evaluated_reposts_leaves_unevaluated_chain_alone(conn):
    make_job(conn, job_url="c", status="new", verdict=None)
    make_job(conn, job_url="r1", repost_of="c", status="new", verdict=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r1") == "new"


def test_skip_evaluated_reposts_reverses_when_unlinked(conn):
    # dupe_unlink clears repost_of; the row is no longer a relisting of anything and
    # must return to 'new' for its own eval instead of being stranded verdict-less.
    make_job(conn, job_url="a", status="repost_evaluated", verdict=None, repost_of=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "a") == "new"


def test_skip_evaluated_reposts_reverses_when_chain_verdict_cleared(conn):
    make_job(conn, job_url="c", status="new", verdict=None)
    make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r1") == "new"


def test_decided_skip_outranks_evaluated_skip_in_stage_order(conn):
    # Both passes apply (decided AND evaluated chain): running them in the pipeline's order
    # must leave the more informative 'repost_decided', not 'repost_evaluated'.
    make_job(conn, job_url="c", status="evaluated", verdict="PASS",
             app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="r1", repost_of="c", status="new", verdict=None)
    chain.skip_decided_reposts(conn)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r1") == "repost_decided"


def test_skip_evaluated_reposts_catches_still_new_canonical(conn):
    # The chain's verdict can sit on a relisting while the CANONICAL is still 'new' (it
    # errored on day 1, the relisting was evaluated on day 2, requeue_error_rows returned the
    # canonical to 'new' on day 3). The pass keys on COALESCE(repost_of, job_url), so the
    # canonical is skipped too — and the reverse pass must NOT immediately un-skip it.
    make_job(conn, job_url="c", status="new", verdict=None)
    make_job(conn, job_url="r1", repost_of="c", status="evaluated", verdict="PASS")
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "c") == "repost_evaluated"
    chain.skip_evaluated_reposts(conn)  # idempotent: a second reconcile keeps it skipped
    assert job_status(conn, "c") == "repost_evaluated"


def test_skip_evaluated_reposts_ignores_rule_stamped_verdicts(conn):
    # apply_hard_filters stamps a synthetic verdict=GATE_FAIL on rule_filtered rows — a
    # deterministic text match, not a judgment. A chain whose ONLY "verdict" is a rule stamp
    # on a sibling must NOT suppress the eval: a relisting whose reworded text no longer trips
    # the rule gets the documented one extra safety-valve eval.
    make_job(conn, job_url="c", status="salary_filtered", verdict=None)
    make_job(conn, job_url="s", repost_of="c", status="rule_filtered", verdict="GATE_FAIL",
             filter_source="rule:clearance", filter_gate="work_auth")
    make_job(conn, job_url="r2", repost_of="c", status="new", verdict=None)
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "r2") == "new"  # eval still happens
    # And the rule stamp never masquerades as the judge's chain verdict.
    r2 = conn.execute("SELECT * FROM jobs WHERE job_url='r2'").fetchone()
    assert chain.effective_decision(conn, r2)["chain_verdict"] is None


def test_reject_on_repost_evaluated_row_lifts_to_rule_filtered(conn):
    # A rejected never-evaluated row must land where the state machine parks rejects
    # ('rule_filtered'), not keep its skip status — otherwise a later unlink's reverse pass
    # would hand the REJECTED row back to the paid eval.
    make_job(conn, job_url="c", status="evaluated", verdict="PASS")
    r1 = make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None)
    chain.reject_posting(conn, r1, "work_auth")
    conn.commit()
    assert job_status(conn, "r1") == "rule_filtered"


def test_reverse_pass_never_restores_a_decided_row(conn):
    # Belt-and-braces for decision stamps that arrive without a status lift (e.g. applied via
    # propagation): an unlinked, chain-less repost_evaluated row that carries a decision must
    # not be handed back to the eval queue by the reverse pass.
    make_job(conn, job_url="a", status="repost_evaluated", verdict=None, repost_of=None,
             app_status="applied", status_date="2026-06-01")
    chain.skip_evaluated_reposts(conn)
    assert job_status(conn, "a") != "new"


def test_decided_pass_upgrades_repost_evaluated_when_chain_decided_later(conn):
    # A chain decided AFTER the eval-skip: the row must upgrade to the more informative
    # 'repost_decided' (and leave the report's "already-evaluated" section) on the next
    # reconcile, not stay mislabeled forever.
    make_job(conn, job_url="c", status="evaluated", verdict="PASS",
             app_status="applied", status_date="2026-06-02")
    make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None,
             app_status="applied", status_date="2026-06-02")
    chain.skip_decided_reposts(conn)
    assert job_status(conn, "r1") == "repost_decided"


def test_decided_pass_catches_still_new_decided_canonical(conn):
    # `applied` on a fetched-but-not-yet-evaluated posting: the canonical carries the decision
    # itself (repost_of NULL), and the COALESCE key must spare it the pointless paid eval.
    make_job(conn, job_url="c", status="new", verdict=None,
             app_status="applied", status_date="2026-06-01")
    chain.skip_decided_reposts(conn)
    assert job_status(conn, "c") == "repost_decided"


def test_restored_row_refaces_filters_before_eval(conn, monkeypatch):
    """The stage-order contract for restores: a skipped row released back to 'new' must
    re-face the CURRENT hard rules before the paid eval — so `run` executes the restore
    direction BEFORE apply_hard_filters and the forward direction after. This test replays
    that exact order with the real functions."""
    import filters
    # A repost_evaluated relisting whose chain verdict has been cleared (manual reset) and
    # whose text trips a rule that was added while it sat skipped.
    make_job(conn, job_url="c", status="new", verdict=None)
    make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None,
             description="requires active TS/SCI clearance")
    monkeypatch.setattr(filters, "load_filters",
                        lambda: [{"name": "clearance", "gate": "work_auth", "any": ["TS/SCI"]}])
    chain.skip_decided_reposts(conn, forward=False)      # the run order, restore first
    chain.skip_evaluated_reposts(conn, forward=False)
    assert job_status(conn, "r1") == "new"                  # released...
    filters.apply_hard_filters({"settings": {}}, conn)   # ...and the current rules see it
    chain.skip_decided_reposts(conn, restore=False)
    chain.skip_evaluated_reposts(conn, restore=False)
    assert job_status(conn, "r1") == "rule_filtered"        # the rule won; eval never will


def test_mark_posting_reconciles_skip_labels_immediately(conn):
    # A decision made through the UI/CLI must upgrade repost_evaluated siblings NOW, not at
    # the next run — a report rebuild in between would show the stale "already-evaluated"
    # label for a role the user acted on. The reconcile is chain-scoped (the global passes
    # cost ~1s per click at real DB sizes).
    make_job(conn, job_url="c", status="evaluated", verdict="PASS")
    make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None)
    canon = conn.execute("SELECT * FROM jobs WHERE job_url='c'").fetchone()
    chain.mark_posting(conn, canon, "applied")
    assert job_status(conn, "r1") == "repost_decided"
    # The undo RELEASES it to 'new' (honest: unscored until it re-faces the filters in the
    # next run, which then re-skips it after them) — deliberately NOT re-skipped inline,
    # which would bypass the restore-before-filters contract.
    chain.mark_posting(conn, canon, None)
    assert job_status(conn, "r1") == "new"
    chain.skip_evaluated_reposts(conn, restore=False)  # the next run's post-filter phase
    assert job_status(conn, "r1") == "repost_evaluated"


def test_mark_posting_does_not_touch_other_chains(conn):
    # The per-click reconcile is scoped to the decided chain: an unrelated chain's stale
    # label is the global run passes' job, not this click's.
    make_job(conn, job_url="c", status="evaluated", verdict="PASS")
    make_job(conn, job_url="other_c", company="Other Co", status="evaluated", verdict="PASS",
             app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="other_r", company="Other Co", repost_of="other_c",
             status="new", verdict=None)
    canon = conn.execute("SELECT * FROM jobs WHERE job_url='c'").fetchone()
    chain.mark_posting(conn, canon, "applied")
    assert job_status(conn, "other_r") == "new"  # untouched; next run's passes will skip it


def test_decided_reverse_pass_rescues_unlinked_ex_canonical(conn):
    # Regression for the pre-existing strand: an unlinked ex-loser-canonical (repost_of NULL,
    # status 'repost_decided') whose copied decision is later undone was never restored —
    # `repost_of NOT IN (...)` is NULL-false. The COALESCE key restores it once undecided,
    # while another decided row exists in the DB (the empty-subquery case masks the bug).
    make_job(conn, job_url="other", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="b", status="repost_decided", verdict=None, repost_of=None,
             app_status=None, filter_source=None)
    chain.skip_decided_reposts(conn)
    assert job_status(conn, "b") == "new"


# ----- effective_decision (the shared report/UI/dupe primitive) ----------------

def test_effective_decision_surfaces_canonical_for_fresh_relisting(conn):
    # The canonical was applied; a relisting fetched LATER has app_status NULL of its own,
    # but effective_decision must report the chain-wide 'applied' so report+UI show the banner.
    make_job(conn, job_url="c", app_status="applied", status_date="2026-06-01")
    relist = make_job(conn, job_url="r1", repost_of="c", app_status=None)
    dec = chain.effective_decision(conn, relist)
    assert dec["app_status"] == "applied"
    assert dec["status_date"] == "2026-06-01"
    assert dec["is_repost"] is True
    assert dec["original_first_seen"] is not None


def test_effective_decision_undecided_chain(conn):
    canon = make_job(conn, job_url="c", app_status=None)
    dec = chain.effective_decision(conn, canon)
    assert dec["app_status"] is None
    assert dec["reject"] is False
    assert dec["is_repost"] is False


def test_chain_verdict_is_most_favorable_member(conn):
    # Repeat evals of one role are noisy; the chain reduces to the MOST FAVORABLE verdict
    # (PASS > RECRUITER_ONLY > GATE_FAIL) so a false GATE_FAIL can't bury a role.
    make_job(conn, job_url="c", verdict="GATE_FAIL")
    make_job(conn, job_url="r1", repost_of="c", verdict="RECRUITER_ONLY")
    relist = make_job(conn, job_url="r2", repost_of="c", status="repost_evaluated", verdict=None)
    dec = chain.effective_decision(conn, relist)
    assert dec["chain_verdict"] == "RECRUITER_ONLY"
    # A PASS anywhere in the chain wins outright.
    make_job(conn, job_url="r3", repost_of="c", verdict="PASS")
    dec = chain.effective_decision(conn, relist)
    assert dec["chain_verdict"] == "PASS"
    # original_verdict (the canonical's own) is untouched by the aggregation.
    assert dec["original_verdict"] == "GATE_FAIL"


def test_chain_verdict_none_for_unevaluated_chain(conn):
    canon = make_job(conn, job_url="c", status="new", verdict=None)
    assert chain.effective_decision(conn, canon)["chain_verdict"] is None


def test_effective_decisions_batched_carries_chain_verdict(conn):
    # The batched variant shares _effective_from_members — chain_verdict must match.
    make_job(conn, job_url="c", verdict="PASS")
    relist = make_job(conn, job_url="r1", repost_of="c", status="repost_evaluated", verdict=None)
    decs = chain.effective_decisions(conn, [relist])
    assert decs["r1"]["chain_verdict"] == "PASS"


def test_effective_decision_reports_chain_reject(conn):
    make_job(conn, job_url="c", filter_source="manual", filter_gate="work_auth",
             filter_date="2026-06-01")
    relist = make_job(conn, job_url="r1", repost_of="c")
    dec = chain.effective_decision(conn, relist)
    assert dec["reject"] is True
    assert dec["filter_gate"] == "work_auth"


# ----- _dupe_resolve guards ----------------------------------------------------

def test_dupe_resolve_picks_earliest_first_seen_as_canonical(conn):
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00")
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00")
    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    assert plan["winner"]["job_url"] == "early"
    assert plan["loser"]["job_url"] == "late"


def test_dupe_resolve_rejects_same_role(conn):
    make_job(conn, job_url="c")
    make_job(conn, job_url="r1", repost_of="c")
    plan, err = chain.dupe_resolve(conn, "c", "r1")
    assert plan is None
    assert "already the same role" in err


def test_dupe_resolve_blocks_conflicting_decisions(conn):
    make_job(conn, job_url="a", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="b", app_status="passed", status_date="2026-06-02")
    plan, err = chain.dupe_resolve(conn, "a", "b")
    assert plan is None
    assert "decided differently" in err


# ----- _dupe_commit / _dupe_unlink round trip ----------------------------------

def test_dupe_commit_links_and_propagates_then_unlink_restores(conn):
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00",
             app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00", app_status=None)

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)

    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["repost_of"] == "early"
    assert late["repost_source"] == "manual"
    # The winner's decision propagated onto the newly-linked loser.
    assert late["app_status"] == "applied"

    # Undo splits them back into independent chains.
    ok, msg, _ = chain.dupe_unlink(conn, late)
    assert ok
    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["repost_of"] is None
    assert late["repost_source"] is None


def test_dupe_commit_defers_eval_skip_to_run_and_unlink_restores(conn):
    # POLICY: linking a still-'new' posting into an evaluated (undecided) chain does NOT
    # eval-skip it inline — the evaluated-FORWARD direction lives only in `run`'s post-filter
    # phase, so the row re-faces the current rules before any label spares it the eval
    # (the restore-before-filters contract; see _reconcile_chain_skips).
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00",
             status="evaluated", verdict="PASS", app_status=None)
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00",
             status="new", verdict=None, app_status=None)

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)
    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["status"] == "new"          # deferred: run's forward pass labels it
    chain.skip_evaluated_reposts(conn, restore=False)   # the run's post-filter phase
    assert job_status(conn, "late") == "repost_evaluated"
    late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert late["verdict"] is None          # read through the chain, never copied

    ok, _, _ = chain.dupe_unlink(conn, late)
    assert ok
    assert job_status(conn, "late") == "new"  # its own chain again → needs its own eval


def test_dupe_commit_skips_new_member_of_decided_chain_inline(conn):
    # A DECIDED merge skips pending members immediately: decided rows never reach the eval,
    # so no filter re-facing is owed and the label must not wait for the next run.
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00",
             status="evaluated", verdict="PASS", app_status="applied",
             status_date="2026-06-02")
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00",
             status="new", verdict=None, app_status=None)
    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)
    assert job_status(conn, "late") == "repost_decided"


def test_dupe_merge_propagates_reject_without_clobbering_rule(conn):
    # Merge a rejected chain into an undecided one: the un-attributed members get 'manual', but a
    # member already auto-failed by a filters.yaml rule keeps its 'rule:<name>' (overwrite_manual=False).
    make_job(conn, job_url="early", first_seen="2026-06-01T00:00:00",
             filter_source="manual", filter_gate="work_auth", filter_date="2026-06-01")
    make_job(conn, job_url="late", first_seen="2026-06-05T00:00:00", filter_source=None)
    make_job(conn, job_url="late_rule", repost_of="late", filter_source="rule:clearance",
             filter_gate="work_auth")

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)

    rows = {r["job_url"]: r["filter_source"]
            for r in conn.execute("SELECT job_url, filter_source FROM jobs")}
    assert rows["late"] == "manual"            # was un-attributed → filled in by the merge
    assert rows["late_rule"] == "rule:clearance"  # rule attribution left intact
