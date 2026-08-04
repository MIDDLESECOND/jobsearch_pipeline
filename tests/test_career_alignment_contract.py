"""Prompt-contract regression tests for the 2026-08-03 career alignment.

These tests are deliberately local and deterministic: they verify the committed prompt
templates and the non-schema output contract without sending private profile/JD data to an
external model. Existing routing behavior remains covered by test_eval_routing.py.
"""

from pathlib import Path

import evaluation


BASE = Path(__file__).resolve().parents[1]
GUIDE = (BASE / "evaluation_guide.example.md").read_text(encoding="utf-8")
PROFILE = (BASE / "profile.example.md").read_text(encoding="utf-8")
ALIGNMENT = (BASE / "docs" / "career_strategy_alignment.md").read_text(encoding="utf-8")


def test_eval_is_classified_by_object_not_keyword():
    assert "Identify the evaluation object" in GUIDE
    assert "production application/workflow evaluation is in scope" in GUIDE
    assert "foundation-model research" in GUIDE
    assert "research benchmark creation" in GUIDE
    assert "not from-scratch model training, evals/benchmarks" not in GUIDE
    assert "not from-scratch model training/tuning, evals/benchmarks" not in PROFILE


def test_held_and_building_capabilities_are_not_inflated():
    assert "Building — NOT yet held production experience" in PROFILE
    assert "must not be represented as held production experience" in PROFILE
    assert "must not\n  raise current artifact depth" in PROFILE


def test_three_questions_stay_separate_and_career_capital_is_non_scoring():
    for question in (
        "Can I perform the work?",
        "Can my current materials pass the screen?",
        "Will the role\naccumulate long-term career capital?",
    ):
        assert question in GUIDE
    assert "Career-capital note (gates-passed roles only; non-scoring)" in GUIDE
    assert "duplicate a\npenalty already carried by `learning_value` or `title_trajectory`" in GUIDE
    assert '"career_capital"' not in evaluation.SYSTEM_TEMPLATE
    assert "Career capital: builds ...; visibly lacks ..." in evaluation.SYSTEM_TEMPLATE


def test_existing_score_and_bucket_contract_is_unchanged():
    assert "Total: ___ / 18" in GUIDE
    assert "14–18 = strong" in GUIDE
    assert "10–13 = acceptable-tier" in GUIDE
    for bucket in ("Bucket 1", "Bucket 2", "Bucket 3"):
        assert bucket in GUIDE
    assert "ai_artifact_depth == 0" in evaluation.SYSTEM_TEMPLATE
    assert "formal_leadership_required == true" in evaluation.SYSTEM_TEMPLATE


def test_existing_gate_and_assistive_boundaries_remain_explicit():
    assert "citizenship-only or clearance requirements" in PROFILE
    assert 'PASS (e.g. "authorized without sponsorship")' in PROFILE
    assert "permanent full-time only" in PROFILE
    assert "Function-precedent check" in GUIDE
    assert "Management-drift (assistive flag, not a cap)" in GUIDE
    assert "Enablement-cluster (assistive flag, not a cap)" in GUIDE
    assert "trajectory risk explained here, not gated" in GUIDE


def test_manual_matrix_covers_requested_boundaries():
    for case_id in ("PAE", "RMT", "BAD", "FDE", "ENA", "MGT", "FUN", "LDR", "AUT"):
        assert f"| {case_id} |" in ALIGNMENT
    assert "No SQLite column or structured output field is added" in ALIGNMENT
