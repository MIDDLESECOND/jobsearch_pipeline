"""normalize_result — the deterministic routing the model is NOT trusted to do.

The load-bearing rule (the '50/0 fix'): a role that clears the gates but whose
ai_artifact_depth is 0 (or unparseable) is capped to RECRUITER_ONLY / bucket 1,
even at a perfect score. Enforced in code so it can't depend on the model complying.
"""

import pipeline


def _norm(**kw):
    return pipeline.normalize_result(dict(kw))


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


def test_recruiter_only_input_with_depth0_stays_bucket1():
    r = _norm(verdict="RECRUITER_ONLY", fit_score=14, score_breakdown=_bd(0))
    assert r["verdict"] == "RECRUITER_ONLY"
    assert r["bucket"] == 1


def test_gate_fail_nulls_bucket_and_score():
    r = _norm(verdict="GATE_FAIL", fit_score=7, bucket=2, failed_gate="years_floor")
    assert r["bucket"] is None
    assert r["fit_score"] is None


def test_unknown_verdict_becomes_gate_fail():
    r = _norm(verdict="MAYBE", fit_score=10)
    assert r["verdict"] == "GATE_FAIL"
    assert r["bucket"] is None
