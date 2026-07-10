"""Post-application outcome tracking — the chain.record_event / undo_event / chain_events /
set_resume / set_channel cores and the _recompute_outcome cache they maintain.

The invariants pinned here:
  * an event is written ONCE, keyed to the chain's canonical at write time, and read
    chain-wide — so histories union across dupe merges and survive unlinks;
  * the cached jobs.outcome_status/outcome_date columns are a pure recompute of
    (chain applied?, events): latest non-note event wins, cleared while not applied,
    restored on re-apply (history is never destroyed by a decision toggle);
  * lifecycle events require the chain applied; bare notes don't;
  * the follow-up predicate (applied + outcome NULL + old status_date) is answerable in
    pure SQL — the schema contract the future funnel view builds on.
"""

from datetime import date

import chain
from conftest import make_job
from states import APP_EVENTS, ALL_EVENTS, EVENT_NOTE

TODAY = date.today().isoformat()


def _events(conn, url):
    row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (url,)).fetchone()
    return [(e["event_type"], e["event_date"]) for e in chain.chain_events(conn, row)]


def _outcome(conn, url):
    return tuple(conn.execute(
        "SELECT outcome_status, outcome_date FROM jobs WHERE job_url=?", (url,)
    ).fetchone())


def test_lifecycle_event_requires_applied_chain(conn):
    row = make_job(conn, job_url="u1")
    ok, msg, affected, _ = chain.record_event(conn, row, "interview")
    assert not ok and "applied" in msg and affected == []
    assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0
    # ...and a passed chain is not an applied chain.
    row = make_job(conn, job_url="u2", app_status="passed", status_date=TODAY)
    ok, msg, _, _ = chain.record_event(conn, row, "offer")
    assert not ok and "passed" in msg


def test_unknown_event_type_and_dateless_note_refused(conn):
    row = make_job(conn, job_url="u1", app_status="applied", status_date=TODAY)
    ok, msg, _, _ = chain.record_event(conn, row, "promoted_to_ceo")
    assert not ok and "event type" in msg
    ok, msg, _, _ = chain.record_event(conn, row, EVENT_NOTE)          # note without text
    assert not ok and "note text" in msg
    ok, msg, _, _ = chain.record_event(conn, row, "interview", "07/04/2026")
    assert not ok and "YYYY-MM-DD" in msg


def test_event_lands_on_canonical_and_outcome_propagates_chainwide(conn):
    make_job(conn, job_url="canon", company="Chain Co", app_status="applied",
             status_date="2026-06-20")
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon",
                      app_status="applied", status_date="2026-06-20")
    ok, _, affected, exempt = chain.record_event(conn, relist, "interview", "2026-07-01")
    assert ok and set(affected) == {"canon", "relist"} and exempt == ["relist"]
    # Written once, keyed to the CANONICAL (recorded from the relisting's card).
    rows = conn.execute("SELECT job_url FROM app_events").fetchall()
    assert [r["job_url"] for r in rows] == ["canon"]
    # The cache reaches every member, and effective_decision reads it off a sibling.
    assert _outcome(conn, "canon") == ("interview", "2026-07-01")
    assert _outcome(conn, "relist") == ("interview", "2026-07-01")
    dec = chain.effective_decision(conn, relist)
    assert dec["outcome_status"] == "interview" and dec["outcome_date"] == "2026-07-01"


def test_interview_rounds_repeat_and_latest_event_wins(conn):
    row = make_job(conn, job_url="u1", app_status="applied", status_date="2026-06-01")
    chain.record_event(conn, row, "recruiter_screen", "2026-06-05")
    chain.record_event(conn, row, "interview", "2026-06-12")
    chain.record_event(conn, row, "interview", "2026-06-19")   # round 2 — both persist
    assert _events(conn, "u1") == [("recruiter_screen", "2026-06-05"),
                                   ("interview", "2026-06-12"), ("interview", "2026-06-19")]
    assert _outcome(conn, "u1") == ("interview", "2026-06-19")
    # Same-day events break the tie by insertion order (id).
    chain.record_event(conn, row, "offer", "2026-06-19")
    assert _outcome(conn, "u1") == ("offer", "2026-06-19")
    # A backdated correction never outranks the latest event_date.
    chain.record_event(conn, row, "recruiter_screen", "2026-06-06")
    assert _outcome(conn, "u1") == ("offer", "2026-06-19")


def test_note_event_attaches_anywhere_and_never_sets_outcome(conn):
    row = make_job(conn, job_url="u1")                          # undecided — notes still allowed
    ok, _, _, _ = chain.record_event(conn, row, EVENT_NOTE, note="referred by J.")
    assert ok
    assert _outcome(conn, "u1") == (None, None)                 # not applied -> no cache either way
    row = make_job(conn, job_url="u2", app_status="applied", status_date=TODAY)
    chain.record_event(conn, row, "interview", "2026-06-12")
    chain.record_event(conn, row, EVENT_NOTE, "2026-06-30", note="sent follow-up")
    assert _outcome(conn, "u2") == ("interview", "2026-06-12")  # the later note didn't win


def test_undo_event_removes_last_recorded_and_recomputes(conn):
    row = make_job(conn, job_url="u1", app_status="applied", status_date="2026-06-01")
    chain.record_event(conn, row, "interview", "2026-06-12")
    chain.record_event(conn, row, "offer", "2026-06-20")
    ok, msg, _, _ = chain.undo_event(conn, row)
    assert ok and "offer" in msg
    assert _outcome(conn, "u1") == ("interview", "2026-06-12")  # cache stepped back
    chain.undo_event(conn, row)
    assert _outcome(conn, "u1") == (None, None)
    ok, msg, _, _ = chain.undo_event(conn, row)                 # nothing left
    assert not ok and "no events" in msg


def test_undo_applied_clears_cache_keeps_history_reapply_restores(conn):
    row = make_job(conn, job_url="u1", app_status="applied", status_date="2026-06-01")
    chain.record_event(conn, row, "interview", "2026-06-12")
    ok, msg, _, _ = chain.mark_posting(conn, row, None)         # undo the decision
    assert ok and "history kept" in msg
    assert _outcome(conn, "u1") == (None, None)                 # cache cleared...
    assert len(_events(conn, "u1")) == 1                        # ...history intact
    chain.mark_posting(conn, row, "applied")                    # re-apply
    assert _outcome(conn, "u1") == ("interview", "2026-06-12")  # recomputed right back


def test_resume_variant_set_at_apply_time_and_edited_after(conn):
    make_job(conn, job_url="canon", company="Chain Co")
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon")
    chain.mark_posting(conn, relist, "applied", "variant-B")
    got = {r["job_url"]: r["resume_variant"]
           for r in conn.execute("SELECT job_url, resume_variant FROM jobs")}
    assert got == {"canon": "variant-B", "relist": "variant-B"}  # propagated chain-wide
    # Edit after the fact; empty clears.
    ok, _, _, _ = chain.set_resume(conn, relist, "variant-C")
    assert ok
    assert chain.effective_decision(conn, relist)["resume_variant"] == "variant-C"
    chain.set_resume(conn, relist, "  ")
    assert chain.effective_decision(conn, relist)["resume_variant"] is None
    # Undo clears it with the decision; a decision-less chain refuses set_resume.
    chain.mark_posting(conn, relist, None)
    assert chain.effective_decision(conn, relist)["resume_variant"] is None
    ok, msg, _, _ = chain.set_resume(conn, relist, "variant-D")
    assert not ok and "applied" in msg


def test_reassert_applied_without_resume_keeps_stored_variant(conn):
    # propagate_app_status only writes resume_variant when given one (or on undo) — a CLI
    # re-run of `applied` without --resume must not blank the stored value.
    row = make_job(conn, job_url="u1")
    chain.mark_posting(conn, row, "applied", "variant-B")
    chain.mark_posting(conn, row, "applied")
    assert chain.effective_decision(conn, row)["resume_variant"] == "variant-B"


def test_resume_variant_never_rides_a_non_applied_chain(conn):
    # resume_variant is an applied-only field. A 'passed' mark with a variant must not store
    # it (it would be invisible and un-clearable: the UI renders the field only on applied
    # cards and set_resume refuses non-applied chains)...
    row = make_job(conn, job_url="u1")
    chain.mark_posting(conn, row, "passed", "variant-B")
    assert conn.execute("SELECT resume_variant FROM jobs WHERE job_url='u1'").fetchone()[0] is None
    # ...and a direct applied→passed switch clears a stored one, matching the UI mirror
    # (patchJob nulls it on 'passed') so DB and card can't diverge.
    row2 = make_job(conn, job_url="u2")
    chain.mark_posting(conn, row2, "applied", "variant-B")
    chain.mark_posting(conn, row2, "passed")
    assert conn.execute("SELECT resume_variant FROM jobs WHERE job_url='u2'").fetchone()[0] is None


def test_reassert_applied_stamps_late_relisting_with_chain_variant(conn):
    # A relisting fetched AFTER the original apply is deliberately never stamped. A later
    # re-assert of 'applied' without a variant must stamp it WITH the chain's stored variant
    # — not applied-with-NULL beside 'variant-B' (a mixed chain makes _decide's read
    # SQL-row-order-dependent, the exact state the uniform-write rule forbids).
    make_job(conn, job_url="canon", company="Chain Co")
    canon = conn.execute("SELECT * FROM jobs WHERE job_url='canon'").fetchone()
    chain.mark_posting(conn, canon, "applied", "variant-B")
    relist = make_job(conn, job_url="late", company="Chain Co", repost_of="canon")  # unstamped
    chain.mark_posting(conn, relist, "applied")            # re-assert, no --resume
    got = {r["job_url"]: r["resume_variant"]
           for r in conn.execute("SELECT job_url, resume_variant FROM jobs")}
    assert got == {"canon": "variant-B", "late": "variant-B"}


def test_blank_or_whitespace_resume_reads_as_not_given(conn):
    # An explicit '' / whitespace --resume must neither blank a stored variant (empty is
    # "not given", set_resume is the clear path) nor be stored verbatim (an invisible
    # variant would also beat a real one in the merge coalesce).
    row = make_job(conn, job_url="u1")
    chain.mark_posting(conn, row, "applied", "variant-B")
    chain.mark_posting(conn, row, "applied", "")
    assert chain.effective_decision(conn, row)["resume_variant"] == "variant-B"
    row2 = make_job(conn, job_url="u2")
    chain.mark_posting(conn, row2, "applied", "   ")
    assert conn.execute("SELECT resume_variant FROM jobs WHERE job_url='u2'").fetchone()[0] is None


def test_undo_message_promises_restore_only_for_lifecycle_events(conn):
    # A notes-only history restores no outcome — the undo message must not claim it does.
    row = make_job(conn, job_url="u1", app_status="applied", status_date=TODAY)
    chain.record_event(conn, row, EVENT_NOTE, note="referred by J.")
    ok, msg, _, _ = chain.mark_posting(conn, row, None)
    assert ok and "restores" not in msg
    row2 = make_job(conn, job_url="u2", app_status="applied", status_date=TODAY)
    chain.record_event(conn, row2, "interview", "2026-06-12")
    ok, msg, _, _ = chain.mark_posting(conn, row2, None)
    assert ok and "re-applying restores it" in msg


def test_unlink_warning_fires_for_events_keyed_to_former_canonical_member(conn):
    # Events keyed to a chain that was ITSELF merged in earlier sit on a former-canonical
    # MEMBER url of the kept chain — the unlink warning must count over the kept chain's
    # full membership, not just its canonical, or it silently skips its own scenario.
    make_job(conn, job_url="w", company="W Co", first_seen="2026-04-01T09:00:00")
    x = make_job(conn, job_url="x", company="X Co", first_seen="2026-05-01T09:00:00",
                 app_status="applied", status_date="2026-06-01")
    chain.record_event(conn, x, "interview", "2026-06-12")   # keyed to canonical 'x'
    plan, err = chain.dupe_resolve(conn, "x", "w")            # 'w' earlier -> wins
    assert err is None
    chain.dupe_commit(conn, plan)                             # events stay keyed to 'x'
    make_job(conn, job_url="y", company="Y Co", first_seen="2026-06-05T09:00:00")
    plan, err = chain.dupe_resolve(conn, "y", "w")
    assert err is None
    chain.dupe_commit(conn, plan)
    y = conn.execute("SELECT * FROM jobs WHERE job_url='y'").fetchone()
    ok, msg, _, _ = chain.dupe_unlink(conn, y)
    assert ok and "stay with the kept role" in msg


def test_dupe_merge_coalesces_resume_variant_across_sides(conn):
    # The surviving decision's side may have applied WITHOUT a variant while the other side
    # recorded one — the merge must coalesce (winner's preferred, other side's as fallback)
    # and write it chain-wide, never leaving a mixed chain whose _decide read would be
    # SQL-row-order-dependent.
    make_job(conn, job_url="a", company="A Co", first_seen="2026-05-01T09:00:00",
             app_status="applied", status_date="2026-06-01")             # winner: no variant
    make_job(conn, job_url="b", company="B Co", first_seen="2026-06-01T09:00:00",
             app_status="applied", status_date="2026-06-01", resume_variant="variant-B")
    plan, err = chain.dupe_resolve(conn, "a", "b")
    assert err is None
    chain.dupe_commit(conn, plan)
    got = {r["job_url"]: r["resume_variant"]
           for r in conn.execute("SELECT job_url, resume_variant FROM jobs")}
    assert got == {"a": "variant-B", "b": "variant-B"}   # uniform, loser's variant survived


def test_event_date_sanity_window(conn):
    # A future-dated typo would permanently pin the outcome cache (latest event_date wins,
    # and undo works in insertion order), so record_event bounds the date to 2000-01-01..today.
    row = make_job(conn, job_url="u1", app_status="applied", status_date=TODAY)
    for bad in ("2062-06-12", "1999-12-31"):
        ok, msg, _, _ = chain.record_event(conn, row, "interview", bad)
        assert not ok and "2000-01-01..today" in msg
    assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0
    ok, _, _, _ = chain.record_event(conn, row, "interview", TODAY)     # today itself is fine
    assert ok


def test_dupe_merge_unions_histories_and_outcome_differences_never_block(conn):
    # Two applied chains for the same role, each with its own history — the merge must union
    # them (latest event wins the cache), not refuse over differing outcomes.
    a = make_job(conn, job_url="a", company="A Co", first_seen="2026-05-01T09:00:00",
                 app_status="applied", status_date="2026-06-01")
    b = make_job(conn, job_url="b", company="B Co", first_seen="2026-06-01T09:00:00",
                 app_status="applied", status_date="2026-06-01")
    chain.record_event(conn, a, "recruiter_screen", "2026-06-05")
    chain.record_event(conn, b, "interview", "2026-06-12")
    plan, err = chain.dupe_resolve(conn, "a", "b")
    assert err is None                                          # outcomes differ, merge allowed
    chain.dupe_commit(conn, plan)
    merged = conn.execute("SELECT * FROM jobs WHERE job_url='b'").fetchone()
    assert merged["repost_of"] == "a"
    assert _events(conn, "b") == [("recruiter_screen", "2026-06-05"),
                                  ("interview", "2026-06-12")]  # union, chain-wide read
    assert _outcome(conn, "a") == ("interview", "2026-06-12")   # latest across both sides
    assert _outcome(conn, "b") == ("interview", "2026-06-12")

    # Unlink leaves event rows where they were written (no data migration either way) but
    # recomputes each side's cache from its OWN events — neither chain may keep showing the
    # other side's outcome.
    b = conn.execute("SELECT * FROM jobs WHERE job_url='b'").fetchone()
    ok, _, _, _ = chain.dupe_unlink(conn, b)
    assert ok
    keyed = {r["job_url"] for r in conn.execute("SELECT job_url FROM app_events")}
    assert keyed == {"a", "b"}                                  # each stayed on its canonical
    assert _outcome(conn, "a") == ("recruiter_screen", "2026-06-05")
    assert _outcome(conn, "b") == ("interview", "2026-06-12")


def test_followup_predicate_is_pure_sql(conn):
    # The schema contract for the future funnel/follow-up view: "applied, no response, older
    # than N days" must be answerable without joins. Seed one of each adjacent case.
    make_job(conn, job_url="due", app_status="applied", status_date="2026-06-01")
    make_job(conn, job_url="fresh", app_status="applied", status_date="2026-06-25")
    make_job(conn, job_url="answered", app_status="applied", status_date="2026-06-01",
             outcome_status="interview", outcome_date="2026-06-10")
    make_job(conn, job_url="not_applied")
    due = {r["job_url"] for r in conn.execute(
        "SELECT job_url FROM jobs WHERE app_status='applied' "
        "AND outcome_status IS NULL AND status_date < ?", ("2026-06-15",))}
    assert due == {"due"}


def test_vocabulary_shape(conn):
    # The UI select and CLI choices derive from these; 'note' must stay out of the
    # lifecycle set (it never sets an outcome) but inside the recordable set.
    assert EVENT_NOTE not in APP_EVENTS
    assert set(APP_EVENTS) | {EVENT_NOTE} == set(ALL_EVENTS)


# --------------------------------------------------------------- channel (set_channel &
# propagation) — resume_variant's sibling field, same applied-only chain-wide contract,
# plus the closed-vocabulary validation resume's free text doesn't have.

def test_channel_set_at_apply_time_and_edited_after(conn):
    make_job(conn, job_url="canon", company="Chain Co")
    relist = make_job(conn, job_url="relist", company="Chain Co", repost_of="canon")
    chain.mark_posting(conn, relist, "applied", channel="direct")
    got = {r["job_url"]: r["channel"]
           for r in conn.execute("SELECT job_url, channel FROM jobs")}
    assert got == {"canon": "direct", "relist": "direct"}   # propagated chain-wide
    # Edit after the fact; empty clears.
    ok, _, _, _ = chain.set_channel(conn, relist, "agency")
    assert ok
    assert chain.effective_decision(conn, relist)["channel"] == "agency"
    chain.set_channel(conn, relist, "  ")
    assert chain.effective_decision(conn, relist)["channel"] is None
    # Undo clears it with the decision; a decision-less chain refuses set_channel.
    chain.mark_posting(conn, relist, None)
    assert chain.effective_decision(conn, relist)["channel"] is None
    ok, msg, _, _ = chain.set_channel(conn, relist, "referral")
    assert not ok and "applied" in msg


def test_channel_validated_against_closed_vocabulary(conn):
    # Unlike resume's free text, channel is a closed vocabulary — a misspelling would split
    # the funnel counts the field exists to make comparable. Refused at apply time (nothing
    # written, not even the app_status) and at edit time.
    row = make_job(conn, job_url="u1")
    ok, msg, _, _ = chain.mark_posting(conn, row, "applied", channel="staffing")
    assert not ok and "channel" in msg
    got = conn.execute("SELECT app_status, channel FROM jobs WHERE job_url='u1'").fetchone()
    assert tuple(got) == (None, None)
    chain.mark_posting(conn, row, "applied", channel="direct")
    ok, msg, _, _ = chain.set_channel(conn, row, "recruiter")
    assert not ok and "channel" in msg
    assert chain.effective_decision(conn, row)["channel"] == "direct"


def test_channel_case_folds_at_every_entry_point(conn):
    # _norm_channel owns the case rule (server-side, not per-front-end): "Direct" through
    # apply time and " REFERRAL " through the edit path both store the canonical spelling —
    # a raw API caller must get the same behavior the UI's lowercasing gives its users.
    row = make_job(conn, job_url="u1")
    ok, _, _, _ = chain.mark_posting(conn, row, "applied", channel="Direct")
    assert ok
    assert chain.effective_decision(conn, row)["channel"] == "direct"
    ok, _, _, _ = chain.set_channel(conn, row, " REFERRAL ")
    assert ok
    assert chain.effective_decision(conn, row)["channel"] == "referral"


def test_reassert_applied_without_channel_keeps_stored_value(conn):
    # Same inherit-on-reassert contract as resume_variant: a re-run of `applied` without
    # --channel must not blank the stored value, and must stamp a late-fetched relisting
    # with the chain's value rather than applied-with-NULL beside it.
    make_job(conn, job_url="canon", company="Chain Co")
    canon = conn.execute("SELECT * FROM jobs WHERE job_url='canon'").fetchone()
    chain.mark_posting(conn, canon, "applied", channel="referral")
    relist = make_job(conn, job_url="late", company="Chain Co", repost_of="canon")
    chain.mark_posting(conn, relist, "applied")             # re-assert, no --channel
    got = {r["job_url"]: r["channel"]
           for r in conn.execute("SELECT job_url, channel FROM jobs")}
    assert got == {"canon": "referral", "late": "referral"}


def test_channel_never_rides_a_non_applied_chain(conn):
    # Applied-only, like resume_variant: a 'passed' mark with a channel must not store it,
    # and an applied→passed switch clears a stored one (matching the UI mirror).
    row = make_job(conn, job_url="u1")
    chain.mark_posting(conn, row, "passed", channel="direct")
    assert conn.execute("SELECT channel FROM jobs WHERE job_url='u1'").fetchone()[0] is None
    row2 = make_job(conn, job_url="u2")
    chain.mark_posting(conn, row2, "applied", channel="direct")
    chain.mark_posting(conn, row2, "passed")
    assert conn.execute("SELECT channel FROM jobs WHERE job_url='u2'").fetchone()[0] is None


def test_dupe_merge_coalesces_channel_across_sides(conn):
    # Same coalesce as the resume variant: winner's preferred, loser's as fallback, written
    # chain-wide — never a mixed chain whose _decide read is SQL-row-order-dependent.
    make_job(conn, job_url="a", company="A Co", first_seen="2026-05-01T09:00:00",
             app_status="applied", status_date="2026-06-01")            # winner: no channel
    make_job(conn, job_url="b", company="B Co", first_seen="2026-06-01T09:00:00",
             app_status="applied", status_date="2026-06-01", channel="agency")
    plan, err = chain.dupe_resolve(conn, "a", "b")
    assert err is None
    chain.dupe_commit(conn, plan)
    got = {r["job_url"]: r["channel"]
           for r in conn.execute("SELECT job_url, channel FROM jobs")}
    assert got == {"a": "agency", "b": "agency"}    # uniform, loser's channel survived
