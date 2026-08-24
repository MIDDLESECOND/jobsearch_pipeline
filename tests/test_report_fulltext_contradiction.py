"""A snippet-scored card must disclose a worse full-text reading of the same role.

The evidence inversion this pins (measured 2026-08-20): snippet rows score systematically
hot — one WSGR requisition scored PASS 16 from a 500-char snippet and RECRUITER_ONLY 12
from the 9,327-char board text the same day, and 403 of 2,167 undecided snippet PASS rows
at the cold-apply bar (19%) had a full-text reading of the same role saying strictly worse,
invisible because cross-source rows rarely chain. The fix is surfacing only: the snippet
row keeps its verdict and score and gains one diagnostics line naming the fuller reading.

Every expected value here is a pinned literal (test rule 1), and every threshold is
asserted on BOTH sides (test rule 3): 520/521 chars for the snippet bound, 2000/2001 for
the full-text bound, fit margin 1 vs 2, and adzuna/not-adzuna for the source leg.
"""
import report
from conftest import make_job

DAY = "2026-06-01"
FLAG = "snippet-scored — the full-text read of this role says"


def _report(conn, tmp_path):
    out = tmp_path / "reports"
    report.generate_report({"settings": {"reports_dir": str(out)}}, conn, for_date=DAY)
    return (out / f"report_{DAY}.md").read_text(encoding="utf-8")


def _snippet(conn, *, company="Acme Corp", title="AI Solutions Analyst",
             verdict="PASS", fit_score=16, desc_len=500, first_seen=f"{DAY}T09:00:00",
             source="adzuna"):
    # source='adzuna' is part of what MAKES this a snippet row: the bound is a truncation
    # reading, not a "short text" reading (report.fulltext_contradiction).
    return make_job(conn, company=company, title=title, verdict=verdict,
                    fit_score=fit_score, description="s" * desc_len, source=source,
                    first_seen=first_seen, eval_json="{}")


def _fulltext(conn, *, company="Acme Corp", title="AI Solutions Analyst",
              verdict="GATE_FAIL", fit_score=None, desc_len=5000):
    # A different day, so only the snippet row renders in DAY's report — the full-text
    # reading must reach the card through fulltext_readings, not through the section.
    return make_job(conn, company=company, title=title, verdict=verdict,
                    fit_score=fit_score, description="f" * desc_len,
                    first_seen="2026-05-20T09:00:00", eval_json="{}")


def test_snippet_pass_with_fulltext_gate_fail_discloses_it(conn, tmp_path):
    _snippet(conn)
    _fulltext(conn, verdict="GATE_FAIL", fit_score=None)
    out = _report(conn, tmp_path)
    assert f"{FLAG} GATE_FAIL" in out


def test_fulltext_fit_margin_two_flags_margin_one_does_not(conn, tmp_path):
    # Same verdict: only a >= 2-point full-text shortfall speaks. 16 vs 14 flags;
    # a second role at 16 vs 15 must stay silent — both sides of CONTRA_FIT_MARGIN.
    _snippet(conn, title="AI Solutions Analyst", fit_score=16)
    _fulltext(conn, title="AI Solutions Analyst", verdict="PASS", fit_score=14)
    _snippet(conn, title="AI Enablement Analyst", fit_score=16)
    _fulltext(conn, title="AI Enablement Analyst", verdict="PASS", fit_score=15)
    out = _report(conn, tmp_path)
    assert f"{FLAG} PASS 14/18" in out
    assert f"{FLAG} PASS 15/18" not in out


def test_most_favorable_fulltext_reading_silences_the_flag(conn, tmp_path):
    # Two full reads exist: GATE_FAIL and PASS 16. Most-favorable wins, so the snippet's
    # PASS 16 is corroborated, not contradicted — the Holland & Knight shape.
    _snippet(conn, fit_score=16)
    _fulltext(conn, verdict="GATE_FAIL", fit_score=None)
    _fulltext(conn, verdict="PASS", fit_score=16)
    out = _report(conn, tmp_path)
    assert FLAG not in out


def test_short_but_complete_non_adzuna_jd_is_not_called_snippet_scored(conn, tmp_path):
    # The bound reads "this text is an Adzuna truncation", not "this text is short". A terse
    # but COMPLETE ATS/Dice JD under the same length was scored on whole evidence, so it must
    # NOT be told a fuller read disagrees. Both sides, same length and same worse full-text
    # reading — only `source` differs, so a dropped source leg fails this test.
    _snippet(conn, title="AI Solutions Analyst", source="adzuna")
    _fulltext(conn, title="AI Solutions Analyst")
    _snippet(conn, title="AI Enablement Analyst", source="greenhouse")
    _fulltext(conn, title="AI Enablement Analyst")
    out = _report(conn, tmp_path)
    lines = [ln for ln in out.splitlines() if FLAG in ln]
    assert len(lines) == 1                       # only the adzuna card carries the disclosure
    assert "AI Solutions Analyst" in out.split(FLAG)[0]


def test_snippet_length_boundary_520_flags_521_does_not(conn, tmp_path):
    _snippet(conn, title="AI Solutions Analyst", desc_len=520)
    _fulltext(conn, title="AI Solutions Analyst")
    _snippet(conn, title="AI Enablement Analyst", desc_len=521)
    _fulltext(conn, title="AI Enablement Analyst")
    out = _report(conn, tmp_path)
    lines = [ln for ln in out.splitlines() if FLAG in ln]
    assert len(lines) == 1  # only the 520-char card carries the disclosure


def test_fulltext_length_boundary_2001_counts_2000_does_not(conn, tmp_path):
    # A 2000-char "full" reading is another stub and must not qualify as the fuller voice.
    _snippet(conn, title="AI Solutions Analyst")
    _fulltext(conn, title="AI Solutions Analyst", desc_len=2001)
    _snippet(conn, title="AI Enablement Analyst")
    _fulltext(conn, title="AI Enablement Analyst", desc_len=2000)
    out = _report(conn, tmp_path)
    lines = [ln for ln in out.splitlines() if FLAG in ln]
    assert len(lines) == 1
    assert "AI Solutions Analyst" in out.split(FLAG)[0]  # the flagged card is the 2001 pairing


def test_fulltext_row_itself_never_flagged_and_keeps_its_own_card(conn, tmp_path):
    # A full-text row rendering in the same report must not disclose against itself.
    make_job(conn, company="Acme Corp", title="AI Solutions Analyst", verdict="RECRUITER_ONLY",
             fit_score=12, description="f" * 5000, first_seen=f"{DAY}T08:00:00", eval_json="{}")
    _snippet(conn, verdict="PASS", fit_score=16, first_seen=f"{DAY}T09:00:00")
    out = _report(conn, tmp_path)
    assert f"{FLAG} RECRUITER_ONLY 12/18" in out          # the snippet card discloses
    assert out.count(FLAG) == 1                            # the full-text card does not
