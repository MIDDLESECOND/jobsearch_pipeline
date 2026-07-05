#!/usr/bin/env python3
"""The pipeline's status/verdict vocabulary — the ONE place these enums are defined.

Every stage of `run` gates on the `status` column, so a typo'd status string is a row no
stage ever picks up again (it silently leaves the pipeline). These constants exist so that
class of bug is an ImportError/pyflakes hit instead. New DBs also get CHECK constraints
built from these tuples (see core.get_db); existing DBs are not rebuilt — the constants in
code are the enforcement that covers both.

The `status` state machine (who sets what — the `run` stage order in pipeline.py is the
authoritative sequence):

    fetchers (fetch.py)        insert rows as         NEW
    requeue_error_rows         last run's ERROR    -> NEW  (retry; runs BEFORE the filters so a
                               requeued row re-faces the current rules and chain decisions)
    apply_salary_filter        NEW below floor     -> SALARY_FILTERED
    apply_hard_filters         NEW hits a rule     -> RULE_FILTERED   (+ verdict=GATE_FAIL)
    skip_decided_reposts       NEW relisting of a
                               decided chain       -> REPOST_DECIDED  (reversed on undo)
    evaluate_new_jobs          remaining NEW       -> EVALUATED | NEEDS_MANUAL | ERROR
    reject (manual override)   a still-NEW row     -> RULE_FILTERED   (undone if never evaluated)

A new pre-eval filter must mirror the existing ones: set a non-NEW status so the paid eval
skips the row. Imports nothing — the leaf under chain.py in the module DAG.

Two adjacent columns are deliberately NOT constant-ized: `app_status` (NULL | 'applied' |
'passed' — the user's decision, also spelled out in the UI's JS) and `filter_source`
(NULL | 'manual' | 'rule:<name>' — a tagged value, not an enum).
"""

STATUS_NEW = "new"
STATUS_EVALUATED = "evaluated"
STATUS_NEEDS_MANUAL = "needs_manual"
STATUS_SALARY_FILTERED = "salary_filtered"
STATUS_RULE_FILTERED = "rule_filtered"
STATUS_REPOST_DECIDED = "repost_decided"
STATUS_ERROR = "error"
STATUSES = (STATUS_NEW, STATUS_EVALUATED, STATUS_NEEDS_MANUAL, STATUS_SALARY_FILTERED,
            STATUS_RULE_FILTERED, STATUS_REPOST_DECIDED, STATUS_ERROR)

VERDICT_PASS = "PASS"
VERDICT_GATE_FAIL = "GATE_FAIL"
VERDICT_RECRUITER_ONLY = "RECRUITER_ONLY"
VERDICTS = [VERDICT_PASS, VERDICT_GATE_FAIL, VERDICT_RECRUITER_ONLY]

GATE_NAMES = ["years_floor", "domain_requirement", "role_substance", "tool_requirement",
              "work_auth", "employment_type"]
# (No SCORE_DIMS constant: the score dimensions live in the eval prompt's output spec and
# the stored eval_json; the report/UI render whatever keys exist, so a code-side list would
# only drift.)
