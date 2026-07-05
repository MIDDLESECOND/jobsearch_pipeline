#!/usr/bin/env python3
"""Daily markdown report: groups the day's postings by verdict/status and renders each. Reports are
DISPOSABLE derivations of jobs.db (the single source of truth) — never reconstruct state from them.
The chain-wide "what has the user decided?" question is answered by chain.effective_decision (the
same function the web UI uses), so the report and UI can't drift. Imports core (BASE_DIR) and chain.

Ordering contract (shared with the web UI via recency_sort_key): within each section, postings at
or above the APPLY_LINE fit score sort freshest-first (posting age, fit tiebreak) — early
application is what gets a strong match seen; below the line, fit-only. Recency is triage
metadata: it is never an eval-prompt input and never a filter.
"""

import json
from datetime import date, datetime, time

from core import BASE_DIR, PARSE_MIN, PARSE_MAX, parse_iso
from chain import effective_decisions
from states import (VERDICT_PASS, VERDICT_GATE_FAIL, VERDICT_RECRUITER_ONLY,
                    STATUS_NEEDS_MANUAL, STATUS_ERROR, STATUS_SALARY_FILTERED,
                    STATUS_REPOST_DECIDED)


def generate_report(cfg, conn, for_date=None):
    # Own the date contract HERE, not at the callers: for_date is parsed once at entry, so a
    # malformed value fails immediately with a clear ValueError before any work is done —
    # never mid-render at the anchor line below. `today` is read once and reused: a second
    # date.today() at the anchor could disagree with `d` across a midnight boundary and
    # silently anchor every age label to yesterday-23:59:59.
    today = date.today()
    d_date = date.fromisoformat(str(for_date)) if for_date else today
    d = d_date.isoformat()
    # No ORDER BY: the Python sort below is the single owner of ordering (the mixed date_posted
    # formats and the first_seen fallback in _recency_dt aren't expressible as a sane ORDER BY).
    rows = conn.execute(
        "SELECT * FROM jobs WHERE substr(first_seen,1,10)=?", (d,)
    ).fetchall()
    # Every section below is a filter over `rows`, so all inherit the two-band order for free.
    rows = sorted(rows, key=recency_sort_key)
    # Age labels are anchored to the report's date, not the wall clock, so rebuilding a past
    # report (`report --date`) is STABLE across rebuilds instead of re-aging every posting to
    # "Nd ago". (Not identical to the original file — that rendered with the intra-day clock;
    # this anchors to end-of-day.) Today's report uses the real clock.
    now = datetime.now() if d_date == today else datetime.combine(d_date, time(23, 59, 59))
    # Fetch every chain decision ONCE (batched), then pass each row's `dec` into the pure render
    # helpers below — same "inject the decision, don't fetch it" shape app.row_to_dict uses. Calling
    # effective_decision per row inside the render loops was an N+1 (one query per posting rendered).
    decisions = effective_decisions(conn, rows)

    # Hard-fail overrides (your rules + manual rejects) are pulled out first so they don't
    # also appear under their model verdict (a manual reject keeps its original PASS verdict).
    hard_filtered = [r for r in rows if r["filter_source"]]
    passes = [r for r in rows if r["verdict"] == VERDICT_PASS and not r["filter_source"]]
    recruiter = [r for r in rows if r["verdict"] == VERDICT_RECRUITER_ONLY and not r["filter_source"]]
    fails = [r for r in rows if r["verdict"] == VERDICT_GATE_FAIL and not r["filter_source"]]
    manual = [r for r in rows if r["status"] == STATUS_NEEDS_MANUAL and not r["filter_source"]]
    errors = [r for r in rows if r["status"] == STATUS_ERROR]
    salary_filtered = [r for r in rows if r["status"] == STATUS_SALARY_FILTERED]
    repost_skipped = [r for r in rows if r["status"] == STATUS_REPOST_DECIDED]

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
        lines.extend(_render_scored_job(r, decisions[r["job_url"]], now))

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
            lines.extend(_render_scored_job(r, decisions[r["job_url"]], now))

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(decisions[r['job_url']])}{_source_tag(r)}{_age_tag(r, now)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']])}{_source_tag(r)}{_age_tag(r, now)}: `{r['failed_gate']}` — "
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
            note = " (model under-filtered)" if (src == "manual" and r["verdict"] in (VERDICT_PASS, VERDICT_RECRUITER_ONLY)) else ""
            # _repost_tag keeps the ALREADY APPLIED / passed / repost marker visible here too,
            # so a rule can't silently bury a relisting of a role you already applied to.
            lines.append(
                f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']])}{_source_tag(r)}{_age_tag(r, now)} · {tag} · "
                f"gate `{r['filter_gate']}`{note} · [link]({r['job_url']})"
            )
        lines.append("")

    if errors:
        lines.append("## ⚠️ Evaluation errors (re-run `python pipeline.py run` to retry is NOT automatic — check log)")
        for r in errors:
            # Age matters most here: a fresh strong posting stuck in error is the one worth
            # a manual look right now.
            lines.append(f"- {r['title']} — {r['company']}{_age_tag(r, now)} · [link]({r['job_url']})")
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


# The "acceptable" fit-score floor. It is BOTH score_band's band boundary AND the "hard line"
# of the two-band triage order (at/above it, posting freshness outranks fit — an early
# application is what gets a strong match seen; below it, fit-only), so the sort band and the
# "(acceptable)" label shown next to it can never disagree.
APPLY_LINE = 10


def score_band(score):
    """Fit-score band label (out of 18). The single definition of the thresholds, shared by the
    report (_render_scored_job) and the web UI (app.row_to_dict) so the two can't disagree."""
    s = score or 0
    return "strong" if s >= 14 else ("acceptable" if s >= APPLY_LINE else "likely pass")


# Date parsing lives in core.parse_iso — ONE parser shared with the fetch-side normalizer
# (fetch._ats_date), so the stored shape's producer and consumer can't drift. It is
# range-checked to PARSE_MIN..PARSE_MAX, so absurd placeholder dates ("9999-12-31") degrade
# to the honest 'seen' fallback here instead of crashing .timestamp() on Windows or pinning
# a fake-fresh row to the top of the sort. PARSE_MIN doubles as the sort-last sentinel.


def _recency_dt(date_posted, first_seen):
    """The effective "posted at" instant for a row — the ONE implementation behind both the age
    label (posting_age) and the sort key (recency_sort_key), so the two can't disagree.
    Returns (datetime, mode) with mode 'posted' (real timestamp precision), 'posted_day'
    (day granularity only), 'seen' (first_seen stands in — an explicit LOWER BOUND, which the
    label hedges with a "seen" prefix), or None (nothing usable; datetime is the sort-last
    sentinel):

    - full-timestamp date_posted (Adzuna, ATS) → use it;
    - date-only date_posted at/after first_seen's date → use first_seen, hedged as 'seen':
      it bounds the posting time within the fetch window but is not the posting time, so the
      label must not claim precision the source didn't give. The >= (not ==) also absorbs
      board-timezone calendar dates a day ahead of local time — comparing == would send those
      to the posted_day branch with a FUTURE midnight that pins the row above everything fresh;
    - date-only date_posted OLDER than first_seen's date (ATS backlog, stale relist) → midnight
      of that date — a months-old board posting must not masquerade as fresh just because we
      only saw it today.
    """
    posted = parse_iso(date_posted)
    seen = parse_iso(first_seen)
    if posted:
        pdt, day_only = posted
        if not day_only:
            return pdt, "posted"
        if seen and pdt.date() >= seen[0].date():
            return seen[0], "seen"
        return pdt, "posted_day"
    if seen:
        return seen[0], "seen"
    return PARSE_MIN, None


def _span_label(hours):
    """Compact age span: '3h ago' / '2d ago' / '3mo ago' (caller handles <1h)."""
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours // 24)
    return f"{days}d ago" if days < 60 else f"{days // 30}mo ago"


def posting_age(date_posted, first_seen, now=None):
    """Human posting-age label: 'just now' / '3h ago' / '2d ago' for real timestamps;
    'seen 3h ago' when first_seen is standing in (no usable posting date, or a calendar date
    at/after the fetch day — either way a lower bound, never claimed as posting time);
    '2d ago' day-granularity for older date-only postings (never fake hour precision);
    '' when nothing is usable. Future/skewed dates clamp to 'just now'. `now` is injectable
    for tests and for date-anchored report rebuilds."""
    dt, mode = _recency_dt(date_posted, first_seen)
    if mode is None:
        return ""
    now = now or datetime.now()
    if mode == "posted_day":
        days = max((now.date() - dt.date()).days, 0)
        return "today" if days == 0 else _span_label(days * 24)
    hours = max((now - dt).total_seconds() / 3600.0, 0.0)
    label = "just now" if hours < 1 else _span_label(hours)
    return f"seen {label}" if mode == "seen" else label


def recency_sort_key(row):
    """The single two-band triage sort key (ascending sort), shared by the report and the web
    UI's today/backlog views. At/above APPLY_LINE: freshest-first, fit tiebreak. Below: fit-only,
    freshness as final tiebreak. Rows with no usable timestamp sort last within their band."""
    fit = row["fit_score"] or 0
    dt, _ = _recency_dt(row["date_posted"], row["first_seen"])
    # Dead by construction — parse_iso range-checks every value and the sentinel IS the
    # floor — and kept anyway: if parse_iso's window is ever loosened or broken, this clamp
    # is what stops the Windows OSError (mktime range) from resurfacing as a crash in the
    # unguarded, post-paid-eval report stage.
    epoch = min(max(dt, PARSE_MIN), PARSE_MAX).timestamp()
    above = fit >= APPLY_LINE
    return (0 if above else 1, -epoch if above else 0.0, -fit, -epoch)


def _age_tag(r, now=None):
    """Compact inline posting-age marker for one-liner sections (mirrors _source_tag, including
    its guard — this file mixes Row and dict rows, and dicts may omit the columns). `now` is
    the report's date anchor (see generate_report); None = wall clock (the live UI)."""
    dp = r["date_posted"] if "date_posted" in r.keys() else ""
    fs = r["first_seen"] if "first_seen" in r.keys() else ""
    label = posting_age(dp, fs, now=now)
    return f" · 🕐 {label}" if label else ""


def _render_scored_job(r, dec, now=None):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines. `dec` is the row's
    precomputed chain decision (see _repost_info); `now` the report's date anchor."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = score_band(score)
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band}){_age_tag(r, now)}"]
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
