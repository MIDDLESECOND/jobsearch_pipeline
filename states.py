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
    evaluate_new_jobs          remaining NEW       -> EVALUATED | NEEDS_MANUAL | ERROR
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
STATUSES = (STATUS_NEW, STATUS_EVALUATED, STATUS_NEEDS_MANUAL, STATUS_SALARY_FILTERED,
            STATUS_RULE_FILTERED, STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED,
            STATUS_ERROR)

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
