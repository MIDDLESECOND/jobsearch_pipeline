"""cmd_prune — reclaims description space WITHOUT touching anything still readable/undoable.

The keep-list is the contract: gates-passed rows (backtest_v2 re-evaluates from stored text;
applied/passed history keeps its JD), repost_decided rows (an undo re-news them for a re-eval),
and never-evaluated manual rejects (reject --undo re-news them). Only aged-out GATE_FAIL and
salary_filtered rows are cleared, and eval_json survives everywhere.
"""

import pipeline
from conftest import make_job

OLD = "2026-01-01T09:00:00"   # far past any --days floor
FRESH = "2026-07-04T09:00:00"


def _desc(conn, url):
    return conn.execute("SELECT description FROM jobs WHERE job_url=?", (url,)).fetchone()[0]


def test_prune_clears_only_aged_rejected_rows(conn):
    make_job(conn, job_url="old_fail", first_seen=OLD, verdict="GATE_FAIL",
             fit_score=None, bucket=None, eval_json='{"gate_notes": "x"}')
    make_job(conn, job_url="old_salary", first_seen=OLD, status="salary_filtered",
             verdict=None, fit_score=None, bucket=None)
    make_job(conn, job_url="fresh_fail", first_seen=FRESH, verdict="GATE_FAIL",
             fit_score=None, bucket=None)
    pipeline.cmd_prune(conn, days=90, vacuum=False)
    assert _desc(conn, "old_fail") is None
    assert _desc(conn, "old_salary") is None
    assert _desc(conn, "fresh_fail") is not None  # inside the age floor
    # eval_json is never pruned — old reports rebuild their one-liners from it.
    row = conn.execute("SELECT eval_json FROM jobs WHERE job_url='old_fail'").fetchone()
    assert row["eval_json"] == '{"gate_notes": "x"}'


def test_prune_keeps_everything_still_readable_or_undoable(conn):
    # Gates-passed: backtest_v2 fixture material + triage reading.
    make_job(conn, job_url="old_pass", first_seen=OLD, verdict="PASS")
    make_job(conn, job_url="old_rec", first_seen=OLD, verdict="RECRUITER_ONLY", bucket=1)
    # Applied history keeps its JD (even a GATE_FAIL verdict, via chain propagation).
    make_job(conn, job_url="old_applied_fail", first_seen=OLD, verdict="GATE_FAIL",
             fit_score=None, bucket=None, app_status="applied", status_date="2026-02-01")
    # Undo returns these to 'new' for a re-eval, which needs the text.
    make_job(conn, job_url="old_repost_skip", first_seen=OLD, status="repost_decided",
             verdict=None, fit_score=None, bucket=None)
    make_job(conn, job_url="old_manual_rej", first_seen=OLD, status="rule_filtered",
             verdict=None, fit_score=None, bucket=None,
             filter_source="manual", filter_gate="other", filter_date="2026-02-01")
    pipeline.cmd_prune(conn, days=90, vacuum=False)
    for url in ("old_pass", "old_rec", "old_applied_fail", "old_repost_skip", "old_manual_rej"):
        assert _desc(conn, url) is not None, f"{url} must keep its description"
