#!/usr/bin/env python3
"""
LinkedIn job search pipeline.

  fetch -> dedupe (SQLite) -> salary filter -> hard-requirement filters -> LLM gate
  evaluation (Claude or DeepSeek) -> daily markdown report

Usage:
  python pipeline.py run                       # full cycle: fetch + filter + evaluate + report
  python pipeline.py report                     # regenerate today's report only (no fetch, no API calls)
  python pipeline.py stats                      # quick database stats
  python pipeline.py ui                         # local web UI to triage postings (applied/passed/reject)
  python pipeline.py applied --url X            # mark a posting (full URL or unique substring) as applied-to
  python pipeline.py passed  --url X            # mark a posting as reviewed-and-passed
  python pipeline.py reject  --url X --gate G   # override the model: mark a hard-fail it missed
                                                #   (--pattern P also writes a reusable rule to filters.yaml)
  # add --undo to applied / passed / reject to clear what you set

Requires the API key for the configured provider (config.yaml): DEEPSEEK_API_KEY by default,
or ANTHROPIC_API_KEY when provider is "anthropic".
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml

# The foundation (paths, cross-cutting constants, config, the DB open/schema/migration path, the
# API-key resolver) lives in core.py; the repost/content-dedup + decision-chain core in chain.py.
# Both are re-imported into this module's namespace so the pipeline-stage code here — and every
# `pipeline.X` reference from app.py / the tests / the validation scripts — keeps working unchanged.
import chain  # noqa: E402
import core   # noqa: E402,F401
from core import (  # noqa: E402,F401
    BASE_DIR, CONFIG_PATH, PROFILE_PATH, GUIDE_PATH,
    GATE_NAMES, SCORE_DIMS, VERDICTS,
    load_config, get_db, _ensure_api_key,
)
from chain import (  # noqa: E402,F401
    _clean, _norm_company, _norm_title, _norm_location, _fingerprint, _NORM_VERSION,
    _find_repost, skip_decided_reposts, _resolve_posting, _chain_targets, _chain_members,
    _chain_decision, _decision_sig, _fmt_decision, effective_decision,
    propagate_app_status, propagate_reject, clear_reject,
    _dupe_resolve, _dupe_commit, _dupe_unlink,
)


# load_config / get_db + the schema and all migrations moved to core.py (re-imported above).


# The two sources (fetch_new_jobs = LinkedIn scrape, fetch_adzuna = Adzuna API) moved to
# fetch.py; the two entry points are re-imported below for `run`.
from fetch import fetch_new_jobs, fetch_adzuna  # noqa: E402,F401


# Repost / content dedup (normalization, fingerprint, _find_repost) moved to chain.py;
# imported at the top of this module so call sites here read unchanged.


# The deterministic pre-eval filters (apply_salary_filter, and the hard-requirement rules:
# load_filters / save_filters / _pattern_matches / _rule_hit / apply_hard_filters + FILTERS_PATH)
# moved to filters.py; re-imported below (the `reject` rule-writer below still reuses them).
from filters import (  # noqa: E402,F401
    apply_salary_filter, apply_hard_filters,
    load_filters, save_filters, _pattern_matches, _rule_hit, FILTERS_PATH,
)


# skip_decided_reposts (the deterministic pre-eval repost-skip pass) moved to chain.py.


# The LLM gate-check evaluation (system prompt, provider calls, the deterministic 50/0
# routing in normalize_result, and the eval loop) moved to evaluation.py; re-imported for
# `run` and for the validation scripts (backtest_v2 / compare_models) that read these names.
from evaluation import (  # noqa: E402,F401
    SYSTEM_TEMPLATE, MODEL_PRICES, build_system_prompt, parse_eval_json, normalize_result,
    _call_anthropic, _call_deepseek, evaluate_new_jobs,
)


# --------------------------------------------------------------------- report

def generate_report(cfg, conn, for_date=None):
    d = for_date or date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE substr(first_seen,1,10)=? ORDER BY fit_score DESC", (d,)
    ).fetchall()

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
    repost_status = [_repost_info(conn, r)[1] for r in reposts]
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
        lines.extend(_render_scored_job(r, conn))

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
            lines.extend(_render_scored_job(r, conn))

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(conn, r)}{_source_tag(r)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)}{_source_tag(r)}: `{r['failed_gate']}` — "
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
                f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)}{_source_tag(r)} · {tag} · "
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


def _repost_info(conn, r):
    """For a posting, return (banner_lines, effective_status) for the report. Both come from
    chain.effective_decision — the single source of truth for a chain's decision, shared with the
    web UI and the dupe guard — so this function only FORMATS them into markdown. `effective_status`
    is 'applied', 'passed', or None (applied outranks passed across the chain); `banner_lines` are
    the matching markdown lines (loud for applied, quiet for passed) plus the repost note."""
    dec = effective_decision(conn, r)
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


def _repost_tag(conn, r):
    """Compact inline marker for one-liner sections (gate fails, manual review)."""
    lines, status = _repost_info(conn, r)
    if status == "applied":
        return " · 🚫 **ALREADY APPLIED**"
    if status == "passed":
        return " · ↩ passed"
    return " · ↻ repost" if r["repost_of"] else ""


def _source_tag(r):
    """Flag postings from a thin-data source so the verdict's context is visible. Adzuna only
    gives a 500-char snippet, so its evals are made on far less text than a LinkedIn JD."""
    # Tolerate rows from a SELECT that omits `source` (this file mixes Row and dict rows).
    source = r["source"] if "source" in r.keys() else None
    if source == "adzuna":
        return " · 📋 adzuna (500-char snippet — verdict on thin text)"
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


def _render_scored_job(r, conn):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = score_band(score)
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band})"]
    out.extend(_repost_info(conn, r)[0])
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


# ----------------------------------------------------------------------- main

def cmd_stats(conn):
    for row in conn.execute(
        "SELECT status, verdict, COUNT(*) n FROM jobs GROUP BY status, verdict ORDER BY n DESC"
    ):
        print(f"{row['status']:>16} {str(row['verdict']):>10} {row['n']:>5}")
    print("  -- application status --")
    for row in conn.execute(
        "SELECT COALESCE(app_status,'(backlog)') s, COUNT(*) n FROM jobs GROUP BY app_status ORDER BY n DESC"
    ):
        print(f"{row['s']:>16} {row['n']:>16}")
    fsrc = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL"
    ).fetchone()["n"]
    if fsrc:
        print("  -- hard-fail overrides --")
        for row in conn.execute(
            "SELECT filter_source s, COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL "
            "GROUP BY filter_source ORDER BY n DESC"
        ):
            print(f"{row['s']:>16} {row['n']:>16}")


# _resolve_posting (url → row) and _chain_targets (decision propagation set) moved to chain.py.


def cmd_mark(conn, url, status):
    """Record the user's decision on a posting: `status` is 'applied', 'passed', or None
    (undo). `url` may be a unique substring of the job_url. The decision propagates to the
    canonical original of a repost chain, so the whole group is covered."""
    label = status or "undo"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    today = date.today().isoformat()
    stamp = today if status else None
    propagate_app_status(conn, _chain_targets(conn, m), status, stamp)
    conn.commit()
    verb = f"marked {status}" if status else "cleared status"
    print(f"[{label}] {verb}: {m['title']} — {m['company']}" + (f" ({today})" if status else ""))
    return True


def cmd_reject(conn, url, gate, pattern, note, undo):
    """Manually correct the model: mark a posting as a hard-fail it missed (distinct from the
    softer `passed`). `--pattern` additionally promotes the catch into a deterministic rule in
    filters.yaml so future postings with the same requirement are auto-failed. `--undo` clears
    the override (it does not remove any rule)."""
    label = "reject"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    if gate not in GATE_NAMES + ["other"]:
        print(f"[{label}] --gate must be one of {GATE_NAMES + ['other']}", file=sys.stderr)
        return False

    today = date.today().isoformat()
    targets = _chain_targets(conn, m)
    if undo:
        clear_reject(conn, targets)
        conn.commit()
        print(f"[{label}] cleared override: {m['title']} — {m['company']}")
        return True

    # The explicitly named row is always (re)stamped — you're overruling it, possibly re-attributing
    # a row a filters.yaml rule auto-failed; siblings are stamped too but never clobber a rule:<name>.
    propagate_reject(conn, targets, gate, today, force_url=m["job_url"], overwrite_manual=True)
    conn.commit()
    print(f"[{label}] rejected (gate: {gate}): {m['title']} — {m['company']} ({today})")

    if pattern:
        _add_filter_rule(conn, gate, pattern, note, m)
    return True


def _add_filter_rule(conn, gate, pattern, note, posting):
    """Promote a pattern into filters.yaml under the rule named for `gate`. Shows the matching
    sentence from this posting and how many existing postings the pattern would also catch
    (false-positive preview) before saving. De-dupes identical patterns."""
    # False-positive preview: how many existing postings would this pattern also match?
    rows = conn.execute("SELECT title, description FROM jobs").fetchall()
    hits = sum(1 for r in rows if _pattern_matches(pattern, f"{r['title'] or ''}\n{r['description'] or ''}"))
    # Show the sentence in THIS posting that the pattern matches, to sanity-check the phrase.
    desc = posting["description"] or ""
    snippet = next(
        (s.strip() for s in re.split(r"(?<=[.!?\n])\s+", desc) if _pattern_matches(pattern, s)),
        None,
    )
    print(f"[reject] pattern {pattern!r} → would match {hits} existing posting(s) in the DB")
    if snippet:
        print(f"[reject] matched here: …{snippet[:200]}…")

    rules = load_filters()
    # Match on `gate` (not `name`) so a hand-edited rule whose name differs from its gate
    # is still extended rather than duplicated.
    rule = next((r for r in rules if r.get("gate") == gate), None)
    if rule is None:
        rule = {"name": gate, "gate": gate, "note": note or "", "any": []}
        rules.append(rule)
    elif note and not rule.get("note"):
        rule["note"] = note
    if pattern in (rule.get("any") or []):
        print(f"[reject] pattern already in rule '{gate}' — nothing to add")
        return
    rule.setdefault("any", []).append(pattern)
    save_filters(rules)
    print(f"[reject] added pattern to rule '{gate}' in {FILTERS_PATH.name} "
          f"({len(rule['any'])} pattern(s) now)")


# ------------------------------------------------------- manual repost linking
#
# The dupe cores (_chain_members, _chain_decision, _decision_sig, _fmt_decision,
# _dupe_resolve, _dupe_commit, _dupe_unlink) live in chain.py and are imported above —
# `dupe` is the manual escape hatch for a relisting `_find_repost` missed (drifted
# title/location, or the same role cross-posted to Adzuna vs LinkedIn). The CLI wrapper
# below and the web UI (app.api_dupe) share those cores so the guard logic lives in one place.

def cmd_dupe(conn, url, of_url, undo, assume_yes):
    """CLI wrapper over the shared dupe cores. Manually link two existing postings as the same role
    (a duplicate `_find_repost` missed): earliest-`first_seen` becomes canonical, the other side is
    repointed under it, and any existing decision propagates across the unified chain. `--undo`
    splits a manual link apart. Previews the merge and confirms (skippable with `assume_yes`)."""
    label = "dupe"
    if undo:
        a = _resolve_posting(conn, url, label)
        if a is None:
            return False
        ok, msg, _ = _dupe_unlink(conn, a)
        print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
        return ok

    plan, err = _dupe_resolve(conn, url, of_url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    winner, loser, dec = plan["winner"], plan["loser"], plan["dec"]

    # Preview + confirm: a wrong merge buries a real job under another role's decision.
    print(f"[{label}] link as the SAME role:")
    print(f"    canonical (kept) : {winner['title']} — {winner['company']} ({winner['first_seen']})")
    print(f"    relisting (merge): {loser['title']} — {loser['company']} ({loser['first_seen']})")
    if len(plan["loser_members"]) > 1:
        print(f"    + {len(plan['loser_members']) - 1} relisting(s) already under the merged side")
    if dec:
        print(f"    decision propagated to the whole chain: {_fmt_decision(dec)}")
    if not assume_yes and not _confirm(f"[{label}] proceed?"):
        print(f"[{label}] aborted", file=sys.stderr)
        return False

    _dupe_commit(conn, plan)
    print(f"[{label}] linked: {loser['title']} — {loser['company']} → canonical {winner['job_url']}")
    return True


def _confirm(prompt):
    """Yes/no prompt; treats a closed stdin (non-interactive) or Ctrl-C as 'no' to fail safe."""
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()  # finish the prompt line so the caller's abort message isn't appended to it
        return False


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed", "reject", "dupe", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / `reject` / `dupe`")
    ap.add_argument("--of", help="`dupe`: job_url (or unique substring) of the other posting this duplicates")
    ap.add_argument("--yes", action="store_true", help="`dupe`: skip the confirmation prompt")
    ap.add_argument("--undo", action="store_true", help="clear the status/override/link instead of setting it")
    ap.add_argument("--gate", default="other",
                    help="hard gate a `reject` represents — one of: " + ", ".join(GATE_NAMES + ["other"]))
    ap.add_argument("--pattern", help="`reject`: promote this pattern into filters.yaml (re: prefix = regex)")
    ap.add_argument("--note", help="`reject`: optional note stored with a new filter rule")
    args = ap.parse_args()

    if args.command == "ui":
        # Lazy import so the core pipeline runs without Flask installed.
        try:
            import app
        except ImportError:
            print("[ui] Flask is required — run: pip install -r requirements.txt", file=sys.stderr)
            return
        app.serve()
        return

    cfg = load_config()
    conn = get_db(cfg)

    if args.command == "run":
        # The `status` column is a state machine and THIS ORDER IS LOAD-BEARING: each stage gates
        # on status and only the deterministic, zero-cost filters run before the *paid* eval, so an
        # obvious reject never reaches the LLM. The transitions:
        #   fetch_new_jobs / fetch_adzuna  insert rows as            'new'
        #   apply_salary_filter            'new' below floor      -> 'salary_filtered'
        #   apply_hard_filters             'new' hits a rule      -> 'rule_filtered'
        #   skip_decided_reposts           'new' relisting of a decided role -> 'repost_decided'
        #   evaluate_new_jobs              remaining 'new'        -> 'evaluated' | 'needs_manual' | 'error'
        # A new pre-eval filter must mirror this: set a non-'new' status so evaluate_new_jobs skips it.
        fetch_new_jobs(cfg, conn)
        fetch_adzuna(cfg, conn)
        apply_salary_filter(cfg, conn)
        apply_hard_filters(cfg, conn)
        skip_decided_reposts(conn)
        evaluate_new_jobs(cfg, conn)
        generate_report(cfg, conn, args.date)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)
    elif args.command in ("applied", "passed"):
        cmd_mark(conn, args.url, None if args.undo else args.command)
    elif args.command == "reject":
        cmd_reject(conn, args.url, args.gate, args.pattern, args.note, args.undo)
    elif args.command == "dupe":
        cmd_dupe(conn, args.url, args.of, args.undo, args.yes)


if __name__ == "__main__":
    main()
