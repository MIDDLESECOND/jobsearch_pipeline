"""_write_result — the write half of the eval stage. normalize_result (the routing) is
tested thickly in test_eval_routing; these tests pin the PERSISTENCE contract instead:
the status='evaluated'/'error' transition, the column list/binding order of the one
ongoing UPDATE of verdict/fit_score/bucket/eval_json/eval_issues, the unknown-failed_gate
coercion to 'other', and the eval_issues denormalization the Action Center queue reads
(comma-joined, NULL = clean — the AGENTS.md contract)."""

import json

import evaluation
import states
from conftest import make_job


def _gates_all_pass():
    return {g: "PASS" for g in states.GATE_NAMES}


def _bd(depth):
    return {"ai_applied_vs_research": 3, "ai_artifact_depth": depth,
            "learning_value": 3, "technical_skill_match": 3,
            "title_trajectory": 3, "years_vs_stated": 3}


def _new_row(conn):
    """A row as the fetchers leave it: status='new', nothing evaluated yet."""
    return make_job(conn, status="new", verdict=None, failed_gate=None,
                    fit_score=None, bucket=None, eval_json=None)


def _fetch(conn, url):
    return conn.execute("SELECT * FROM jobs WHERE job_url=?", (url,)).fetchone()


def test_clean_result_lands_every_column(conn):
    row = _new_row(conn)
    result = {"verdict": "PASS", "fit_score": 15, "score_breakdown": _bd(2),
              "gate_results": _gates_all_pass(), "formal_leadership_required": False}
    evaluation._write_result(conn, row["job_url"], result)
    got = _fetch(conn, row["job_url"])
    assert got["status"] == states.STATUS_EVALUATED
    assert got["verdict"] == "PASS"
    assert got["failed_gate"] is None
    assert got["fit_score"] == 15
    assert got["bucket"] == 2           # depth 2 → acceptable tier, set by normalization
    assert got["eval_issues"] is None   # clean = NULL, never ""
    # What's stored is the NORMALIZED result, not the raw model payload.
    stored = json.loads(got["eval_json"])
    assert stored["verdict"] == "PASS"
    assert stored["gate_results"] == _gates_all_pass()
    assert stored["eval_issues"] == []  # empty list in the JSON ↔ NULL in the column


def test_eval_issues_are_comma_joined_onto_the_row(conn):
    row = _new_row(conn)
    # Two independent findings (an arbitration split + a missing gate table) must land
    # as ONE comma-joined cell — the shape the review queue's column test parses.
    result = {"verdict": "PASS", "fit_score": 14, "score_breakdown": _bd(2),
              "arbitration": {"split": True}}
    evaluation._write_result(conn, row["job_url"], result)
    got = _fetch(conn, row["job_url"])
    assert got["status"] == states.STATUS_EVALUATED
    assert got["eval_issues"] == "arbitration-split,gate-results-incomplete"


def test_unknown_failed_gate_is_coerced_to_other(conn):
    row = _new_row(conn)
    result = {"verdict": "GATE_FAIL", "failed_gate": "not_a_real_gate",
              "gate_results": _gates_all_pass()}
    evaluation._write_result(conn, row["job_url"], result)
    got = _fetch(conn, row["job_url"])
    assert got["status"] == states.STATUS_EVALUATED
    assert got["verdict"] == "GATE_FAIL"
    assert got["failed_gate"] == states.GATE_OTHER
    assert got["fit_score"] is None     # normalization clears score/bucket on GATE_FAIL
    assert got["bucket"] is None


def test_none_result_marks_row_error_and_touches_nothing_else(conn):
    # A failed call writes exactly one thing — status='error' for the next run's
    # requeue; no eval column may be invented for a row that was never judged.
    row = _new_row(conn)
    evaluation._write_result(conn, row["job_url"], None)
    got = _fetch(conn, row["job_url"])
    assert got["status"] == states.STATUS_ERROR
    assert got["verdict"] is None
    assert got["fit_score"] is None
    assert got["eval_json"] is None
    assert got["eval_issues"] is None
