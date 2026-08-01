#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Backtest the v2 (split-AI / RECRUITER_ONLY / bucket) prompt against known cases.

Pulls real postings already stored in jobs.db, re-evaluates them through the SAME
provider/model the pipeline uses, applies evaluation.normalize_result (so the hard
artifact-depth cap is exercised), and checks each verdict against an expectation.

The cases live in backtest_cases.local.json next to this script (gitignored — they
name real postings from the private jobs.db, same convention as
boundary_cases.local.json). Each case is an object:

  {"company_like": ..., "title_like": ...,      # SQL LIKE fragments to pick the row
   "expected": "PASS" | ["GATE_FAIL", ...],     # one verdict, or any-of — for cases
                                                #   pinned to "one of these guards
                                                #   catches it", not a single guard
   "extra": {"flag": <substring>,               # optional additional assertions
             "title_trajectory_max": <int>},
   "note": ...}                                 # why this expectation — free text

Run:  python tests/validation/backtest_v2.py   (from the repo root, next to jobs.db)
Needs the same API key the configured provider needs (DEEPSEEK_API_KEY by default).
"""
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Lives in tests/validation/ but imports the pipeline modules at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core
import evaluation

CASES_PATH = Path(__file__).with_name("backtest_cases.local.json")


def load_cases():
    # A regression guard with zero cases is a silent lie — refuse to "pass" on a
    # missing file instead of exiting green having asserted nothing.
    if not CASES_PATH.exists():
        sys.exit(f"no cases file: {CASES_PATH}\n"
                 "(local-only, gitignored — it names real postings; create it as a JSON "
                 "list of case objects, see this script's docstring)")
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _norm(s):
    """Lowercase and strip non-alphanumerics, so a flag check is robust to the model
    emitting 'management drift' / 'Management-Drift' / 'management_drift'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick(conn, company_like, title_like):
    # status='evaluated' AND verdict IS NOT NULL: the JUDGE-verdict predicate (same as
    # chain.skip_evaluated_reposts) — never a 'repost_evaluated' relisting (verdict NULL) and
    # never a rule_filtered row whose GATE_FAIL is a filters.yaml stamp, not a judgment; the
    # backtest must re-evaluate the exact text the judge actually scored.
    return conn.execute(
        "SELECT * FROM jobs WHERE company LIKE ? AND title LIKE ? "
        "AND status='evaluated' AND verdict IS NOT NULL "
        "AND length(trim(description))>0 LIMIT 1",
        (f"%{company_like}%", f"%{title_like}%"),
    ).fetchone()


def evaluate(cfg, system_prompt, row):
    provider = cfg["settings"].get("provider", "anthropic")
    model = cfg["settings"]["model"]
    user_msg = (
        f"TITLE: {row['title']}\nCOMPANY: {row['company']}\nLOCATION: {row['location']}\n"
        f"SOURCE SEARCH: {row['search_name']} (tier: {row['tier']})\n"
        f"POSTED SALARY: {row['salary_min']}–{row['salary_max']}\n\n"
        f"JOB DESCRIPTION:\n{row['description']}"
    )
    if provider == "anthropic":
        import anthropic
        core._ensure_api_key("ANTHROPIC_API_KEY")
        client = anthropic.Anthropic()
        text = evaluation._call_anthropic(client, model, system_prompt, user_msg)[0]
    else:
        key = core._ensure_api_key("DEEPSEEK_API_KEY")
        text = evaluation._call_deepseek(key, model, system_prompt, user_msg)[0]
    return evaluation.normalize_result(evaluation.parse_eval_json(text))


def main():
    cases = load_cases()
    cfg = core.load_config()
    conn = core.get_db(cfg)
    system_prompt = evaluation.build_system_prompt()
    print(f"provider={cfg['settings'].get('provider')} model={cfg['settings']['model']}\n")

    passed = failed = 0
    for case in cases:
        company_like, title_like = case["company_like"], case["title_like"]
        expected = case["expected"]
        extra = case.get("extra")
        row = pick(conn, company_like, title_like)
        if row is None:
            print(f"  SKIP  {company_like}/{title_like}: not found in jobs.db")
            continue
        try:
            res = evaluate(cfg, system_prompt, row)
        except Exception as e:
            print(f"  ERROR {company_like}: {type(e).__name__}: {e}")
            failed += 1
            continue
        verdict = res.get("verdict")
        bucket = res.get("bucket")
        bd = res.get("score_breakdown") or {}
        flags = res.get("flags") or []
        # `expected` may be a list of acceptable verdicts — for cases whose pinned
        # invariant is "any of these guards catches it" (the years-floor gate OR the
        # leadership cap's RECRUITER_ONLY backstop both mean "never a cold PASS"),
        # so the regression gate doesn't flake on which guard the noisy judge hits first.
        accepted = (expected,) if isinstance(expected, str) else tuple(expected)
        ok = verdict in accepted

        extra_lines = []
        if extra:
            if "flag" in extra:
                hit = any(_norm(extra["flag"]) in _norm(f) for f in flags)
                ok = ok and hit
                extra_lines.append(
                    f"flag '{extra['flag']}' {'present' if hit else 'MISSING'} "
                    f"(flags={flags})")
            if "title_trajectory_max" in extra:
                tt = bd.get("title_trajectory")
                hit = tt is not None and tt <= extra["title_trajectory_max"]
                ok = ok and hit
                extra_lines.append(
                    f"title_trajectory={tt} (need <= {extra['title_trajectory_max']}) "
                    f"{'OK' if hit else 'FAIL'}")

        passed += ok
        failed += not ok
        mark = "PASS✓" if ok else "FAIL✗"
        print(f"  {mark}  {row['company']} — {row['title'][:42]}")
        print(f"          expected {' or '.join(accepted)}, got {verdict} (bucket {bucket}, "
              f"score {res.get('fit_score')})")
        print(f"          ai_applied_vs_research={bd.get('ai_applied_vs_research')}  "
              f"ai_artifact_depth={bd.get('ai_artifact_depth')}")
        for ln in extra_lines:
            print(f"          {ln}")
        print(f"          {res.get('one_line','')}\n")

    print("=" * 60)
    print(f"backtest: {passed} matched expectation, {failed} did not")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
