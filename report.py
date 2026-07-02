#!/usr/bin/env python3
"""Daily markdown report: groups the day's postings by verdict/status and renders each. Reports are
DISPOSABLE derivations of jobs.db (the single source of truth) — never reconstruct state from them.
The chain-wide "what has the user decided?" question is answered by chain.effective_decision (the
same function the web UI uses), so the report and UI can't drift. Imports core (BASE_DIR) and chain.
"""

import json
from datetime import date

from core import BASE_DIR
from chain import effective_decisions


def generate_report(cfg, conn, for_date=None):
    d = for_date or date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE substr(first_seen,1,10)=? ORDER BY fit_score DESC", (d,)
    ).fetchall()
    # Fetch every chain decision ONCE (batched), then pass each row's `dec` into the pure render
    # helpers below — same "inject the decision, don't fetch it" shape app.row_to_dict uses. Calling
    # effective_decision per row inside the render loops was an N+1 (one query per posting rendered).
    decisions = effective_decisions(conn, rows)

    # Hard-fail overrides (your rules + manual rejects) are pulled out first so they don't
    # also appear under their model verdict (a manual reject keeps its original PASS verdict).
    hard_filtered = [r for r in rows if r["filter_source"]]
    passes = [r for r in rows if r["verdict"] == "PASS" and not r["filter_source"]]
    recruiter = [r for r in rows if r["verdict"] == "RECRUITER_ONLY" and not r["filter_source"]]
    fails = [r for r in rows if r["verdict"] == "GATE_FAIL" and not r["filter_source"]]
    manual = [r for r in rows if r["status"] == "needs_manual" and not r["filter_source"]]
    errors = [r for r in rows if r["status"] == "error"]
    salary_filtered = [r for r in rows if r["status"] == "salary_filtered"]
    repost_skipped = [r for r in rows if r["status"] == "repost_decided"]

    reposts = [r for r in rows if r["repost_of"]]
    repost_status = [decisions[r["job_url"]]["app_status"] for r in reposts]
    applied_reposts = sum(s == "applied" for s in repost_status)
    passed_reposts = sum(s == "passed" for s in repost_status)

    lines = [f"# Job Pipeline Report — {d}", ""]
    lines.append(
        f"**{len(rows)} new postings** | {len(passes)} cold-apply (PASS) | "
        f"{len(recruiter)} recruiter-only | {len(fails)} gate fails | "
        f"{len(manual)} need manual review | {len(salary_filtered)} salary-filtered | "
        f"{len(hard_filtered)} hard-filtered | {len(repost_skipped)} repost-skipped | {len(errors)} errors"
    )
    if reposts:
        n = len(reposts)
        extra = []
        if applied_reposts:
            extra.append(f"🚫 {applied_reposts} ALREADY APPLIED")
        if passed_reposts:
            extra.append(f"↩ {passed_reposts} previously passed")
        lines.append(
            f"↻ **{n} repost{'s' if n != 1 else ''}** of roles already seen"
            + (" · " + " · ".join(extra) if extra else "")
        )
    lines.append("")

    lines.append("## ✅ Cold-apply (PASS) — worth your read (triage, not verdict)")
    lines.append("")
    if not passes:
        lines.append("*None today.*")
    for r in passes:
        lines.extend(_render_scored_job(r, decisions[r["job_url"]]))

    if recruiter:
        lines.append("## 🤝 Recruiter-only — route to a human, do NOT cold-apply")
        lines.append("")
        lines.append(
            "*Passed every gate and scored well, but the artifact is a generation behind the "
            "role's **required** AI depth (artifact-depth 0). An ATS screen filters these out; "
            "a recruiter or referral can carry the ramp narrative. This is the 50/0 fix.*"
        )
        lines.append("")
        for r in recruiter:
            lines.extend(_render_scored_job(r, decisions[r["job_url"]]))

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(decisions[r['job_url']])}{_source_tag(r)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']])}{_source_tag(r)}: `{r['failed_gate']}` — "
            f"{ev.get('gate_notes', '')} · [link]({r['job_url']})"
        )
    lines.append("")

    if hard_filtered:
        lines.append("## 🚫 Hard-fail filters (your rules + manual rejects)")
        lines.append("")
        lines.append(
            "*Auto-failed by a `filters.yaml` rule or rejected by you — overrides the model. "
            "Skim to catch an over-aggressive rule.*"
        )
        lines.append("")
        for r in hard_filtered:
            src = r["filter_source"] or ""
            tag = f"`rule: {src[5:]}`" if src.startswith("rule:") else "`manual`"
            note = " (model under-filtered)" if (src == "manual" and r["verdict"] in ("PASS", "RECRUITER_ONLY")) else ""
            # _repost_tag keeps the ALREADY APPLIED / passed / repost marker visible here too,
            # so a rule can't silently bury a relisting of a role you already applied to.
            lines.append(
                f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']])}{_source_tag(r)} · {tag} · "
                f"gate `{r['filter_gate']}`{note} · [link]({r['job_url']})"
            )
        lines.append("")

    if errors:
        lines.append("## ⚠️ Evaluation errors (re-run `python pipeline.py run` to retry is NOT automatic — check log)")
        for r in errors:
            lines.append(f"- {r['title']} — {r['company']} · [link]({r['job_url']})")
        lines.append("")

    out_dir = BASE_DIR / cfg["settings"]["reports_dir"]
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"report_{d}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] written: {out_path}")


def _repost_info(dec):
    """For a posting, return (banner_lines, effective_status) for the report. `dec` is the chain's
    decision from chain.effective_decision(s) — the single source of truth, shared with the web UI
    and the dupe guard — so this function only FORMATS it into markdown (it is conn-free: the caller
    fetches all decisions once, batched, and passes each in). `effective_status` is 'applied',
    'passed', or None (applied outranks passed across the chain); `banner_lines` are the matching
    markdown lines (loud for applied, quiet for passed) plus the repost note."""
    status = dec["app_status"]
    lines = []
    if status == "applied":
        lines.append(f"- 🚫 **ALREADY APPLIED** ({dec['status_date']}) — do not re-apply")
    elif status == "passed":
        lines.append(f"- ↩ You reviewed & passed on {dec['status_date']} — skip unless reconsidering")
    if dec["is_repost"]:
        seen = (dec["original_first_seen"] or "")[:10]
        lines.append(f"- ↻ Repost — original first seen {seen}, prior verdict {dec['original_verdict']}")
    return lines, status


def _repost_tag(dec):
    """Compact inline marker for one-liner sections (gate fails, manual review)."""
    _, status = _repost_info(dec)
    if status == "applied":
        return " · 🚫 **ALREADY APPLIED**"
    if status == "passed":
        return " · ↩ passed"
    return " · ↻ repost" if dec["is_repost"] else ""


def _source_tag(r):
    """Source-provenance tag. Adzuna's carries a thin-text warning — it only gives a 500-char
    snippet, so its evals are made on far less text than a LinkedIn JD. ATS-board sources get a
    plain provenance tag: their descriptions are full text, so there is no caveat to flag."""
    # Tolerate rows from a SELECT that omits `source` (this file mixes Row and dict rows).
    source = r["source"] if "source" in r.keys() else None
    if source == "adzuna":
        return " · 📋 adzuna (500-char snippet — verdict on thin text)"
    # Same rule as the UI's meta line (index.html): any non-LinkedIn source gets a plain
    # provenance tag, so a future board added in fetch.py can't drift out of the report.
    if source and source != "linkedin":
        return f" · 🏢 {source}"
    return ""


BUCKET_LABELS = {
    1: "Bucket 1 — required AI depth a generation ahead (recruiter/referral)",
    2: "Bucket 2 — acceptable-tier BI/BA (cold-apply where title gap is small)",
    3: "Bucket 3 — clean low-code / Power Platform AI delivery (cold-apply)",
}


def score_band(score):
    """Fit-score band label (out of 18). The single definition of the thresholds, shared by the
    report (_render_scored_job) and the web UI (app.row_to_dict) so the two can't disagree."""
    s = score or 0
    return "strong" if s >= 14 else ("acceptable" if s >= 10 else "likely pass")


def _render_scored_job(r, dec):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines. `dec` is the row's
    precomputed chain decision (see _repost_info)."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = score_band(score)
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band})"]
    out.extend(_repost_info(dec)[0])
    out.append(f"- {r['location']}  ·  tier: {r['tier']}  ·  search: `{r['search_name']}`{_source_tag(r)}")
    if r["bucket"]:
        out.append("- " + BUCKET_LABELS.get(r["bucket"], "Bucket " + str(r["bucket"])))
    if r["salary_min"] or r["salary_max"]:
        out.append(f"- Posted salary: {_fmt_sal(r['salary_min'])}–{_fmt_sal(r['salary_max'])}")
    out.append(f"- {ev.get('one_line', '')}")
    bd = ev.get("score_breakdown") or {}
    if bd:
        out.append("- Scores: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in bd.items()))
    for fl in ev.get("flags") or []:
        out.append(f"- ⚠️ {fl}")
    out.append(f"- [Posting]({r['job_url']})")
    out.append("")
    return out


def _fmt_sal(v):
    return f"${int(v):,}" if v else "?"
