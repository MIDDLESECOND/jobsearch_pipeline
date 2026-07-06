"""normalize_result — the deterministic routing the model is NOT trusted to do.

The load-bearing rule (the '50/0 fix'): a role that clears the gates but whose
ai_artifact_depth is 0 (or unparseable) is capped to RECRUITER_ONLY / bucket 1,
even at a perfect score. Enforced in code so it can't depend on the model complying.
"""

import chain
import evaluation
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


def test_gate_fail_nulls_bucket_and_score():
    r = _norm(verdict="GATE_FAIL", fit_score=7, bucket=2, failed_gate="years_floor")
    assert r["bucket"] is None
    assert r["fit_score"] is None


def test_unknown_verdict_becomes_gate_fail():
    r = _norm(verdict="MAYBE", fit_score=10)
    assert r["verdict"] == "GATE_FAIL"
    assert r["bucket"] is None


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
