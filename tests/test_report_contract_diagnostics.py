"""The eval-contract diagnostic must be visible on REJECTED rows.

A causeless GATE_FAIL — the model rejecting a role while its own gate table reads all
PASS — is the irreversible direction of the judge's verdict instability: a rejected
posting surfaces in no queue, no Action Center card, and no follow-up, so the role
simply vanishes. The first live instance was exactly that shape (an agentic-solutions
engineering role, i.e. the target tier, whose own one_line said "all gates pass").

The diagnostic originally rendered only inside _render_scored_job, which runs for
gates-passed rows — so the finding that matters most appeared exactly where it could
not be seen. These tests pin it into the gate-fail section.
"""
import json

import report
from conftest import make_job

DAY = "2026-06-01"


def _report(conn, tmp_path):
    # reports_dir is joined onto BASE_DIR; an absolute path wins that join, which keeps
    # the test out of the repo's real reports/ directory.
    out = tmp_path / "reports"
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=DAY)
    return (out / f"report_{DAY}.md").read_text(encoding="utf-8")


def _gate_fail(conn, *, eval_json, title="Engineer, Agentic Solutions"):
    make_job(conn, title=title, company="Acme Semiconductor", status="evaluated",
             verdict="GATE_FAIL", failed_gate=None, fit_score=None, bucket=None,
             first_seen=f"{DAY}T09:00:00", eval_json=json.dumps(eval_json))


def test_causeless_gate_fail_shows_its_diagnostic_in_the_report(conn, tmp_path):
    _gate_fail(conn, eval_json={
        "gate_notes": "all gates pass on the stated text",
        "gate_results": {g: "PASS" for g in
                         ["years_floor", "domain_requirement", "role_substance",
                          "tool_requirement", "work_auth", "employment_type"]},
        "eval_issues": ["gate-results-inconsistent"],
    })
    out = _report(conn, tmp_path)
    assert "gate-results-inconsistent" in out
    assert "verify before trusting this rejection" in out


def test_clean_gate_fail_carries_no_diagnostic_noise(conn, tmp_path):
    _gate_fail(conn, eval_json={
        "gate_notes": "15+ years required",
        "gate_results": {"years_floor": "FAIL"},
        "eval_issues": [],
    })
    out = _report(conn, tmp_path)
    assert "🔎" not in out
    assert "15+ years required" in out


def test_gate_fail_predating_the_field_still_renders(conn, tmp_path):
    # Rows evaluated before eval_issues existed have no such key; the section must
    # render exactly as before rather than KeyError or print an empty marker.
    _gate_fail(conn, eval_json={"gate_notes": "sales role, no precedent"})
    out = _report(conn, tmp_path)
    assert "sales role, no precedent" in out
    assert "🔎" not in out
