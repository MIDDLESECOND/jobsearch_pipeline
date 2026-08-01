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
   "xfail": true,                               # optional: the CURRENT model is
                                                #   expected to get this case wrong —
                                                #   the red is a known drift alarm.
                                                #   An xfail miss doesn't fail the
                                                #   run; an unexpected pass prints
                                                #   an XPASS notice instead (these
                                                #   cases are variance-prone: re-run
                                                #   before flipping the case).
   "job_url": ...,                              # optional: unique url substring
                                                #   (the CLI's --url convention)
                                                #   pinning the exact row when the
                                                #   LIKE fragments match several
   "note": ...}                                 # why this expectation — free text

Run:  python tests/validation/backtest_v2.py   (any CWD — config and DB resolve via
core.BASE_DIR, the cases file next to this script; unlike the comparison scripts,
nothing here is CWD-relative).
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
_CASE_KEYS = {"company_like", "title_like", "expected", "extra", "xfail", "job_url", "note"}
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
        # isinstance first: set(c) on a stray string "validates" its characters and
        # c.get() then raises a raw AttributeError — the loudness must stay clean.
        if not isinstance(c, dict):
            sys.exit(f"case {i}: not a JSON object ({type(c).__name__}) — see docstring")
        problems = []
        bad = set(c) - _CASE_KEYS
        if bad:
            problems.append(f"unknown keys {sorted(bad)}")
        missing = {"company_like", "title_like", "expected"} - set(c)
        if missing:
            problems.append(f"missing {sorted(missing)}")
        if "expected" in c and not isinstance(c["expected"], (str, list)):
            problems.append("'expected' must be a string or list of strings")
        extra = c.get("extra")
        if extra is not None:
            if not isinstance(extra, dict):
                problems.append("'extra' must be an object")
            else:
                bad_extra = set(extra) - _EXTRA_KEYS
                if bad_extra:
                    problems.append(f"unknown extra keys {sorted(bad_extra)} "
                                    f"(accepted: {sorted(_EXTRA_KEYS)})")
        if problems:
            sys.exit(f"case {i} ({c.get('company_like', '?')}): " + "; ".join(problems)
                     + f" — accepted keys: {sorted(_CASE_KEYS)}")
    return cases


def _norm(s):
    """Lowercase and strip non-alphanumerics, so a flag check is robust to the model
    emitting 'management drift' / 'Management-Drift' / 'management_drift'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick(conn, company_like, title_like, url_like=None):
    # status='evaluated' AND verdict IS NOT NULL: the JUDGE-verdict predicate (same as
    # chain.skip_evaluated_reposts) — never a 'repost_evaluated' relisting (verdict NULL) and
    # never a rule_filtered row whose GATE_FAIL is a filters.yaml stamp, not a judgment; the
    # backtest must re-evaluate the exact text the judge actually scored.
    # ORDER BY pins WHICH matching row a broad LIKE fragment resolves to — without it
    # LIMIT 1 is scan-order (rowid), which VACUUM / _rebuild_for_stale_checks can
    # reorder, silently re-pinning a case to a different posting than its note describes.
    # (first_seen IS NULL last: SQLite sorts NULLs first ASC, and a NULL-stamped stray
    # row must never shadow the intended posting.) A case that needs an EXACT row —
    # e.g. the truncated variant among several relistings — pins it with "job_url".
    sql = ("SELECT * FROM jobs WHERE company LIKE ? AND title LIKE ? "
           "AND status='evaluated' AND verdict IS NOT NULL "
           "AND length(trim(description))>0 ")
    params = [f"%{company_like}%", f"%{title_like}%"]
    if url_like:
        sql += "AND job_url LIKE ? "
        params.append(f"%{url_like}%")
    sql += "ORDER BY first_seen IS NULL, first_seen, job_url LIMIT 2"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) > 1:
        print(f"  note  {company_like}/{title_like}: matches multiple rows — "
              f"using earliest first_seen ({rows[0]['job_url']}); "
              f"add \"job_url\" to the case to pin one")
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
    if provider == "deepseek":
        key = core._ensure_api_key("DEEPSEEK_API_KEY")
        if not key:
            sys.exit("DEEPSEEK_API_KEY not set")
        return lambda user_msg: evaluation._call_deepseek(
            key, model, system_prompt, user_msg)[0]
    # Mirrors evaluate_new_jobs' explicit unknown-provider arm: a future provider
    # must get its own branch here, never fall through to the wrong endpoint.
    sys.exit(f"unknown provider '{provider}' — backtest_v2 speaks anthropic|deepseek")


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

    passed = failed = skipped = xfailed = xpassed = 0
    for case in cases:
        company_like, title_like = case["company_like"], case["title_like"]
        expected = case["expected"]
        extra = case.get("extra")
        xfail = case.get("xfail", False)
        row = pick(conn, company_like, title_like, case.get("job_url"))
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

        if xfail:
            # pytest-style non-strict xfail: the known alarm staying red is the
            # EXPECTED outcome, so it doesn't fail the run; an unexpected pass is
            # surfaced loudly but — because these boundary cases are variance-prone
            # (one gave all three verdicts in a day) — never auto-suggests flipping
            # the case on a single green.
            if ok:
                xpassed += 1
                mark = "XPASS!"
            else:
                xfailed += 1
                mark = "XFAIL·"
        else:
            passed += ok
            failed += not ok
            mark = "PASS✓" if ok else "FAIL✗"
        print(f"  {mark}  {row['company']} — {row['title'][:42]}")
        if xfail and ok:
            print("          UNEXPECTED PASS on a known-alarm case — variance-prone; "
                  "re-run a few times before flipping or removing the case")
        print(f"          expected {' or '.join(accepted)}, got {verdict} (bucket {bucket}, "
              f"score {res.get('fit_score')})")
        print(f"          ai_applied_vs_research={bd.get('ai_applied_vs_research')}  "
              f"ai_artifact_depth={bd.get('ai_artifact_depth')}")
        for ln in extra_lines:
            print(f"          {ln}")
        print(f"          {res.get('one_line','')}\n")

    print("=" * 60)
    parts = [f"{passed} matched expectation", f"{failed} did not"]
    if xfailed:
        parts.append(f"{xfailed} known-alarm xfail (expected red)")
    if xpassed:
        parts.append(f"{xpassed} XPASS (re-run before flipping)")
    if skipped:
        parts.append(f"{skipped} skipped")
    print("backtest: " + ", ".join(parts))
    # Same silent-lie principle as load_cases: every skip means a pinned anchor
    # dropped out of jobs.db (stale LIKE fragment, rebuilt DB) — always actionable
    # on this single-user tool, never environmental, so it must not read as green.
    if skipped:
        sys.exit(f"{skipped} case(s) skipped — fix the LIKE fragment/job_url or the DB")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
