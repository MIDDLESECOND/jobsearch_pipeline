"""Boundary-band arbitration — the majority-vote pass over the noisy temp-0 judge.

The trigger (needs_arbitration) fires only on a scored verdict whose fit lands in
ARBITRATION_BAND — the band around the two action bars (cold-apply PASS>=13,
recruiter_route RECRUITER_ONLY>=15) where a rerun flip would change what the user
DOES, as measured by tests/validation/flip_consequence.py. The vote (arbitrate)
overrides the first draw only on a strict majority; anything short of that keeps
the first draw and surfaces `arbitration-split` via normalize_result — review,
never re-route, same policy as the gate-contract diagnostics.
"""

import json

import evaluation
from states import GATE_NAMES


def _draw(verdict, fit, depth=2):
    """A minimal NORMALIZED-shaped draw for the pure arbitrate() tests."""
    return {"verdict": verdict, "fit_score": fit, "bucket": 2,
            "score_breakdown": {"ai_artifact_depth": depth, "marker_fit": fit}}


# ---- needs_arbitration -------------------------------------------------------

def test_trigger_fires_inside_band_on_both_scored_verdicts():
    lo, hi = evaluation.ARBITRATION_BAND
    for verdict in ("PASS", "RECRUITER_ONLY"):
        for fit in (lo, (lo + hi) // 2, hi):
            assert evaluation.needs_arbitration({"verdict": verdict, "fit_score": fit})


def test_trigger_silent_outside_band():
    lo, hi = evaluation.ARBITRATION_BAND
    for fit in (lo - 1, hi + 1, 0, 18):
        assert not evaluation.needs_arbitration({"verdict": "PASS", "fit_score": fit})


def test_trigger_never_fires_on_gate_fail_or_malformed_fit():
    assert not evaluation.needs_arbitration({"verdict": "GATE_FAIL", "fit_score": None})
    # GATE_FAIL first draws are deliberately un-arbitrated (73% fire rate for +6pp
    # catch — see flip_consequence.py); malformed fits follow the caps' discipline.
    for bad in (None, True, float("nan"), "14"):
        assert not evaluation.needs_arbitration({"verdict": "PASS", "fit_score": bad})


# ---- arbitrate ---------------------------------------------------------------

def test_majority_overrides_first_draw():
    first = _draw("PASS", 14)
    chosen = evaluation.arbitrate([first, _draw("RECRUITER_ONLY", 12),
                                   _draw("RECRUITER_ONLY", 13)])
    assert chosen["verdict"] == "RECRUITER_ONLY"
    arb = chosen["arbitration"]
    assert arb["overrode_first"] and not arb["split"] and arb["k"] == 3
    assert [d["verdict"] for d in arb["draws"]] == ["PASS", "RECRUITER_ONLY",
                                                    "RECRUITER_ONLY"]


def test_chosen_draw_is_kept_whole_at_the_winners_low_median():
    # Two winners (12, 13) -> the lower-middle draw wins, and its own
    # score_breakdown rides along — never a synthetic average.
    chosen = evaluation.arbitrate([_draw("PASS", 14), _draw("RECRUITER_ONLY", 13),
                                   _draw("RECRUITER_ONLY", 12)])
    assert chosen["fit_score"] == 12
    assert chosen["score_breakdown"]["marker_fit"] == 12


def test_malformed_extra_draw_fit_does_not_crash_the_vote():
    # Only draws[0]'s fit is type-checked (needs_arbitration); an extra draw can
    # carry a string/None/NaN. A naive sort key raises TypeError here, and that
    # throw is outside any retry — it would discard all three PAID draws and mark
    # the row 'error'. The chosen draw must still be a real, whole draw.
    for bad in ("14", None, float("nan"), True, [14]):
        chosen = evaluation.arbitrate([_draw("PASS", 14),
                                       {"verdict": "PASS", "fit_score": bad},
                                       _draw("PASS", 15)])
        assert chosen["verdict"] == "PASS"
        assert chosen["fit_score"] in (14, 15, bad)


def test_unanimous_keeps_verdict_and_takes_median_fit():
    chosen = evaluation.arbitrate([_draw("PASS", 17), _draw("PASS", 11),
                                   _draw("PASS", 14)])
    assert chosen["verdict"] == "PASS" and chosen["fit_score"] == 14
    assert not chosen["arbitration"]["overrode_first"]


def test_three_way_split_keeps_first_draw_and_flags():
    first = _draw("PASS", 14)
    chosen = evaluation.arbitrate([first, _draw("RECRUITER_ONLY", 14),
                                   _draw("GATE_FAIL", None)])
    assert chosen is first and chosen["arbitration"]["split"]


def test_two_draw_disagreement_is_a_split_not_an_override():
    # An extra draw failed: 1-1 has no strict majority — keep the production draw.
    first = _draw("PASS", 14)
    chosen = evaluation.arbitrate([first, _draw("RECRUITER_ONLY", 14)])
    assert chosen is first and chosen["arbitration"]["split"]
    assert not chosen["arbitration"]["overrode_first"]


def test_single_draw_records_attempt_without_split():
    first = _draw("PASS", 14)
    chosen = evaluation.arbitrate([first])
    assert chosen is first
    assert chosen["arbitration"]["k"] == 1 and not chosen["arbitration"]["split"]


def test_split_surfaces_as_eval_issue_and_stays_idempotent():
    r = evaluation.arbitrate([_draw("PASS", 14), _draw("RECRUITER_ONLY", 14)])
    for _ in range(2):  # _write_result re-normalizes; the issue must not stack
        evaluation.normalize_result(r)
        assert r["eval_issues"].count("arbitration-split") == 1


def test_clean_majority_adds_no_issue():
    r = evaluation.arbitrate([_draw("PASS", 14), _draw("PASS", 15), _draw("PASS", 16)])
    evaluation.normalize_result(r)
    assert "arbitration-split" not in r["eval_issues"]


# ---- _evaluate_one wiring ----------------------------------------------------

ROW = {"job_url": "https://example.com/jobs/1", "title": "T", "company": "C",
       "location": "L", "search_name": "s", "tier": "core",
       "salary_min": None, "salary_max": None, "description": "d"}


def _model_json(verdict, fit=None, depth=2):
    if verdict == "GATE_FAIL":
        gr = {g: "PASS" for g in GATE_NAMES}
        gr["tool_requirement"] = "FAIL"
        return json.dumps({"verdict": verdict, "gate_results": gr,
                           "failed_gate": "tool_requirement", "fit_score": None,
                           "score_breakdown": None,
                           "formal_leadership_required": False, "bucket": None})
    bd = {"ai_applied_vs_research": 3, "ai_artifact_depth": depth,
          "learning_value": 2, "technical_skill_match": 2,
          "title_trajectory": 2, "years_vs_stated": 2}
    return json.dumps({"verdict": verdict,
                       "gate_results": {g: "PASS" for g in GATE_NAMES},
                       "failed_gate": None, "fit_score": fit, "score_breakdown": bd,
                       "formal_leadership_required": False, "bucket": None})


def _script(monkeypatch, responses):
    """Patch the provider call with a scripted response queue; returns the call log.
    A response that is an Exception instance is raised instead of returned."""
    calls = []

    def fake(api_key, model, system_prompt, user_msg):
        i = len(calls)
        calls.append(user_msg)
        r = responses[i]
        if isinstance(r, Exception):
            raise r
        return r, 10, 100, 1000, 0

    monkeypatch.setattr(evaluation, "_call_deepseek", fake)
    return calls


def test_in_band_first_draw_spends_exactly_two_extra_calls(monkeypatch):
    calls = _script(monkeypatch, [_model_json("PASS", 14), _model_json("PASS", 15),
                                  _model_json("PASS", 16)])
    url, result, tin, tout, cr, cw = evaluation._evaluate_one(
        ROW, "deepseek", "m", "sys", None, "key")
    assert len(calls) == 3
    assert result is not None
    assert result["arbitration"]["k"] == 3
    # Token tally sums all three draws — the cost line must not hide arbitration.
    assert (tin, tout, cr, cw) == (30, 300, 3000, 0)


def test_out_of_band_first_draw_stays_a_single_call(monkeypatch):
    calls = _script(monkeypatch, [_model_json("PASS", 8)])
    _, result, *_ = evaluation._evaluate_one(ROW, "deepseek", "m", "sys", None, "key")
    assert result is not None
    assert len(calls) == 1 and "arbitration" not in result


def test_gate_fail_first_draw_is_never_arbitrated(monkeypatch):
    calls = _script(monkeypatch, [_model_json("GATE_FAIL")])
    _, result, *_ = evaluation._evaluate_one(ROW, "deepseek", "m", "sys", None, "key")
    assert result is not None
    assert len(calls) == 1 and result["verdict"] == "GATE_FAIL"


def test_majority_override_end_to_end(monkeypatch):
    _script(monkeypatch, [_model_json("PASS", 14),
                          _model_json("RECRUITER_ONLY", 14),
                          _model_json("RECRUITER_ONLY", 13)])
    _, result, *_ = evaluation._evaluate_one(ROW, "deepseek", "m", "sys", None, "key")
    assert result is not None
    assert result["verdict"] == "RECRUITER_ONLY"
    assert result["arbitration"]["overrode_first"]


def test_failed_extra_draw_degrades_to_a_two_draw_vote(monkeypatch):
    _script(monkeypatch, [_model_json("PASS", 14), RuntimeError("boom"),
                          _model_json("PASS", 14)])
    _, result, *_ = evaluation._evaluate_one(ROW, "deepseek", "m", "sys", None, "key")
    assert result is not None
    # Draw 2 died, draw 3 agreed: unanimous 2-draw vote, no split, no override.
    arb = result["arbitration"]
    assert arb["k"] == 2 and not arb["split"] and not arb["overrode_first"]
