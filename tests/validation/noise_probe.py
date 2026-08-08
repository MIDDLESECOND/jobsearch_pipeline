#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Measure the judge's REAL verdict-flip rate on the production population.

The stratified probes (guide_size_probe, effort_probe) measured a ~17-33% per-draw
verdict instability — but their sampling deliberately over-weights boundary-ish rows
(equal postings per verdict x length stratum, vs a real population that is mostly
easy GATE_FAILs). This probe answers the operational question those can't: **of the
postings production actually evaluates, what fraction would change verdict if the
run were repeated?** That number decides whether a boundary-band majority-vote
mechanism is worth building, and sizes what it would catch.

Design:
- UNIFORM random sample over evaluated rows (no stratification) — the estimate is
  of the population, so the sample must look like the population.
- k draws per posting (default 3) under ONE condition (default: production's
  request shape; --effort low/high/none to measure a candidate tier before
  switching). Flip rate = fraction of postings whose draws are not unanimous.
- Also reported: WHERE the instability lives (by stored verdict class and by
  fit-score band), which is exactly the targeting data a majority-vote trigger
  needs, and a Wilson 95% interval on the flip rate so a small sample can't
  overclaim precision.

Run:  python tests/validation/noise_probe.py [--n 60] [--reps 3] [--effort ...]
Cost: n x reps x ~$0.0012 (high) — 60x3 ≈ $0.22.
Writes tests/validation/results/noise_probe_<stamp>.json.
"""
import argparse
import json
import math
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

RESULTS_DIR = Path(__file__).with_name("results")
TIMEOUT = 300
# Overrides applied to evaluation.deepseek_request_body. "prod" is empty because the
# builder IS production's shape — nothing here can drift when production moves.
EFFORT_BODY = {
    "prod": {},
    "high": {"reasoning_effort": "high"},
    "low": {"reasoning_effort": "low"},
    "none": {"thinking": {"type": "disabled"}, "reasoning_effort": None},
}


def call(api_key, model, system_prompt, user_msg, extra):
    try:
        r = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=evaluation.deepseek_request_body(
                model, system_prompt, user_msg, **extra),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        return {"transport_error": f"{type(e).__name__}: {e}"}
    d = r.json()
    u = d.get("usage", {})
    content = d["choices"][0]["message"].get("content") or ""
    out = {"completion_tok": u.get("completion_tokens"),
           "empty_answer": len(content.strip()) == 0}
    try:
        res = evaluation.normalize_result(evaluation.parse_eval_json(content))
        out.update({"parsed": True, "verdict": res.get("verdict"),
                    "fit_score": res.get("fit_score"), "bucket": res.get("bucket")})
    except Exception as e:
        out.update({"parsed": False, "parse_error": str(e)[:120]})
    return out


def wilson(k, n, z=1.96):
    """95% interval for a proportion — honest about small n."""
    if not n:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--effort", choices=sorted(EFFORT_BODY), default="prod")
    ap.add_argument("--seed", type=int, default=20260807)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    cfg = core.load_config()
    model = cfg["settings"]["model"]
    api_key = core._ensure_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set")
    extra = EFFORT_BODY[args.effort]
    system_prompt = evaluation.build_system_prompt()

    conn = core.connect_db(cfg)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT job_url, title, company, location, search_name, tier, date_posted,
                  salary_min, salary_max, description, source, verdict, fit_score
           FROM jobs WHERE status='evaluated' AND description IS NOT NULL
             AND length(trim(description)) > 200"""
    ).fetchall()
    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))
    print(f"model={model}  effort={args.effort}  n={len(sample)}  reps={args.reps}  "
          f"calls={len(sample) * args.reps}")
    mix = Counter(r["verdict"] for r in sample)
    print(f"sample verdict mix (should look like the population): {dict(mix)}\n")

    jobs = [{"row": r, "rep": i} for r in sample for i in range(args.reps)]
    done = [0]

    def run(job):
        res = call(api_key, model, system_prompt,
                   evaluation.build_user_msg(job["row"]), extra)
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"  ... {done[0]}/{len(jobs)}")
        r = job["row"]
        return {"job_url": r["job_url"], "stored_verdict": r["verdict"],
                "stored_fit": r["fit_score"], "rep": job["rep"], **res}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        records = list(ex.map(run, jobs))
    print(f"completed in {time.time() - t0:.0f}s\n")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"noise_probe_{stamp}.json"
    out_path.write_text(json.dumps({
        "model": model, "effort": args.effort, "n": len(sample),
        "reps": args.reps, "seed": args.seed, "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    by = defaultdict(list)
    for r in records:
        by[r["job_url"]].append(r)

    flip = unan = unmeasured = 0
    flips_by_class = defaultdict(lambda: [0, 0])
    flips_by_band = defaultdict(lambda: [0, 0])
    for u, rs in by.items():
        vs = [r.get("verdict") for r in rs if r.get("verdict")]
        if len(vs) < 2:
            unmeasured += 1
            continue
        flipped = len(set(vs)) > 1
        flip += flipped
        unan += not flipped
        sv = rs[0]["stored_verdict"]
        flips_by_class[sv][0] += flipped
        flips_by_class[sv][1] += 1
        sf = rs[0]["stored_fit"]
        band = ("no-fit" if sf is None else
                "12-15" if 12 <= sf <= 15 else "<12" if sf < 12 else ">15")
        flips_by_band[band][0] += flipped
        flips_by_band[band][1] += 1

    n_meas = flip + unan
    lo, hi = wilson(flip, n_meas)
    print("=" * 66)
    print(f"POPULATION FLIP RATE: {flip}/{n_meas} = {100*flip/n_meas:.0f}%  "
          f"(Wilson 95% CI {100*lo:.0f}%-{100*hi:.0f}%)")
    if unmeasured:
        print(f"  ({unmeasured} postings had <2 parseable draws — excluded)")
    print("\nby stored verdict class:")
    for k in sorted(flips_by_class):
        f, n = flips_by_class[k]
        print(f"  {str(k):>15}: {f}/{n} flipped")
    print("\nby stored fit band (majority-vote trigger targeting):")
    for k in ("no-fit", "<12", "12-15", ">15"):
        if k in flips_by_band:
            f, n = flips_by_band[k]
            print(f"  {k:>7}: {f}/{n} flipped")
    empties = sum(1 for r in records if r.get("empty_answer"))
    tok = [r["completion_tok"] for r in records if r.get("completion_tok")]
    print(f"\nempty answers: {empties}/{len(records)}   "
          f"completion tokens: median {statistics.median(tok):,.0f}  "
          f"sd {statistics.pstdev(tok):,.0f}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
