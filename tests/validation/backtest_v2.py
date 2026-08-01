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

Run:  python tests/validation/backtest_v2.py   (any CWD — config/DB/cases are all
__file__-anchored via core.BASE_DIR; unlike the comparison scripts, nothing here
is CWD-relative).
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


# The full vocabulary the runner understands. Validated on load because a typo'd
# key in the JSON would otherwise be silently ignored — the case would degrade to
# a verdict-only check while still printing PASS (the old hardcoded-Python CASES
# made that mistake a loud NameError; JSON needs the loudness re-added).
_CASE_KEYS = {"company_like", "title_like", "expected", "extra", "note"}
_EXTRA_KEYS = {"flag", "title_trajectory_max"}


def load_cases():
    # A regression guard with zero cases is a silent lie — refuse to "pass" on a
    # missing or empty file instead of exiting green having asserted nothing.
    if not CASES_PATH.exists():
        sys.exit(f"no cases file: {CASES_PATH}\n"
                 "(local-only, gitignored — it names real postings; create it as a JSON "
                 "list of case objects, see this script's docstring)")
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        sys.exit(f"cases file is not a non-empty JSON list: {CASES_PATH}")
    for i, c in enumerate(cases):
        bad = set(c) - _CASE_KEYS
        bad_extra = set(c.get("extra") or {}) - _EXTRA_KEYS
        missing = {"company_like", "title_like", "expected"} - set(c)
        if bad or bad_extra or missing:
            sys.exit(f"case {i} ({c.get('company_like', '?')}): "
                     f"unknown keys {sorted(bad | bad_extra)}, missing {sorted(missing)} "
                     f"— accepted: {sorted(_CASE_KEYS)}, extra: {sorted(_EXTRA_KEYS)}")
    return cases


def _norm(s):
    """Lowercase and strip non-alphanumerics, so a flag check is robust to the model
    emitting 'management drift' / 'Management-Drift' / 'management_drift'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick(conn, company_like, title_like):
    # status='evaluated' AND verdict IS NOT NULL: the JUDGE-verdict predicate (same as
    # chain.skip_evaluated_reposts) — never a 'repost_evaluated' relisting (verdict NULL) and
    # never a rule_filtered row whose GATE_FAIL is a filters.yaml stamp, not a judgment; the
    # backtest must re-evaluate the exact text the judge actually scored.
    # ORDER BY pins WHICH matching row a broad LIKE fragment resolves to — without it
    # LIMIT 1 is scan-order (rowid), which VACUUM / _rebuild_for_stale_checks can
    # reorder, silently re-pinning a case to a different posting than its note describes.
    rows = conn.execute(
        "SELECT * FROM jobs WHERE company LIKE ? AND title LIKE ? "
        "AND status='evaluated' AND verdict IS NOT NULL "
        "AND length(trim(description))>0 ORDER BY first_seen, job_url LIMIT 2",
        (f"%{company_like}%", f"%{title_like}%"),
    ).fetchall()
    if len(rows) > 1:
        print(f"  note  {company_like}/{title_like}: matches multiple rows — "
              f"using earliest first_seen ({rows[0]['job_url']})")
    return rows[0] if rows else None


def make_caller(cfg, system_prompt):
    """Resolve provider/key/client ONCE, before the case loop — a missing key must be
    one clean exit up front, not one 'Bearer None' 401 per case miscounted as an eval
    regression (the production pipeline gets this via its upfront check + EvalAuthError;
    this mirrors it). Returns user_msg -> raw response text."""
    provider = cfg["settings"].get("provider", "anthropic")
    model = cfg["settings"]["model"]
    if provider == "anthropic":
        if not core._ensure_api_key("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY not set")
        import anthropic
        client = anthropic.Anthropic()
        return lambda user_msg: evaluation._call_anthropic(
            client, model, system_prompt, user_msg)[0]
    key = core._ensure_api_key("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("DEEPSEEK_API_KEY not set")
    return lambda user_msg: evaluation._call_deepseek(
        key, model, system_prompt, user_msg)[0]


def evaluate(call, row):
    text = call(evaluation.build_user_msg(row))
    return evaluation.normalize_result(evaluation.parse_eval_json(text))


def main():
    cases = load_cases()
    cfg = core.load_config()
    conn = core.get_db(cfg)
    system_prompt = evaluation.build_system_prompt()
    call = make_caller(cfg, system_prompt)
    print(f"provider={cfg['settings'].get('provider')} model={cfg['settings']['model']}\n")

    passed = failed = skipped = 0
    for case in cases:
        company_like, title_like = case["company_like"], case["title_like"]
        expected = case["expected"]
        extra = case.get("extra")
        row = pick(conn, company_like, title_like)
        if row is None:
            print(f"  SKIP  {company_like}/{title_like}: not found in jobs.db")
            skipped += 1
            continue
        try:
            res = evaluate(call, row)
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
    print(f"backtest: {passed} matched expectation, {failed} did not, {skipped} skipped")
    # Same silent-lie principle as load_cases: an all-skip run (fresh/rebuilt DB,
    # stale LIKE fragments) asserted nothing and must not read as green.
    if passed + failed == 0:
        sys.exit("backtest executed ZERO cases — every case skipped; not a pass")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
