#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Does a shorter evaluation guide change judgment, or just cost?

Sends the SAME postings through the SAME model twice — once with the production
`evaluation_guide.md`, once with a condensed variant that keeps every rule and drops
only narrative, origin stories, and worked examples. Records verdicts and per-call
token usage for both.

Why it repeats: at temp=0 on this build the same posting has been observed scoring
11 / 13 / 15 across runs, so a single call per condition cannot separate a guide
effect from ordinary variance. Each posting runs REPS times per condition and the
comparison is made on the majority verdict plus mean tokens, never on one draw.

Why it stratifies: the DB is ~63% truncated Adzuna snippets, and truncated postings
behave differently from full-text ones (they sit nearer the judge's decision
boundary). An unstratified sample would mostly measure the truncated population.

The condensed guide lives next to this script as `guide_lean.local.md` (gitignored —
it carries the private profile/judgment text, same convention as the .local.json
case files). Build it by cutting prose from the real guide, never by dropping a rule:
this probe is only meaningful if both conditions carry an identical rule set.

Run:  python tests/validation/guide_size_probe.py [--reps N] [--per-stratum N]
Needs DEEPSEEK_API_KEY. Cost is roughly (postings x conditions x reps) x $0.0012.
Writes tests/validation/results/guide_size_probe_<stamp>.json.
"""
import argparse
import json
import random
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

import core
import evaluation

LEAN_GUIDE = Path(__file__).with_name("guide_lean.local.md")
RESULTS_DIR = Path(__file__).with_name("results")
SEED = 20260807
SHORT_DESC = 1500          # chars; below this a posting is a truncated snippet
MAX_TOKENS = 16000
TIMEOUT = 300


def build_prompts():
    """Return {condition: system_prompt}. Swaps evaluation.GUIDE_PATH rather than
    reimplementing the template, so both conditions are assembled exactly the way
    production assembles the real one."""
    original = evaluation.GUIDE_PATH
    try:
        full = evaluation.build_system_prompt()
        evaluation.GUIDE_PATH = LEAN_GUIDE
        lean = evaluation.build_system_prompt()
    finally:
        evaluation.GUIDE_PATH = original
    return {"full": full, "lean": lean}


def sample_postings(conn, per_stratum):
    """Stratified by (stored verdict class) x (description length class)."""
    rows = conn.execute(
        """SELECT job_url, title, company, location, search_name, tier, date_posted,
                  salary_min, salary_max, description, source, verdict, fit_score
           FROM jobs
           WHERE status='evaluated' AND description IS NOT NULL
             AND length(trim(description)) > 200"""
    ).fetchall()

    strata = defaultdict(list)
    for r in rows:
        length_class = "short" if len(r["description"]) < SHORT_DESC else "long"
        strata[(r["verdict"], length_class)].append(r)

    rng = random.Random(SEED)
    picked = []
    for key in sorted(strata, key=lambda k: (str(k[0]), k[1])):
        bucket = strata[key]
        take = min(per_stratum, len(bucket))
        for r in rng.sample(bucket, take):
            picked.append((key, r))
    return picked


def call(api_key, model, system_prompt, user_msg):
    """One API call. Returns a dict of facts, never raises for model-side failures —
    an empty answer IS the measurement here, not an error to hide."""
    t0 = time.time()
    try:
        r = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model, "max_tokens": MAX_TOKENS, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": user_msg}],
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        return {"transport_error": f"{type(e).__name__}: {e}", "elapsed": time.time() - t0}

    d = r.json()
    ch = d["choices"][0]
    u = d.get("usage", {})
    details = u.get("completion_tokens_details") or {}
    content = ch["message"].get("content") or ""

    out = {
        "elapsed": round(time.time() - t0, 1),
        "finish_reason": ch.get("finish_reason"),
        "prompt_tok": u.get("prompt_tokens"),
        "cache_hit_tok": u.get("prompt_cache_hit_tokens"),
        "completion_tok": u.get("completion_tokens"),
        "reasoning_tok": details.get("reasoning_tokens"),
        "content_chars": len(content),
        "empty_answer": len(content.strip()) == 0,
    }
    try:
        result = evaluation.parse_eval_json(content)
        raw_verdict = result.get("verdict")
        evaluation.normalize_result(result)
        bd = result.get("score_breakdown")
        out.update({
            "parsed": True,
            "raw_verdict": raw_verdict,
            "verdict": result.get("verdict"),
            "capped": raw_verdict != result.get("verdict"),
            "fit_score": result.get("fit_score"),
            "failed_gate": result.get("failed_gate"),
            "bucket": result.get("bucket"),
            "leadership": result.get("formal_leadership_required"),
            "depth": (bd or {}).get("ai_artifact_depth") if isinstance(bd, dict) else None,
            "flags": result.get("flags"),
        })
    except Exception as e:
        out.update({"parsed": False, "parse_error": str(e)[:120]})
    return out


def majority(values):
    """Most common non-None value, or None if there isn't one."""
    vals = [v for v in values if v is not None]
    return Counter(vals).most_common(1)[0][0] if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--per-stratum", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    if not LEAN_GUIDE.exists():
        sys.exit(f"missing condensed guide: {LEAN_GUIDE}")

    cfg = core.load_config()
    model = cfg["settings"]["model"]
    api_key = core._ensure_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set")

    prompts = build_prompts()
    conn = core.connect_db(cfg)
    conn.row_factory = sqlite3.Row
    picked = sample_postings(conn, args.per_stratum)

    print(f"model={model}  reps={args.reps}  postings={len(picked)}")
    for cond, p in prompts.items():
        print(f"  {cond:>5} system prompt: {len(p):>7,} chars")
    print(f"  total calls: {len(picked) * len(prompts) * args.reps}\n")

    jobs = []
    for (verdict_class, length_class), row in picked:
        user_msg = evaluation.build_user_msg(row)
        for cond in prompts:
            for rep in range(args.reps):
                jobs.append({
                    "job_url": row["job_url"], "title": row["title"],
                    "company": row["company"], "stored_verdict": verdict_class,
                    "length_class": length_class, "desc_chars": len(row["description"]),
                    "condition": cond, "rep": rep, "user_msg": user_msg,
                })

    done = [0]

    def run(job):
        res = call(api_key, model, prompts[job["condition"]], job["user_msg"])
        done[0] += 1
        if done[0] % 10 == 0:
            print(f"  ... {done[0]}/{len(jobs)}")
        return {**{k: v for k, v in job.items() if k != "user_msg"}, **res}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        records = list(ex.map(run, jobs))
    print(f"\ncompleted in {time.time() - t0:.0f}s\n")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"guide_size_probe_{stamp}.json"
    out_path.write_text(json.dumps({
        "model": model, "reps": args.reps, "seed": SEED,
        "prompt_chars": {c: len(p) for c, p in prompts.items()},
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- summary -------------------------------------------------------------
    def mean(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return statistics.mean(xs) if xs else float("nan")

    print("=" * 74)
    print("PER-CONDITION TOTALS")
    print(f"{'cond':>6}{'prompt':>9}{'cache%':>8}{'complet':>9}{'reason':>8}"
          f"{'empty':>7}{'parse✗':>8}{'sec':>6}")
    for cond in prompts:
        rs = [r for r in records if r["condition"] == cond]
        n = len(rs)
        cache = mean([100 * (r.get("cache_hit_tok") or 0) / r["prompt_tok"]
                      for r in rs if r.get("prompt_tok")])
        print(f"{cond:>6}{mean([r.get('prompt_tok') for r in rs]):>9,.0f}"
              f"{cache:>8.1f}{mean([r.get('completion_tok') for r in rs]):>9,.0f}"
              f"{mean([r.get('reasoning_tok') for r in rs]):>8,.0f}"
              f"{sum(1 for r in rs if r.get('empty_answer')):>4}/{n:<2}"
              f"{sum(1 for r in rs if r.get('parsed') is False):>5}/{n:<2}"
              f"{mean([r.get('elapsed') for r in rs]):>6.0f}")

    print("\nFIT SCORE (gates-passed draws only)")
    for cond in prompts:
        scores = [r["fit_score"] for r in records
                  if r["condition"] == cond and isinstance(r.get("fit_score"), int)]
        if scores:
            print(f"{cond:>6}  n={len(scores):>3}  mean={statistics.mean(scores):.2f}"
                  f"  median={statistics.median(scores)}"
                  f"  sd={statistics.pstdev(scores):.2f}")

    print("\nPER-POSTING MAJORITY VERDICT  (disagreements marked ***)")
    print(f"{'stored':>14} {'len':>5} {'full':>15} {'lean':>15}  title")
    agree = disagree = 0
    for (vc, lc), row in picked:
        got = {}
        for cond in prompts:
            rs = [r for r in records
                  if r["job_url"] == row["job_url"] and r["condition"] == cond]
            got[cond] = majority([r.get("verdict") for r in rs])
        same = got["full"] == got["lean"]
        agree += same
        disagree += not same
        mark = "   " if same else "***"
        print(f"{str(vc):>14} {lc:>5} {str(got['full']):>15} {str(got['lean']):>15} "
              f"{mark} {(row['title'] or '')[:38]}")

    total = agree + disagree
    print(f"\nmajority-verdict agreement between conditions: {agree}/{total}"
          f"  ({100 * agree / total:.0f}%)" if total else "")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
