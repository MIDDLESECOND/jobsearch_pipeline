#!/usr/bin/env python3
"""The pipeline's status/verdict vocabulary — the ONE place these enums are defined.

Every stage of `run` gates on the `status` column, so a typo'd status string is a row no
stage ever picks up again (it silently leaves the pipeline). These constants exist so that
class of bug is an ImportError/pyflakes hit instead. New DBs also get CHECK constraints
built from these tuples (core._jobs_table_sql); a pre-CHECK DB is covered by these
code-side constants alone, and an existing DB whose baked-in CHECK falls BEHIND these
tuples is rebuilt once at startup (core._rebuild_for_stale_checks — a stale CHECK doesn't
just under-enforce, it REJECTS newly-added legal values and aborts every run). Growing a
tuple here therefore triggers a one-shot, row-preserving table swap on CHECK-bearing DBs.

The `status` state machine (who sets what — the `run` stage order in pipeline.py is the
authoritative sequence):

    fetchers (fetch.py)        insert rows as         NEW
    requeue_error_rows         last run's ERROR    -> NEW  (retry; runs BEFORE the filters so a
                               requeued row re-faces the current rules and chain decisions)
    apply_salary_filter        NEW below floor     -> SALARY_FILTERED
    apply_hard_filters         NEW hits a rule     -> RULE_FILTERED   (+ verdict=GATE_FAIL)
    skip_decided_reposts       NEW or REPOST_EVALUATED
                               member of a decided
                               chain               -> REPOST_DECIDED  (reversed on undo)
    skip_evaluated_reposts     NEW member of an
                               evaluated chain     -> REPOST_EVALUATED (reversed on unlink while
                               undecided; details in chain.skip_evaluated_reposts)
    apply_age_valve            NEW first seen more
                               than settings.
                               eval_max_age_days ago -> AGED_OUT  (terminal; corpus mode — runs
                               AFTER the forward skips on purpose, see filters.apply_age_valve)
    evaluate_new_jobs          remaining NEW       -> EVALUATED | NEEDS_MANUAL | ERROR
                               (skipped entirely under settings.evaluate: false)
    reject (manual override)   a never-evaluated row
                               (NEW or REPOST_EVALUATED) -> RULE_FILTERED (undone if never evaluated)

Direction split and stage placement of the two skip passes: see pipeline.py `run`.

A new pre-eval filter must mirror the existing ones: set a non-NEW status so the paid eval
skips the row. Imports nothing — the leaf under chain.py in the module DAG.

Two adjacent columns are deliberately NOT constant-ized: `app_status` (NULL | 'applied' |
'passed' — the user's decision, also spelled out in the UI's JS) and `filter_source`
(NULL | 'manual' | 'rule:<name>' — a tagged value, not an enum). The same policy extends
to the post-application outcome vocabulary below (`app_events.event_type` and its cached
`jobs.outcome_status`): they are user-decision values no `run` stage gates on, so they get
NO schema CHECK — a CHECK there would be a second frozen-CHECK liability that
core._rebuild_for_stale_checks (jobs-only) doesn't cover. Enforcement is code-side, in
chain.record_event's validation against ALL_EVENTS (the same shape as reject_posting
validating against GATE_NAMES).
"""

STATUS_NEW = "new"
STATUS_EVALUATED = "evaluated"
STATUS_NEEDS_MANUAL = "needs_manual"
STATUS_SALARY_FILTERED = "salary_filtered"
STATUS_RULE_FILTERED = "rule_filtered"
STATUS_REPOST_DECIDED = "repost_decided"
STATUS_REPOST_EVALUATED = "repost_evaluated"
STATUS_ERROR = "error"
# Corpus-mode valve (filters.apply_age_valve, 2026-09-09): a 'new' row first seen more than
# settings.eval_max_age_days ago when the eval stage is reached is aged out of the paid eval
# for good. Terminal — no stage reads it back; the row stays as fetched evidence only.
STATUS_AGED_OUT = "aged_out"
STATUSES = (STATUS_NEW, STATUS_EVALUATED, STATUS_NEEDS_MANUAL, STATUS_SALARY_FILTERED,
            STATUS_RULE_FILTERED, STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED,
            STATUS_ERROR, STATUS_AGED_OUT)

VERDICT_PASS = "PASS"
VERDICT_GATE_FAIL = "GATE_FAIL"
VERDICT_RECRUITER_ONLY = "RECRUITER_ONLY"
# Favor-ranking for reducing a repost chain's several (noisy) verdicts to one: most
# favorable wins. The eval is a cheap pre-filter in front of a human — a false PASS costs
# seconds of manual triage, a false GATE_FAIL silently buries a role — so with a noisy
# judge the tie breaks toward showing the posting. max() over the set is also
# order-independent, unlike "canonical's verdict" or "latest verdict".
VERDICT_FAVOR = {VERDICT_PASS: 2, VERDICT_RECRUITER_ONLY: 1, VERDICT_GATE_FAIL: 0}
# Derived, not re-enumerated: a verdict added here but absent from VERDICT_FAVOR would be
# silently dropped by chain_verdict's `in VERDICT_FAVOR` filter — one list, one owner.
VERDICTS = list(VERDICT_FAVOR)

# The two fit-score action bars, code-owned so every consumer reads the same lines.
#
# CORRECTION 2026-08-22 — the previous comment here read "COLD_APPLY_MIN_FIT is the
# guide's PASS action bar ('apply at 13+')" and that is NOT what the guide says. The
# guide's standing allocation has read `fit >= 15, posted <= 14 days` since 2026-08-07
# (briefly 14 that day, restored the same evening); 13 appears nowhere in it as an apply
# line. The stale sentence was believed and propagated into three documents in one
# session before anyone re-read the guide, which is why the correction is recorded here
# rather than quietly deleted: a comment that names another file's rule is a claim, and
# this one had silently gone false.
#
# What each constant actually is:
#  - COLD_APPLY_MIN_FIT (13) is a CODE-OWNED lower action line, not a guide quote. It is
#    the floor of evaluation.ARBITRATION_BAND's action-relevant span, the zone floor the
#    doorbell counts from, and the deepdive batch's admission threshold for re-reading a
#    row (notify_deepdive_batch.BATCH_MIN_FIT aliases it). Reading is not applying.
#  - RECRUITER_ROUTE_MIN_FIT (15) is the recruiter-route bar (workflow's default) AND the
#    number the guide's standing allocation uses for cold applies. The same value for two
#    reasons; second_judge.PASS_MIN_FIT keeps its own literal for exactly this reason.
#
# Standing note on the 15: the guide sets it by a STRICTNESS GAP (bar minus the
# gates-passed fit mean) — historically 4.1, deliberately 4.4 when the bar was restored
# on 2026-08-07 against a scale of ~10.6. Measured 2026-08-22 the gates-passed mean is
# ~9.5 (weekly 9.23-9.56 since 08-03), so that gap is now 5.4-5.8 and the bar is
# materially more conservative than it was set to be; the guide's own rule would put it
# at ~14.0 today. Moving it is a GUIDE edit gated on backtest_v2, not a code change here.
#
# Both are real action lines, which is why classify_disagreement below reads a crossing of
# EITHER as the candidate signal inside an unchanged verdict. Candidate, not verdict: since
# 2026-08-19 a crossing is necessary but no longer sufficient in the DEMOTE direction, which
# must also clear DISAGREEMENT_FIT_MARGIN below.
COLD_APPLY_MIN_FIT = 13
RECRUITER_ROUTE_MIN_FIT = 15

# How far a within-verdict fit drop must exceed the second judge's KNOWN OFFSET before
# it counts as disagreement rather than scale.
#
# Measured 2026-08-19 over the review layer's first week (608 paired opinions, the
# 2026-08-12 cost/yield review): the second judge is not a coin-flip arbiter of the
# primary, it is a consistently LOWER scale — mean Δfit -2.49, median -3, and only 3 of
# 608 rows scored HIGHER at all. Against a raw bar crossing that fired on 78% of the
# main band, i.e. a flag on almost everything, which cannot rank and therefore cannot
# be triaged. 5 = the main band's measured median offset (3) + one action-bar width
# (RECRUITER_ROUTE_MIN_FIT - COLD_APPLY_MIN_FIT = 2); it takes that 78% to 22%.
# Re-measure BOTH numbers before moving this — the offset is a property of the judge
# pair, so a model change on either side invalidates it (same standing rule as
# evaluation.ARBITRATION_BAND).
#
# Deliberately NOT applied to two cases:
#  - Verdict-level moves. Those already fire on only 14% of rows, and a PASS -> RECRUITER_ONLY
#    or -> GATE_FAIL move is a categorical judgment rather than a score difference: there is
#    no offset to subtract from a category. (An earlier version of this comment added "and it
#    is the leg notify_deepdive_batch's `batchable` subquery reads — margining it would
#    silently re-admit downgraded rows to the deepdive batch." That is FALSE and was corrected
#    2026-08-23: `batchable` compares raw `second_opinions.verdict` in SQL and never calls
#    this function, so margining this leg could not have reached it. The two read the same
#    FACT independently; neither guards the other. Kept rather than deleted for the same
#    reason as the COLD_APPLY_MIN_FIT correction above — the wrong claim also reached the
#    2026-08-19 CHANGELOG entry, and a silent deletion leaves that copy unchallenged.)
#  - Promotions. Under a -3 median offset an opinion that scores level or higher has
#    already beaten the offset by existing (3 of 608), so it is informative unmargined.
DISAGREEMENT_FIT_MARGIN = 5


def _fit_band(verdict, fit):
    """Which action bars this (verdict, fit) reading clears — a small ordinal within a
    verdict. PASS climbs both bars; RECRUITER_ONLY acts only at the recruiter bar;
    GATE_FAIL never acts. A missing fit counts as clearing nothing."""
    fit = fit or 0
    if verdict == VERDICT_PASS:
        return (fit >= COLD_APPLY_MIN_FIT) + (fit >= RECRUITER_ROUTE_MIN_FIT)
    if verdict == VERDICT_RECRUITER_ONLY:
        return 1 if fit >= RECRUITER_ROUTE_MIN_FIT else 0
    return 0


def classify_disagreement(row_verdict, row_fit, op_verdict, op_fit):
    """'demote' / 'promote' / None — the ONE definition of "the second judge disagrees
    with the primary", shared by the report section (report._second_opinion_lines) and
    the UI card warning (both via second_judge.opinion_summaries): change it here and
    both surfaces move together. A verdict-level move ranks by VERDICT_FAVOR; within a
    shared verdict, crossing an action bar (_fit_band) counts — but a DEMOTION must
    additionally clear DISAGREEMENT_FIT_MARGIN, because the second judge scores on a
    systematically lower scale and a bar crossing it produces by offset alone is not a
    disagreement (see that constant for the measurement). So RECRUITER_ONLY 16→8 is
    still a demotion and PASS 16→14 is no longer one; promotions and verdict-level
    moves are unmargined."""
    f1 = VERDICT_FAVOR.get(row_verdict, -1)
    f2 = VERDICT_FAVOR.get(op_verdict, -1)
    if f2 != f1:
        return "demote" if f2 < f1 else "promote"
    b1 = _fit_band(row_verdict, row_fit)
    b2 = _fit_band(op_verdict, op_fit)
    if b2 > b1:
        return "promote"
    # `or 0` is _fit_band's own reading of a missing fit — shared here on purpose, so the
    # bar crossing and the margin can never disagree about what None means.
    if b2 < b1 and (row_fit or 0) - (op_fit or 0) >= DISAGREEMENT_FIT_MARGIN:
        return "demote"
    return None


def sql_list(values):
    """The one spelling of a quoted SQL IN-list over a vocabulary (`'a', 'b', ...`) — used by
    the schema CHECKs, the stale-CHECK precheck, and the skip passes' subqueries, so a
    formatting slip (double quotes read as identifiers, a missing separator) can't creep into
    one site unnoticed. Values are trusted module constants, never user input."""
    return ", ".join(f"'{v}'" for v in values)

GATE_NAMES = ["years_floor", "domain_requirement", "role_substance", "tool_requirement",
              "work_auth", "employment_type"]

# The escape hatch: a screen that cannot be cleared but that none of the six named gates
# describes (the guide's unmeetable-stated-qualification rule — a stated experience
# ceiling, an unsatisfiable precondition). Deliberately NOT a member of GATE_NAMES:
# evaluation._write_result uses GATE_NAMES as the "is this one of the canonical six?"
# test and coerces anything else to this value, so adding it there would make the
# coercion a no-op and let a hallucinated gate name reach the DB verbatim.
# GATE_NAMES_WITH_OTHER is the accepted-input vocabulary — what a human or the model may
# SUPPLY — and it lived as a hand-copied `GATE_NAMES + ["other"]` in four modules
# (the reject service core, the CLI's help text, the web UI's picker, and the eval
# output spec) before being named here.
GATE_OTHER = "other"
GATE_NAMES_WITH_OTHER = GATE_NAMES + [GATE_OTHER]

# The role's core DAILY function, as read off the posting (eval output `core_function`).
# A closed vocabulary because it is a code-enforced cap's INPUT: NO_PRECEDENT_FUNCTIONS
# names the functions with zero career precedent, and evaluation.normalize_result caps
# those at RECRUITER_ONLY exactly as it caps ai_artifact_depth == 0 and
# formal_leadership_required — skill adjacency does not substitute for function precedent
# at a cold screen.
#
# Why the model REPORTS a function instead of applying the cap itself: the guide has
# carried a function-precedent rule since 2026-07-25, but phrased as "cap the verdict"
# it asked the model to overturn scoring it had just done, and it complied rarely
# (measured 2026-08-10: 96 of 924 fit>=15 family rows, ~10%; Nolro scored 3/3 on all six
# dimensions with the cap unfired). The two caps that DO hold ask only for a fact.
#
# Membership is about EXTERNAL customer ownership, not difficulty or seniority.
# CONSULTING_DELIVERY sits deliberately OUTSIDE the capped set — Big 4 / SI delivery is
# an active target track — and INTERNAL_BUILD is the precedent seat itself. Widening the
# capped set is a judgment change: it belongs in CHANGELOG.md with the evidence.
FUNCTION_PRESALES_DEMO = "presales_demo"
FUNCTION_POST_SALES_DELIVERY = "post_sales_delivery"
FUNCTION_QUOTA_CARRYING = "quota_carrying"
FUNCTION_PEOPLE_MANAGEMENT = "people_management"
FUNCTION_CONSULTING_DELIVERY = "consulting_delivery"
FUNCTION_INTERNAL_BUILD = "internal_build"
FUNCTION_OTHER = "other"
ALL_CORE_FUNCTIONS = (FUNCTION_PRESALES_DEMO, FUNCTION_POST_SALES_DELIVERY,
                      FUNCTION_QUOTA_CARRYING, FUNCTION_PEOPLE_MANAGEMENT,
                      FUNCTION_CONSULTING_DELIVERY, FUNCTION_INTERNAL_BUILD,
                      FUNCTION_OTHER)
NO_PRECEDENT_FUNCTIONS = (FUNCTION_PRESALES_DEMO, FUNCTION_POST_SALES_DELIVERY,
                          FUNCTION_QUOTA_CARRYING, FUNCTION_PEOPLE_MANAGEMENT)

# Post-application outcome events (app_events.event_type). APP_EVENTS are the lifecycle
# transitions — recording one requires the chain to be applied, and the LATEST one (by
# event_date, insertion-order tiebreak) is cached chain-wide as jobs.outcome_status /
# outcome_date (chain._recompute_outcome, the one cache writer). 'interview' is repeatable
# (rounds). EVENT_FOLLOWUP_SENT is applied-only history but deliberately outside APP_EVENTS:
# sending a message advances follow-up cadence without claiming the employer responded.
# EVENT_NOTE is outside APPLIED_ONLY_EVENTS: it attaches free text to any posting.
# No schema CHECK on any of these — see the module docstring.
EVENT_RECRUITER_SCREEN = "recruiter_screen"
EVENT_INTERVIEW = "interview"
EVENT_OFFER = "offer"
EVENT_REJECTED_BY_EMPLOYER = "rejected_by_employer"
EVENT_GHOSTED = "ghosted"
EVENT_WITHDREW = "withdrew"
APP_EVENTS = (EVENT_RECRUITER_SCREEN, EVENT_INTERVIEW, EVENT_OFFER,
              EVENT_REJECTED_BY_EMPLOYER, EVENT_GHOSTED, EVENT_WITHDREW)
EVENT_FOLLOWUP_SENT = "followup_sent"
APPLIED_ONLY_EVENTS = APP_EVENTS + (EVENT_FOLLOWUP_SENT,)
EVENT_NOTE = "note"
ALL_EVENTS = APPLIED_ONLY_EVENTS + (EVENT_NOTE,)

# Application channel (jobs.channel): HOW the application went out — the conversion-analysis
# axis (direct cold-apply vs staffing agency vs referral convert at very different rates, so
# an aggregate response rate over all three is meaningless). Applied-only, propagated
# chain-wide exactly like resume_variant (chain.propagate_app_status / set_channel). Closed
# vocabulary, unlike resume_variant's free text — per-user spellings ("agent", "recruiter",
# "staffing") would split the funnel counts this field exists to make comparable. No schema
# CHECK — same user-decision-vocabulary policy as ALL_EVENTS above; enforced code-side in
# chain (mark_posting / set_channel).
CHANNEL_DIRECT = "direct"
CHANNEL_AGENCY = "agency"
CHANNEL_REFERRAL = "referral"
ALL_CHANNELS = (CHANNEL_DIRECT, CHANNEL_AGENCY, CHANNEL_REFERRAL)
# (No SCORE_DIMS constant: the score dimensions live in the eval prompt's output spec and
# the stored eval_json; the report/UI render whatever keys exist, so a code-side list would
# only drift.)
