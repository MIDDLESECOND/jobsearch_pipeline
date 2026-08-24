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

import hashlib
import json
from datetime import date, datetime, time

from core import (ADZUNA_SNIPPET_MAX_CHARS, BASE_DIR, PARSE_MIN, PARSE_MAX, parse_iso,
                  recency_dt)
from chain import effective_decisions, _norm_company, _norm_title
from health import failed_fetch_targets, staleness_readings
from second_judge import opinion_summaries
from states import (VERDICT_PASS, VERDICT_GATE_FAIL, VERDICT_RECRUITER_ONLY, VERDICT_FAVOR,
                    STATUS_NEEDS_MANUAL, STATUS_NEW, STATUS_ERROR, STATUS_SALARY_FILTERED,
                    STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED)


def generate_report(cfg, conn, for_date=None, *, maps=None):
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
    # The two corpus-wide evidence maps (see corpus_maps). Injected the same way `decisions`
    # is, and for the same reason one step up: they do not depend on `d`, so a caller
    # rebuilding several days computes them ONCE instead of re-scanning 240 MB of description
    # text per day. `maps=None` keeps the single-day callers (`report --date`, the tests)
    # working with no ceremony.
    maps = corpus_maps(conn) if maps is None else maps
    floors, ft_readings = maps["floors"], maps["ft_readings"]

    # Hard-fail overrides (your rules + manual rejects) are pulled out first so they don't
    # also appear under their model verdict (a manual reject keeps its original PASS verdict).
    hard_filtered = [r for r in rows if r["filter_source"]]
    passes = [r for r in rows if r["verdict"] == VERDICT_PASS and not r["filter_source"]]
    recruiter = [r for r in rows if r["verdict"] == VERDICT_RECRUITER_ONLY and not r["filter_source"]]
    fails = [r for r in rows if r["verdict"] == VERDICT_GATE_FAIL and not r["filter_source"]]
    manual = [r for r in rows if r["status"] == STATUS_NEEDS_MANUAL and not r["filter_source"]]
    # `not filter_source` on every bucket below: a rejected row belongs under Hard-fail
    # filters only (hard_filtered selects on filter_source alone, regardless of status) —
    # counting it in a status bucket too would render it twice and make the summary's
    # buckets sum past the postings total. _REJECT_SET keeps the status of already-parked
    # rows (error/salary_filtered/repost_decided), so the overlap is reachable for all four.
    errors = [r for r in rows if r["status"] == STATUS_ERROR and not r["filter_source"]]
    salary_filtered = [r for r in rows
                       if r["status"] == STATUS_SALARY_FILTERED and not r["filter_source"]]
    repost_skipped = [r for r in rows
                      if r["status"] == STATUS_REPOST_DECIDED and not r["filter_source"]]
    # Rows whose chain already holds a judge verdict — eval skipped, verdict read through the
    # chain (dec["chain_verdict"]). Shown most-favorable-first so a PASS relisting still surfaces.
    repost_evaluated = sorted(
        (r for r in rows if r["status"] == STATUS_REPOST_EVALUATED and not r["filter_source"]),
        key=lambda r: VERDICT_FAVOR.get(decisions[r["job_url"]]["chain_verdict"], -1),
        reverse=True,
    )

    reposts = [r for r in rows if r["repost_of"]]
    repost_status = [decisions[r["job_url"]]["app_status"] for r in reposts]
    applied_reposts = sum(s == "applied" for s in repost_status)
    passed_reposts = sum(s == "passed" for s in repost_status)

    # Rows still awaiting the paid eval when this rendered. NO bucket above can hold them
    # (each selects on a verdict or a terminal status), so the headline count would exceed
    # the sections' sum with nothing saying why. Used to mean "the eval crashed"; since the
    # peak-rate deferral (pipeline._defer_eval_for_peak) it is a normal twice-a-day state.
    # Transient by design: `run` rebuilds this day's report once these rows are evaluated,
    # and the line disappears — an unexplained gap must never be the resting state.
    awaiting = [r for r in rows if r["status"] == STATUS_NEW]
    lines = [f"# Job Pipeline Report — {d}", ""]
    # The health warning renders at the very top: this report is the ONE surface a human
    # reads every day, and the 2026-08-18 seam audit showed a dead schedule (a missing log
    # day, a canary that never ran) was otherwise invisible — report.py previously carried
    # no health information at all. Staleness is a now-fact, so it enters TODAY's render
    # only: stamping today's readings into a `report --date` rebuild of a past day would be
    # an anachronism and would break the rebuild-stability contract the age anchor above
    # establishes. The day's failed fetch targets are a day-fact (health's per-target
    # attempt records — no new storage) and render for any date.
    failed_targets = failed_fetch_targets(conn, d)
    warning = _health_warning_line(
        staleness_readings(conn) if d_date == today else None,
        len(failed_targets), sorted({t["source_family"] for t in failed_targets}))
    if warning:
        lines.append(warning)
        lines.append("")
    lines.append(
        f"**{len(rows)} new postings** | {len(passes)} cold-apply (PASS) | "
        f"{len(recruiter)} recruiter-only | {len(fails)} gate fails | "
        f"{len(manual)} need manual review | {len(salary_filtered)} salary-filtered | "
        f"{len(hard_filtered)} hard-filtered | {len(repost_skipped) + len(repost_evaluated)} repost-skipped | {len(errors)} errors"
        + (f" | **{len(awaiting)} awaiting evaluation**" if awaiting else "")
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
        lines.extend(_render_scored_job(r, decisions[r["job_url"]], now, floors, ft_readings))

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
            lines.extend(_render_scored_job(r, decisions[r["job_url"]], now, floors, ft_readings))

    lines.extend(_second_opinion_lines(conn, passes + recruiter))

    if repost_evaluated:
        # Not necessarily reposts: a still-'new' CANONICAL whose verdict sits on a sibling
        # (requeued error row, dupe merge) lands here too — the title says "roles", not
        # "reposts", so the header never asserts a repost relationship a row doesn't have.
        lines.append("## ↻ Already-evaluated roles seen again (eval skipped)")
        lines.append("")
        lines.append(
            "*The role's chain already holds a verdict, so these postings were not re-scored "
            "(re-evaluating re-samples a noisy judge and costs money). Verdict shown is the "
            "chain's most favorable — a PASS here is still worth a look if you never acted on it.*"
        )
        lines.append("")
        for r in repost_evaluated:
            dec = decisions[r["job_url"]]
            # 'first seen' only for actual reposts: for a skipped canonical the original's
            # first_seen IS its own, and _age_tag already covers that date.
            seen = f" · first seen {_seen_day(dec)}" if dec["is_repost"] else ""
            lines.append(
                f"- **{r['title']} — {r['company']}** · chain verdict **{dec['chain_verdict'] or '?'}**"
                f"{_repost_tag(dec, r)}{_source_tag(r)}{_age_tag(r, now, floors)}{seen} · [link]({r['job_url']})"
            )
        lines.append("")

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(decisions[r['job_url']], r)}{_source_tag(r)}{_age_tag(r, now, floors)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        # A contract diagnostic matters MOST here and was previously rendered only for
        # gates-passed rows: a GATE_FAIL whose own gate table says everything passed may
        # be a mis-rejected role, and a rejected role never surfaces anywhere else — the
        # silent, irreversible direction of the judge's ~25% verdict instability.
        issues = ev.get("eval_issues") or []
        issue_tag = f" · 🔎 **{', '.join(issues)}** — verify before trusting this rejection" if issues else ""
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']], r)}{_source_tag(r)}{_age_tag(r, now, floors)}: `{r['failed_gate']}` — "
            f"{ev.get('gate_notes', '')}{issue_tag} · [link]({r['job_url']})"
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
            # chain_verdict, not the row's own: a rejected eval-skipped relisting carries
            # verdict NULL by design while the judge's PASS sits on a sibling — the chain
            # reading is the one that says "you overruled the model" (one verdict per chain).
            note = (" (model under-filtered)"
                    if (src == "manual" and decisions[r["job_url"]]["chain_verdict"]
                        in (VERDICT_PASS, VERDICT_RECRUITER_ONLY)) else "")
            # _repost_tag keeps the ALREADY APPLIED / passed / repost marker visible here too,
            # so a rule can't silently bury a relisting of a role you already applied to.
            lines.append(
                f"- **{r['title']} — {r['company']}**{_repost_tag(decisions[r['job_url']], r)}{_source_tag(r)}{_age_tag(r, now, floors)} · {tag} · "
                f"gate `{r['filter_gate']}`{note} · [link]({r['job_url']})"
            )
        lines.append("")

    if errors:
        lines.append("## ⚠️ Evaluation errors (re-run `python pipeline.py run` to retry is NOT automatic — check log)")
        for r in errors:
            # Age matters most here: a fresh strong posting stuck in error is the one worth
            # a manual look right now.
            lines.append(f"- {r['title']} — {r['company']}{_age_tag(r, now, floors)} · [link]({r['job_url']})")
        lines.append("")

    out_dir = BASE_DIR / cfg["settings"]["reports_dir"]
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"report_{d}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] written: {out_path}")


# Wording per staleness signal for the daily health line: (aged-phrase prefix, phrase for a
# signal with no readable stamp at all). Deliberately neutral fact-statements — the sentinel
# reports, it never diagnoses intent (an unregistered schedule may be a choice).
_SILENCE_PHRASES = {
    "pipeline_run": ("last successful run", "no successful run on record"),
    "canary": ("canary last ran", "canary: no run history on record"),
    "second_judge": ("second judge last collected", "second judge: nothing collected on record"),
}


def _health_warning_line(staleness, failed_target_count, failed_sources):
    """Compose the report's one conditional health line, or None when nothing warrants it.

    Pure over its inputs so the two states are testable without a clock: `staleness` is
    health.staleness_readings() output (or None when suppressed — a past-date rebuild),
    `failed_target_count`/`failed_sources` come from the day's per-target attempt facts.
    The quiet state stays quiet: a healthy day renders no line at all, so the warning
    keeps its signal value on the day it finally appears.
    """
    parts = []
    for reading in (staleness or {}).get("readings", []):
        if not reading["stale"]:
            continue
        prefix, never = _SILENCE_PHRASES.get(
            reading["signal"],
            (f"{reading['signal']} last seen", f"{reading['signal']}: nothing on record"))
        if reading["age_hours"] is None:
            parts.append(never)
        else:
            # age_hours is source-clamped to >=0, and stale implies age > a positive
            # threshold anyway, so this branch never sees a negative span.
            parts.append(f"{prefix} {_span_label(reading['age_hours'])}")
    if failed_target_count:
        plural = "s" if failed_target_count != 1 else ""
        names = f" ({', '.join(failed_sources)})" if failed_sources else ""
        parts.append(f"{failed_target_count} fetch target{plural} failed{names}")
    if not parts:
        return None
    return "⚠ pipeline health: " + "; ".join(parts)


def _second_opinion_lines(conn, rows):
    """The second judge's read of today's interesting zone (second_judge.py) —
    evidence rendered BESIDE the primary verdicts, never a replacement for them.
    The summaries come from second_judge.opinion_summaries — the SAME chain-scoped
    read the UI card uses, sharing one disagreement definition and one note cut —
    so the two surfaces can't drift. Disagreements render loud (⬇/⬆ with the
    second judge's reason); agreement collapses to a tally; a day whose opinions
    are all still pending renders NOTHING (the section only earns attention once
    there is a judgment or an error to look at — the report never waits on the
    second judge)."""
    ops = opinion_summaries(conn, rows)
    demote, promote, agree, pending, errors = [], [], 0, 0, 0
    models = set()
    for r in rows:
        o = ops[r["job_url"]]
        if o is None:
            continue
        # 'retry' is a released error awaiting its bounded re-attempt: nothing to read
        # yet, and the exhausted ones stay 'error', so it counts as in-flight here.
        if o["status"] in ("pending", "submitting", "retry"):
            pending += 1
            continue
        if o["status"] != "done":
            errors += 1
            continue
        models.add(o["model"])
        if o["direction"] == "demote":
            demote.append((r, o))
        elif o["direction"] == "promote":
            promote.append((r, o))
        else:
            agree += 1
    if not (demote or promote or agree or errors):
        return []
    judge = ", ".join(sorted(models))
    lines = ["## 🧑‍⚖️ Second opinion — review layer, primary verdicts unchanged", ""]
    lines.append(
        f"*{agree + len(demote) + len(promote)} reviewed · {agree} agree · "
        f"{len(demote)} demoted · {len(promote)} promoted · {pending} pending · "
        f"{errors} error(s). Opinions are triage evidence from the second judge"
        f"{f' ({judge})' if judge else ''}; the sections above keep the primary verdicts.*"
    )
    lines.append("")
    for tag, pairs in (("⬇", demote), ("⬆", promote)):
        for r, o in pairs:
            why = o["note"] or ""
            second_fit = o["fit_score"] if o["fit_score"] is not None else "—"
            lines.append(
                f"- {tag} **{r['title']} — {r['company']}** · "
                f"{r['verdict']}({r['fit_score']}) → **{o['verdict']}({second_fit})**"
                f"{' · ' + why if why else ''} · [link]({r['job_url']})"
            )
    lines.append("")
    return lines


def _seen_day(dec):
    """The chain original's first-seen DAY for display — one owner for the date slice, shared
    by the banner and the eval-skipped section so the two can't format the same fact apart."""
    return (dec["original_first_seen"] or "")[:10] or "unknown"


def _repost_info(dec):
    """For a posting, return (banner_lines, effective_status) for the report. `dec` is the chain's
    decision from chain.effective_decision(s) — the single source of truth, shared with the web UI
    and the dupe guard — so this function only FORMATS it into markdown (it is conn-free: the caller
    fetches all decisions once, batched, and passes each in). `effective_status` is 'applied',
    'passed', or None (applied outranks passed across the chain); `banner_lines` are the matching
    markdown lines (loud for applied, quiet for passed) plus the chain-reject and repost notes."""
    status = dec["app_status"]
    lines = []
    if status == "applied":
        # Append the chain's recorded outcome when one exists (the cached latest app_event —
        # chain._recompute_outcome), so the banner says not just "applied" but where the
        # application stands: (2026-06-20 · interview 2026-07-01).
        out = (f" · {dec['outcome_status'].replace('_', ' ')} {dec['outcome_date']}"
               if dec["outcome_status"] else "")
        lines.append(f"- 🚫 **ALREADY APPLIED** ({dec['status_date']}{out}) — do not re-apply")
    elif status == "passed":
        lines.append(f"- ↩ You reviewed & passed on {dec['status_date']} — skip unless reconsidering")
    elif dec["reject"]:
        # A chain reject can sit entirely on a SIBLING (a filters.yaml rule stamps only the row
        # it matched), so this scored card's own filter_source is NULL and it renders under its
        # verdict — without this line the card would invite applying to a role the chain rejects.
        lines.append(f"- 🚫 Chain rejected (gate `{dec['filter_gate']}`) — a rule or manual "
                     f"reject applies to this role; skim before acting")
    if dec["is_repost"]:
        # chain_verdict, not the canonical's own verdict: the canonical may be verdict-NULL
        # (salary-filtered) while a sibling holds the judge's answer — one verdict reading per
        # chain, everywhere. Clause suppressed when no judge ever scored the chain.
        v = dec["chain_verdict"]
        lines.append(f"- ↻ Repost — original first seen {_seen_day(dec)}"
                     + (f", chain verdict {v}" if v else ""))
    return lines, status


def _repost_tag(dec, row=None):
    """Compact inline marker for one-liner sections (gate fails, manual review). Pass the row
    when available: the reject marker exists to flag a reject living on a SIBLING, so it is
    suppressed for a row whose OWN filter_source is set (the Hard-fail section already labels
    those with their `rule:`/`manual` tag — repeating it there is noise)."""
    _, status = _repost_info(dec)
    if status == "applied":
        return " · 🚫 **ALREADY APPLIED**"
    if status == "passed":
        return " · ↩ passed"
    own_filter = row is not None and "filter_source" in row.keys() and row["filter_source"]
    if dec["reject"] and not own_filter:
        # A chain-wide reject can sit on a sibling while this row renders under its own verdict
        # — without the marker a rejected role reads as still-actionable.
        return " · 🚫 rejected" + (" · ↻ repost" if dec["is_repost"] else "")
    return " · ↻ repost" if dec["is_repost"] else ""


def _source_tag(r):
    """Source-provenance tag. Adzuna's carries two caveats: it only gives a 500-char snippet
    (so its evals are made on far less text than a LinkedIn JD), and Adzuna keeps listing
    delisted jobs — verified empirically; the details page of a dead ad says "no longer
    available", so opening the link before applying IS the liveness check (automated
    verification was rejected: the web side 403s scripts and the volume is thousands of rows
    per run). ATS-board sources get a plain provenance tag: their descriptions are full text
    and a board only returns open roles, so there is no caveat to flag."""
    # Tolerate rows from a SELECT that omits `source` (this file mixes Row and dict rows).
    source = r["source"] if "source" in r.keys() else None
    if source == "adzuna":
        return " · 📋 adzuna (500-char snippet — may be stale; verify on employer site)"
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


# The effective posted-at reading (formerly defined here) lives in core.recency_dt now,
# beside parse_iso — second_judge's freshness window shares it, and importing report from
# there would invert the DAG. The alias keeps this module's call sites unchanged.
_recency_dt = recency_dt


def _span_label(hours):
    """Compact age span: '3h ago' / '2d ago' / '3mo ago' (caller handles <1h)."""
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours // 24)
    return f"{days}d ago" if days < 60 else f"{days // 30}mo ago"


EVERGREEN_MIN_GAP_DAYS = 3   # below this a floor is noise, not a re-dating signal


def posting_age(date_posted, first_seen, now=None, floor=None):
    """Human posting-age label: 'just now' / '3h ago' / '2d ago' for real timestamps;
    'seen 3h ago' when first_seen is standing in (no usable posting date, or a calendar date
    at/after the fetch day — either way a lower bound, never claimed as posting time);
    '2d ago' day-granularity for older date-only postings (never fake hour precision);
    '' when nothing is usable. Future/skewed dates clamp to 'just now'. `now` is injectable
    for tests and for date-anchored report rebuilds.

    `floor` (a date, optional) is the EVERGREEN FLOOR: the earliest `date_posted` carried by
    any row whose stored description is byte-identical to this one — i.e. the same posting
    re-stamped with a newer date. When it is at least EVERGREEN_MIN_GAP_DAYS earlier than
    this row's own reading, it is appended rather than substituted.

    Appended, never substituted, for two reasons. It is a LOWER BOUND, not the truth —
    measured 2026-08-20, the floor recovered Wilson Sonsini's R1579 to 40 days against an
    employer `startDate` of 78 days, and missed PNM's evergreen req entirely because that
    snippet appears once. And demoting a row on a floor would let a bound hide a live role,
    which is the one failure this repo's discovery surfaces are not allowed to have (the same
    rule that keeps needs_attention and dupe_candidates presentation-only)."""
    dt, mode = _recency_dt(date_posted, first_seen)
    if mode is None:
        return ""
    now = now or datetime.now()
    if mode == "posted_day":
        days = max((now.date() - dt.date()).days, 0)
        label = "today" if days == 0 else _span_label(days * 24)
    else:
        hours = max((now - dt).total_seconds() / 3600.0, 0.0)
        label = "just now" if hours < 1 else _span_label(hours)
        if mode == "seen":
            label = f"seen {label}"
    if floor is not None:
        gap = (dt.date() - floor).days
        if gap >= EVERGREEN_MIN_GAP_DAYS:
            age = max((now.date() - floor).days, 0)
            label = f"{label} (same JD dated ≥{age}d ago)"
    return label


def recency_sort_key(row, fit=None):
    """The single two-band triage sort key (ascending sort), shared by the report and the web
    UI's today/backlog views. At/above APPLY_LINE: freshest-first, fit tiebreak. Below: fit-only,
    freshness as final tiebreak. Rows with no usable timestamp sort last within their band.
    `fit` overrides the row's own fit_score — the UI passes the chain's fit for eval-skipped
    rows (own fit NULL), so a relisting of a strong role doesn't sink to the bottom band."""
    if fit is None:
        fit = row["fit_score"] or 0
    dt, _ = _recency_dt(row["date_posted"], row["first_seen"])
    # Dead by construction — parse_iso range-checks every value and the sentinel IS the
    # floor — and kept anyway: if parse_iso's window is ever loosened or broken, this clamp
    # is what stops the Windows OSError (mktime range) from resurfacing as a crash in the
    # unguarded, post-paid-eval report stage.
    epoch = min(max(dt, PARSE_MIN), PARSE_MAX).timestamp()
    above = fit >= APPLY_LINE
    return (0 if above else 1, -epoch if above else 0.0, -fit, -epoch)


def evergreen_floors(conn):
    """{sha256(description) -> earliest `date_posted` anywhere for that exact text}.

    Rows whose stored description is byte-identical but whose `date_posted` differs are one
    requisition re-stamped with a fresher date — the shape that let a posting open since Jun 3
    read as "1 day old" and clear the cold-apply bar. Measured on the live corpus 2026-08-20:
    4,748 identical-JD Adzuna groups carry a nonzero date spread, 46% of them wider than the
    entire 14-day window; inside the actionable zone it is 85 rows / 40 distinct texts, i.e.
    17% of rows and 10% of requisitions.

    Whole-corpus scan — measured 2026-08-23 at 1.5s over 107k rows / 240 MB of description
    text, which is why the per-request web UI does not use it and why corpus_maps exists to
    compute it ONCE per render batch rather than once per rendered day. Rows without a
    description or without a `date_posted` cannot contribute a floor and are skipped, so the
    map is a LOWER BOUND on its own coverage as well as on each date.

    The key is the TEXT, not the employer, and 13.4% of floor-producing groups span more than
    one company string (measured 2026-08-23: 699 of 5,203). Both kinds are in there and both
    are wanted. Most are one employer under several spellings — `cla` / `clifton larson allen`
    / `cliftonlarsonallen`, `jpmorgan chase` / `jpmorgan chase bank n a` — which the
    company+title fingerprint splits into separate chains and this grouping sees through. The
    rest are genuinely different posters sharing byte-identical text, i.e. staffing agencies
    relisting one client requisition, which is the evergreen signal itself. The label says
    "same JD", not "same employer", and it is appended rather than substituted, so a pooled
    group can only add a caveat — never demote or hide a row.
    """
    floors = {}
    for desc, dp in conn.execute(
        "SELECT description, date_posted FROM jobs "
        "WHERE description IS NOT NULL AND length(description) > 200 "
        "AND date_posted IS NOT NULL AND date_posted <> ''"
    ):
        parsed = parse_iso(dp)
        if not parsed:
            continue
        day = parsed[0].date()
        key = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        if key not in floors or day < floors[key]:
            floors[key] = day
    return floors


def corpus_maps(conn):
    """The whole-corpus evidence maps every render needs, computed together, once.

    Both are cross-row groupings over the ENTIRE jobs table and neither depends on which day
    is being rendered, so the unit of work is the render BATCH, not the report. `pipeline.py`
    rebuilds several days in one pass whenever rows were evaluated late (peak deferral, error
    requeue, a fetch that outran the eval), and computing these inside generate_report re-ran
    both scans per day for byte-identical results. Measured 2026-08-23 on the live corpus:
    1.5s + 1.0s over 107k rows / 240 MB of description text.

    Returned as a dict rather than a tuple so a third map can be added without touching every
    call site's unpacking."""
    return {"floors": evergreen_floors(conn), "ft_readings": fulltext_readings(conn)}


def floor_for(r, floors):
    """This row's evergreen floor, or None when floors is unset, the row has no usable text,
    or its text is unique in the corpus (the common case — most postings appear once)."""
    if not floors:
        return None
    desc = r["description"] if "description" in r.keys() else None
    if not desc or len(desc) <= 200:
        return None
    return floors.get(hashlib.sha256(desc.encode("utf-8")).hexdigest())


FULLTEXT_MIN_CHARS = 2000  # the fuller reading must be MEANINGFULLY fuller, not another stub
CONTRA_FIT_MARGIN = 2      # within-verdict: the full-text fit must trail by at least this
# The snippet bound is core.ADZUNA_SNIPPET_MAX_CHARS, imported rather than respelled: it is
# the ONE reading of "this row's stored text is a truncation", already shared by
# second_judge.pending_rows and notify_deepdive_batch.zone_rows. Verdict ranking is
# states.VERDICT_FAVOR for the same reason — states.py owns that list, and a local copy would
# rank a newly added verdict silently (this file already imports and uses it above).


def fulltext_readings(conn):
    """{(normalized company, normalized title) -> most favorable FULL-TEXT (verdict, fit)}.

    The evidence-inversion hazard this feeds (measured 2026-08-20): snippet-scored rows run
    systematically hot — the same WSGR requisition scored PASS 16 from a 500-char Adzuna
    snippet and RECRUITER_ONLY 12 from the 9,327-char board text the same day, and of 2,167
    undecided snippet PASS rows at the cold-apply bar, 403 (19%) had a full-text reading of
    the same role saying strictly worse. The snippet rows carry the inflated number into
    every ranked surface while the full-text verdict sits in a chain nobody opens.

    Taking the MOST FAVORABLE full-text reading per role is the conservative direction: if
    ANY full read clears the snippet's claim, no flag — e.g. Holland & Knight's AI Legal
    Engineer carried two full-text GATE_FAILs and later a full-text PASS 16, which rightly
    silences the flag. Same corpus-scan shape as evergreen_floors, for the same reason (a
    cross-row grouping is not a per-request cost) — see corpus_maps, which owns both.

    The key deliberately OMITS location, unlike chain's fingerprint. One requisition mass-posted
    across many cities therefore collapses into a single reading, and cross-source location
    strings rarely agree anyway (the same evidence trade dupe_candidates' blocking key makes).
    Stated rather than left implicit: the direction is conservative — the most favorable of the
    pooled cities wins, so pooling can only SILENCE a flag, never invent one — but a pooled key
    is a weaker claim than a per-city one and the docstring has to say so.
    """
    readings = {}
    for company, title, verdict, fit in conn.execute(
        "SELECT company, title, verdict, fit_score FROM jobs "
        "WHERE status='evaluated' AND verdict IS NOT NULL "
        "AND description IS NOT NULL AND length(description) > ?",
        (FULLTEXT_MIN_CHARS,),
    ):
        key = (_norm_company(company or ""), _norm_title(title or ""))
        cur = readings.get(key)
        cand = (VERDICT_FAVOR.get(verdict, -1), fit if fit is not None else -1, verdict, fit)
        if cur is None or cand[:2] > cur[:2]:
            readings[key] = cand
    return {k: (v[2], v[3]) for k, v in readings.items()}


def fulltext_contradiction(r, readings):
    """The better-evidence reading this snippet row's judgment must answer to, or None.

    Fires only when THIS row was actually scored on a truncation — an ADZUNA row whose stored
    text is <= ADZUNA_SNIPPET_MAX_CHARS — and the role's most favorable full-text reading is
    strictly less favorable: a lower verdict, or the same verdict trailing by >=
    CONTRA_FIT_MARGIN fit points. Surfacing only, exactly like the evergreen floor: the row
    keeps its own verdict and score, and nothing routes or filters on this — the reader is
    being told a fuller read of the same role exists and what it said, so the snippet's number
    stops being the only voice on ranked surfaces.

    The source leg matches the two other readings of this bound (second_judge.pending_rows,
    notify_deepdive_batch.zone_rows): a terse but COMPLETE ATS or Dice JD under the bound was
    scored on whole evidence, so telling its card "snippet-scored" would be the mirror of the
    short-but-complete-JD hazard those two guard with the same clause. A row missing the
    column reads as not-adzuna — the silent direction.

    NO OBSERVED TRIGGER, said out loud because in this repo a guard is read by default as
    backed by an incident. Measured 2026-08-23 over the live corpus: of 58,425 evaluated rows
    at/under the bound, 58,295 are adzuna and the 130 that are not (120 linkedin, 10 dice)
    contribute ZERO flags either way — a short non-adzuna row needs a >FULLTEXT_MIN_CHARS
    sibling reading of the same company+title to flag at all, and none has one. This leg
    changes nothing today; it is here so the label cannot start lying as the ATS/Dice lanes
    grow, which is the direction they are growing (five board families as of 2026-08-20).
    """
    if not readings or not r["verdict"]:
        return None
    keys = r.keys()
    source = (r["source"] if "source" in keys else "") or ""
    desc = r["description"] if "description" in keys else None
    if source != "adzuna" or not desc or len(desc) > ADZUNA_SNIPPET_MAX_CHARS:
        return None
    ft = readings.get((_norm_company(r["company"] or ""), _norm_title(r["title"] or "")))
    if ft is None:
        return None
    ft_verdict, ft_fit = ft
    row_q = VERDICT_FAVOR.get(r["verdict"], -1)
    ft_q = VERDICT_FAVOR.get(ft_verdict, -1)
    if ft_q < row_q:
        return ft
    if (ft_q == row_q and ft_fit is not None and r["fit_score"] is not None
            and ft_fit <= r["fit_score"] - CONTRA_FIT_MARGIN):
        return ft
    return None


def _age_tag(r, now=None, floors=None):
    """Compact inline posting-age marker for one-liner sections (mirrors _source_tag, including
    its guard — this file mixes Row and dict rows, and dicts may omit the columns). `now` is
    the report's date anchor (see generate_report); None = wall clock (the live UI)."""
    dp = r["date_posted"] if "date_posted" in r.keys() else ""
    fs = r["first_seen"] if "first_seen" in r.keys() else ""
    label = posting_age(dp, fs, now=now, floor=floor_for(r, floors))
    return f" · 🕐 {label}" if label else ""


def _render_scored_job(r, dec, now=None, floors=None, ft_readings=None):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines. `dec` is the row's
    precomputed chain decision (see _repost_info); `now` the report's date anchor."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = score_band(score)
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band}){_age_tag(r, now, floors)}"]
    out.extend(_repost_info(dec)[0])
    out.append(f"- {r['location']}  ·  tier: {r['tier']}  ·  search: `{r['search_name']}`{_source_tag(r)}")
    if r["bucket"]:
        out.append("- " + BUCKET_LABELS.get(r["bucket"], "Bucket " + str(r["bucket"])))
    if r["salary_min"] or r["salary_max"]:
        out.append(f"- Posted salary: {_fmt_sal(r['salary_min'])}–{_fmt_sal(r['salary_max'])}")
    # Evaluation-quality diagnostics ride WITH the verdict, above the role's own
    # caveats: they qualify how much to trust this card, not what the job is like.
    for iss in ev.get("eval_issues") or []:
        out.append(f"- 🔎 eval quality: {iss}")
    contra = fulltext_contradiction(r, ft_readings)
    if contra:
        cv, cf = contra
        detail = f"{cv} {cf}/18" if cf is not None else cv
        out.append(f"- 🔎 eval quality: snippet-scored — the full-text read of this role says {detail}")
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
