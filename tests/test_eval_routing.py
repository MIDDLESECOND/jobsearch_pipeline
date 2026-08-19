"""normalize_result — the deterministic routing the model is NOT trusted to do.

The load-bearing rule (the '50/0 fix'): a role that clears the gates but whose
ai_artifact_depth is 0 (or unparseable) is capped to RECRUITER_ONLY / bucket 1,
even at a perfect score. Its sibling (the formal-leadership cap): a required
formal-leadership tenure (`formal_leadership_required: true`) caps the same way,
but fails OPEN on absence — pre-cap eval_json rows lack the key. The third (the
function-precedent cap, 2026-08-10) reads the model's `core_function` extraction
against states.NO_PRECEDENT_FUNCTIONS and also fails open. All three enforced in
code so they can't depend on the model complying — the function cap exists precisely
because its guide-only phrasing ("cap the verdict yourself") fired on ~10% of the
family it names.
"""

import chain
import evaluation
import states
from conftest import make_job, job_status


def _norm(**kw):
    return evaluation.normalize_result(dict(kw))


def _bd(depth):
    return {"ai_applied_vs_research": 3, "ai_artifact_depth": depth,
            "learning_value": 3, "technical_skill_match": 3,
            "title_trajectory": 3, "years_vs_stated": 3}


def test_depth0_caps_pass_to_recruiter_even_at_perfect_score():
    r = _norm(verdict="PASS", fit_score=18, bucket=3, score_breakdown=_bd(0))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_depth3_is_clean_delivery_bucket3():
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3))
    assert r["verdict"] == "PASS"
    assert r["bucket"] == 3


def test_depth2_is_acceptable_tier_bucket2():
    r = _norm(verdict="PASS", fit_score=12, score_breakdown=_bd(2))
    assert r["verdict"] == "PASS"
    assert r["bucket"] == 2


def test_missing_breakdown_fails_closed_to_recruiter():
    # Output spec allows a null breakdown; an unscored depth must NOT slip to bucket 2.
    r = _norm(verdict="PASS", fit_score=16, score_breakdown=None)
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_depth_none_fails_closed():
    r = _norm(verdict="PASS", score_breakdown=_bd(None))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_depth_bool_true_is_not_a_valid_number():
    # isinstance(True, int) is True in Python — the cap must reject bools explicitly.
    r = _norm(verdict="PASS", score_breakdown=_bd(True))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_depth_nan_fails_closed():
    r = _norm(verdict="PASS", score_breakdown=_bd(float("nan")))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_non_dict_breakdown_fails_closed():
    # The model can emit score_breakdown as a non-dict (list/string/number); normalize_result
    # must fail closed, not AttributeError on bd.get() — that throw is outside the eval retry
    # boundary and would abort the whole batch.
    for bad in ([3, 2, 1], "0/3 each", 5):
        r = _norm(verdict="PASS", fit_score=16, score_breakdown=bad)
        assert r["verdict"] == "RECRUITER_ONLY"
        assert r["bucket"] == 1


def test_recruiter_only_input_with_depth0_stays_bucket1():
    r = _norm(verdict="RECRUITER_ONLY", fit_score=14, score_breakdown=_bd(0))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_leadership_requirement_caps_pass_to_recruiter_even_at_perfect_score():
    # The cold-screen sibling of the 50/0 fix (the 17/18 manager-role case): a required
    # formal-leadership tenure is a wall the fit total must not outvote.
    r = _norm(verdict="PASS", fit_score=18, bucket=3, score_breakdown=_bd(3),
              formal_leadership_required=True)
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_leadership_string_true_still_caps():
    # A model quoting the boolean must not dodge the cap.
    r = _norm(verdict="PASS", fit_score=17, score_breakdown=_bd(3),
              formal_leadership_required="true")
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_leadership_noncanonical_affirmatives_still_cap():
    # The cap is judged on the normalized VALUE, not the JSON type: 1 and "yes" are
    # affirmative answers a weaker model plausibly emits, and each must cap — a silent
    # fail-open on an affirmative is the cold-apply miss the cap exists to prevent.
    for aff in (1, "yes", "Yes", " TRUE "):
        r = _norm(verdict="PASS", fit_score=17, score_breakdown=_bd(3),
                  formal_leadership_required=aff)
        assert r["verdict"] == "RECRUITER_ONLY", aff
        assert r["bucket"] == 1


def test_leadership_unrecognized_value_fails_open_but_warns(capsys):
    # Neither a recognized affirmative nor a recognized negative: still fail open (the
    # cap's polarity), but no longer silently — the bypass is logged to stderr.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              formal_leadership_required="preferred")
    assert r["verdict"] == "PASS"
    assert r["bucket"] == 3
    err = capsys.readouterr().err
    assert "formal_leadership_required" in err and "preferred" in err


def test_leadership_absent_or_false_fails_open():
    # Opposite polarity from the depth cap: most roles require no leadership and pre-cap
    # eval_json rows lack the key (backtest re-runs) — absence must NOT bucket-1 the feed.
    for kw in ({}, {"formal_leadership_required": False},
               {"formal_leadership_required": None},
               {"formal_leadership_required": "no"}):
        r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), **kw)
        assert r["verdict"] == "PASS"
        assert r["bucket"] == 3


def test_no_precedent_function_caps_pass_to_recruiter_even_at_perfect_score():
    # The Nolro case (2026-08-10): 3/3 on all six dimensions, depth 3 — i.e. everything
    # the other two caps look at is clean — but the seat's daily job is owning delivery
    # to external paying customers, which has zero career precedent.
    r = _norm(verdict="PASS", fit_score=18, bucket=3, score_breakdown=_bd(3),
              core_function="post_sales_delivery")
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_every_no_precedent_function_caps():
    for fn in ("presales_demo", "post_sales_delivery", "quota_carrying",
               "people_management"):
        r = _norm(verdict="PASS", fit_score=17, score_breakdown=_bd(3), core_function=fn)
        assert r["verdict"] == "RECRUITER_ONLY", fn
        assert r["bucket"] == 1, fn


def test_precedented_functions_do_not_cap():
    # consulting_delivery is deliberately OUTSIDE the capped set (Big 4 / SI engagement
    # delivery is an active target track); internal_build is the precedent seat itself.
    for fn in ("consulting_delivery", "internal_build", "other"):
        r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), core_function=fn)
        assert r["verdict"] == "PASS", fn
        assert r["bucket"] == 3, fn


def test_core_function_absent_fails_open():
    # Same polarity as the leadership cap, same reason: every eval_json written before
    # this field existed lacks the key, and backtest_v2 re-normalizes those stored rows.
    for kw in ({}, {"core_function": None}, {"core_function": ""}):
        r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), **kw)
        assert r["verdict"] == "PASS"
        assert r["bucket"] == 3


def test_core_function_unrecognized_fails_open_warns_and_is_not_guessed(capsys):
    # A closed vocabulary, so an unknown string is normalized to None rather than
    # coerced toward some member — a silent coercion would let one hallucinated value
    # re-route a clean role.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              core_function="customer_facing_delivery")
    assert r["verdict"] == "PASS"
    assert r["bucket"] == 3
    assert r["core_function"] is None
    err = capsys.readouterr().err
    assert "core_function" in err and "customer_facing_delivery" in err


def test_core_function_is_normalized_onto_the_result():
    r = _norm(verdict="PASS", fit_score=18, score_breakdown=_bd(3),
              core_function="  Presales_Demo  ")
    assert r["core_function"] == "presales_demo"
    assert r["verdict"] == "RECRUITER_ONLY"


def test_core_function_non_string_fails_open():
    for bad in (3, True, ["presales_demo"], {"fn": "presales_demo"}):
        r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), core_function=bad)
        assert r["verdict"] == "PASS"
        assert r["core_function"] is None


def test_core_function_empty_string_is_absence_not_a_warning(capsys):
    # "" means the model omitted the field, not that it answered wrongly — warning on it
    # would put a stderr line on every such row. Matches the leadership cap, which lists
    # "" among its recognized negatives.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), core_function="   ")
    assert r["verdict"] == "PASS"
    assert r["core_function"] is None
    assert "core_function" not in capsys.readouterr().err


def test_prompt_vocabulary_cannot_drift_from_states():
    # The failure this guards is SILENT: a prompt offering a value states.py doesn't know
    # means the model emits it, normalize_result fails open, and the cap quietly stops
    # covering that class. The two mechanical lists are interpolated; the per-value
    # descriptions are prose, so assert each member is actually described.
    for fn in states.ALL_CORE_FUNCTIONS:
        assert f'"{fn}"' in evaluation.SYSTEM_TEMPLATE, f"{fn} has no description in the prompt"
    assert set(states.NO_PRECEDENT_FUNCTIONS) <= set(states.ALL_CORE_FUNCTIONS)
    rendered = evaluation.SYSTEM_TEMPLATE.format(
        profile="P", guide="G",
        all_functions=evaluation._quoted(states.ALL_CORE_FUNCTIONS),
        capped_functions=evaluation._quoted(states.NO_PRECEDENT_FUNCTIONS))
    # Every capped value must reach the model as capped, and nothing else may.
    for fn in states.ALL_CORE_FUNCTIONS:
        capped_in_prompt = f'"{fn}"' in rendered.split("core_function in (")[1].split(")")[0]
        assert capped_in_prompt == (fn in states.NO_PRECEDENT_FUNCTIONS), fn


def test_core_function_does_not_resurrect_a_gate_fail():
    # The cap lives inside the gates-passed branch: a rejected role stays rejected, and
    # nothing writes a bucket back onto it.
    r = _norm(verdict="GATE_FAIL", fit_score=7, failed_gate="years_floor",
              core_function="post_sales_delivery")
    assert r["verdict"] == "GATE_FAIL"
    assert r["bucket"] is None
    assert r["fit_score"] is None


def test_gate_fail_nulls_bucket_and_score():
    r = _norm(verdict="GATE_FAIL", fit_score=7, bucket=2, failed_gate="years_floor")
    assert r["bucket"] is None
    assert r["fit_score"] is None


def test_unknown_verdict_becomes_gate_fail():
    r = _norm(verdict="MAYBE", fit_score=10)
    assert r["verdict"] == "GATE_FAIL"
    assert r["bucket"] is None


# ----- gate_results: the per-gate output contract (schema addition 2026-08-07) ---
# Normalization is assistive and never verdict-changing: a malformed diagnostics
# field must not re-bucket a role, it must earn a flag the human (and backtest) sees.

def _gr(**overrides):
    gr = {g: "PASS" for g in evaluation.GATE_NAMES}
    gr.update(overrides)
    return gr


def test_gate_results_complete_and_consistent_passes_clean():
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), gate_results=_gr())
    assert r["gate_results"] == _gr()
    assert r["eval_issues"] == []
    assert "gate-results-inconsistent" not in r["eval_issues"]


def test_gate_results_missing_gate_flags_incomplete_without_touching_verdict():
    partial = _gr()
    del partial["work_auth"]
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3), gate_results=partial)
    assert r["verdict"] == "PASS"          # assistive: never re-routes
    assert r["gate_results"]["work_auth"] is None
    assert "gate-results-incomplete" in r["eval_issues"]


def test_gate_results_absent_entirely_flags_incomplete():
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3))
    assert all(v is None for v in r["gate_results"].values())
    assert "gate-results-incomplete" in r["eval_issues"]


def test_gate_results_non_dict_fails_soft():
    # Same container-type discipline as score_breakdown: runs outside the retry
    # try/except, so a list/string here must degrade, never throw.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              gate_results=["years_floor: PASS"])
    assert all(v is None for v in r["gate_results"].values())
    assert "gate-results-incomplete" in r["eval_issues"]


def test_gate_results_case_and_whitespace_normalized():
    r = _norm(verdict="GATE_FAIL", failed_gate="years_floor",
              gate_results=_gr(years_floor=" fail "))
    assert r["gate_results"]["years_floor"] == "FAIL"
    assert "gate-results-inconsistent" not in r["eval_issues"]


def test_gate_fail_whose_named_gate_reads_pass_is_inconsistent():
    r = _norm(verdict="GATE_FAIL", failed_gate="years_floor", gate_results=_gr())
    assert r["verdict"] == "GATE_FAIL"
    assert "gate-results-inconsistent" in r["eval_issues"]


def test_gate_fail_naming_no_cause_anywhere_is_inconsistent():
    # No failed_gate and no gate reading FAIL: the verdict rejected the role while
    # pointing at nothing. "other" exists precisely so a real non-named fail can say
    # so, which makes this shape unexplained rather than ambiguous.
    r = _norm(verdict="GATE_FAIL", gate_results=_gr())
    assert "gate-results-inconsistent" in r["eval_issues"]


def test_gate_fail_with_an_unnamed_explicit_fail_is_consistent():
    # failed_gate absent but a gate explicitly reads FAIL — the cause IS stated,
    # just not duplicated into failed_gate. Not the unexplained shape.
    r = _norm(verdict="GATE_FAIL", gate_results=_gr(work_auth="FAIL"))
    assert "gate-results-inconsistent" not in r["eval_issues"]


def test_gate_fail_other_with_all_gates_passing_is_consistent():
    # The unmeetable-qualification rule fails OUTSIDE the six named gates: an
    # "other" failed_gate with six PASSes is the documented shape, not a conflict.
    r = _norm(verdict="GATE_FAIL", failed_gate="other", gate_results=_gr())
    assert "gate-results-inconsistent" not in r["eval_issues"]


def test_pass_verdict_with_an_explicit_gate_fail_is_inconsistent_but_uncapped():
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              gate_results=_gr(employment_type="FAIL"))
    assert r["verdict"] == "PASS"          # flag, don't re-route
    assert "gate-results-inconsistent" in r["eval_issues"]


def test_gate_results_junk_value_reads_as_missing():
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              gate_results=_gr(role_substance="N/A"))
    assert r["gate_results"]["role_substance"] is None
    assert "gate-results-incomplete" in r["eval_issues"]


def test_gate_results_flags_are_idempotent_on_renormalize():
    # normalize_result mutates in place; re-running it (a re-analysis pass, a probe
    # that normalizes then re-normalizes) must not stack duplicate diagnostics.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3))
    evaluation.normalize_result(r)
    assert r["eval_issues"].count("gate-results-incomplete") == 1


def test_diagnostics_never_touch_the_role_flag_channel():
    # `flags` is the model's free-text "what about this ROLE needs judgment" stream
    # (76% of rows carry one, ~53k distinct phrasings). Contract diagnostics answer a
    # different question — how much to trust the evaluation — and must stay out of it,
    # in both directions: an incomplete gate table must not append to flags, and the
    # model's own flags must survive untouched.
    r = _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
              flags=["management-drift"])          # gate_results absent -> an issue
    assert r["flags"] == ["management-drift"]
    assert r["eval_issues"] == ["gate-results-incomplete"]


def test_flags_left_alone_when_absent_or_malformed():
    # Diagnostics no longer coerce `flags`, so a missing or non-list value from the
    # model passes through as-is rather than being silently rewritten to a list.
    assert "flags" not in _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3))
    assert _norm(verdict="PASS", fit_score=15, score_breakdown=_bd(3),
                 flags="not-a-list")["flags"] == "not-a-list"


# ----- fit_score: the sort key must reach the DB as an integer 0-18 or NULL -----
# fit is a SORT KEY, not a routing gate, so validation nulls-and-flags and never
# touches the verdict. Unvalidated, both failure shapes are silent at the sqlite3
# boundary: float NaN binds as NULL (the row vanishes from every fit-line query),
# and a string stores as TEXT, which SQLite orders above every integer — so
# 'abc' >= 15 is TRUE and garbage crosses the action bars into the paid batch.

def test_fit_nan_nulled_and_flagged_without_touching_verdict():
    r = _norm(verdict="PASS", fit_score=float("nan"), score_breakdown=_bd(3))
    assert r["verdict"] == "PASS"          # review, never re-route
    assert r["fit_score"] is None
    assert "fit-score-invalid" in r["eval_issues"]


def test_fit_string_nulled_and_flagged():
    r = _norm(verdict="PASS", fit_score="abc", score_breakdown=_bd(3))
    assert r["fit_score"] is None
    assert "fit-score-invalid" in r["eval_issues"]


def test_fit_out_of_range_nulled_and_flagged():
    # Finite and numeric but outside the spec's declared 0-18 domain: 999 would
    # out-sort every real score and cross every bar, -5 would just sink — neither
    # is a value any consumer can read as a fit.
    for bad in (999, -5):
        r = _norm(verdict="RECRUITER_ONLY", fit_score=bad, score_breakdown=_bd(2))
        assert r["verdict"] == "RECRUITER_ONLY"
        assert r["fit_score"] is None
        assert "fit-score-invalid" in r["eval_issues"]


def test_fit_bool_true_is_not_a_valid_score():
    # isinstance(True, int) is True in Python — same explicit bool rejection as
    # the depth cap.
    r = _norm(verdict="PASS", fit_score=True, score_breakdown=_bd(3))
    assert r["fit_score"] is None
    assert "fit-score-invalid" in r["eval_issues"]


def test_fit_numeric_string_is_rejected_not_coerced():
    # Deliberate, and the opposite of the leadership cap's value-based reading: that
    # cap normalizes "true"/1/"yes" because it reads a small CLOSED vocabulary, while
    # fit's near sibling is the depth cap, which takes numbers only. Coercing "15"
    # would open a parsing surface with no bottom ("15/18", "fifteen", "15 of 18"),
    # and a quoted number is exactly the TEXT storage class that made an unvalidated
    # fit dangerous. Nulling costs nothing recoverable — eval_json keeps the raw
    # value and the flag puts a human on the row.
    r = _norm(verdict="PASS", fit_score="15", score_breakdown=_bd(3))
    assert r["fit_score"] is None
    assert "fit-score-invalid" in r["eval_issues"]


def test_fit_fraction_truncates_not_flagged():
    # needs_arbitration/_fit_key already read any finite in-range float as a
    # usable fit, so 15.5 is coerced to the declared integer domain, not
    # discarded. int() is floor on this non-negative range: a fraction never
    # rounds UP across an action bar.
    r = _norm(verdict="PASS", fit_score=15.5, score_breakdown=_bd(3))
    assert r["fit_score"] == 15
    assert "fit-score-invalid" not in r["eval_issues"]


def test_fit_missing_on_scored_verdict_flagged():
    # The output spec sets fit whenever gates pass; a scored row with NULL fit is
    # the same invisibility class as NaN. Flagging the stored None is also what
    # keeps the issue alive through _write_result's re-normalization, which
    # rebuilds eval_issues from scratch.
    r = _norm(verdict="PASS", score_breakdown=_bd(3))
    assert r["fit_score"] is None
    assert "fit-score-invalid" in r["eval_issues"]


def test_fit_flag_idempotent_on_renormalize():
    # First pass nulls the garbage; the second pass must re-derive the flag from
    # the stored None, not stack a duplicate.
    r = _norm(verdict="PASS", fit_score="abc", score_breakdown=_bd(3))
    evaluation.normalize_result(r)
    assert r["eval_issues"].count("fit-score-invalid") == 1


def test_fit_boundary_integers_untouched():
    # The whole legal domain — historical replays (backtest_v2) must read exactly
    # as before.
    for edge in (0, 18):
        r = _norm(verdict="PASS", fit_score=edge, score_breakdown=_bd(3))
        assert r["fit_score"] == edge
        assert "fit-score-invalid" not in r["eval_issues"]


def test_gate_fail_null_fit_not_flagged():
    # GATE_FAIL nulls fit BY DESIGN (no score on a rejected role); only scored
    # verdicts are held to the 0-18 contract.
    r = _norm(verdict="GATE_FAIL", failed_gate="years_floor",
              gate_results=_gr(years_floor="FAIL"))
    assert r["fit_score"] is None
    assert "fit-score-invalid" not in r["eval_issues"]


# ----- deepseek_request_body: one definition of the production request shape -----
# Four validation probes each hand-copied this dict and every copy silently became a
# different experiment when production moved (2026-08-07: a probe measured the wrong
# reasoning tier; a comparison column would have benchmarked the incumbent at a tier
# it doesn't run; an effort probe's "high" column would have measured low).

def _body(**overrides):
    return evaluation.deepseek_request_body("m", "SYS", "USER", **overrides)


def test_request_body_carries_the_production_settings():
    b = _body()
    # The literal, not evaluation.DEEPSEEK_EFFORT: asserting the constant against
    # itself stays green at any value, and "low" is the cost-bearing default tier
    # (measured 2026-08-13: low is honored; illegal values 400). Same rule as the
    # other literals below — a drifted default must turn this red.
    assert b["reasoning_effort"] == "low"
    assert b["max_tokens"] == 16000
    assert b["temperature"] == 0
    assert b["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert b["messages"][0]["content"] == "SYS"
    assert b["messages"][1]["content"] == "USER"


def test_request_body_override_replaces_a_key():
    assert _body(reasoning_effort="high")["reasoning_effort"] == "high"


def test_request_body_none_override_deletes_a_key():
    # How a caller says "send no effort at all" — e.g. beside thinking-disabled.
    b = _body(thinking={"type": "disabled"}, reasoning_effort=None)
    assert "reasoning_effort" not in b
    assert b["thinking"] == {"type": "disabled"}


def test_request_body_is_not_shared_between_calls():
    # A mutated body must not leak into the next request.
    a = _body()
    a["messages"].append({"role": "user", "content": "leak"})
    assert len(_body()["messages"]) == 2


# ----- retryable-vs-fatal error classification + the error-row requeue -----------

class _HttpxStyleError(Exception):
    """Carries .response.status_code, like httpx.HTTPStatusError."""
    def __init__(self, status):
        self.response = type("R", (), {"status_code": status})()


class _AnthropicStyleError(Exception):
    """Carries .status_code, like anthropic.APIStatusError."""
    def __init__(self, status):
        self.status_code = status


def test_retryable_classification():
    # Heals on its own -> retry: rate limits, timeouts, server errors, non-HTTP failures.
    assert evaluation._retryable(_HttpxStyleError(429))
    assert evaluation._retryable(_HttpxStyleError(408))
    assert evaluation._retryable(_AnthropicStyleError(500))
    assert evaluation._retryable(_AnthropicStyleError(529))
    assert evaluation._retryable(ValueError("no JSON object in model response"))
    assert evaluation._retryable(TimeoutError())
    # Our request is wrong -> fatal for the row, no retry.
    assert not evaluation._retryable(_HttpxStyleError(400))
    assert not evaluation._retryable(_AnthropicStyleError(404))
    assert not evaluation._retryable(_HttpxStyleError(422))


def test_http_status_extraction():
    assert evaluation._http_status(_HttpxStyleError(401)) == 401
    assert evaluation._http_status(_AnthropicStyleError(403)) == 403
    assert evaluation._http_status(ValueError("x")) is None


def test_requeue_error_rows(conn):
    make_job(conn, job_url="e1", status="error", verdict=None, fit_score=None, bucket=None)
    make_job(conn, job_url="done", status="evaluated")
    make_job(conn, job_url="fresh", status="new", verdict=None, fit_score=None, bucket=None)
    assert evaluation.requeue_error_rows(conn) == 1
    statuses = {r["job_url"]: r["status"]
                for r in conn.execute("SELECT job_url, status FROM jobs")}
    assert statuses == {"e1": "new", "done": "evaluated", "fresh": "new"}


def test_requeued_error_row_refaces_the_filters(conn):
    """The stage-order contract: requeue runs BEFORE the deterministic filters (see `run`),
    so a chain decision made while a relisting sat in 'error' repost-skips it instead of
    letting it slip straight into the paid eval."""
    make_job(conn, job_url="canon", company="Chain Co", app_status="applied",
             status_date="2026-07-01")
    make_job(conn, job_url="err", company="Chain Co", repost_of="canon",
             status="error", verdict=None, fit_score=None, bucket=None)
    evaluation.requeue_error_rows(conn)   # error -> new (the run stage after the fetchers)
    chain.skip_decided_reposts(conn)      # then the pre-eval passes run over 'new'
    assert job_status(conn, "err") == "repost_decided"   # eval never sees it


def test_requeued_relisting_of_evaluated_chain_is_not_rebilled(conn):
    """Same stage-order contract for the evaluated-chain skip: a relisting requeued from
    'error' whose role already holds a verdict goes to 'repost_evaluated', not back into
    the paid eval."""
    make_job(conn, job_url="canon", company="Chain Co", status="evaluated", verdict="PASS")
    make_job(conn, job_url="err", company="Chain Co", repost_of="canon",
             status="error", verdict=None, fit_score=None, bucket=None)
    evaluation.requeue_error_rows(conn)
    chain.skip_decided_reposts(conn)      # no user decision — this pass leaves it 'new'
    chain.skip_evaluated_reposts(conn)    # ...and this one catches it
    assert job_status(conn, "err") == "repost_evaluated"   # eval never sees it
