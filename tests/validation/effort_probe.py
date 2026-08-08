#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Does lowering DeepSeek V4 Flash's reasoning effort change judgment, or only cost?

V4-Flash defaults to thinking-on at effort "high", and until 2026-08-07 production sent
no `reasoning_effort` at all — so every eval bought maximum-depth reasoning, which the
0731 build then made ~2.5x more verbose. That is most of the eval bill. This probe sends
the SAME postings through three explicitly-named effort tiers and compares verdicts
against the measured noise floor, exactly like guide_size_probe.py (same SEED, same
stratification, same majority-vote reading — results are cross-comparable):

  high  — "reasoning_effort": "high"   (the provider default; production before 08-07)
  low   — "reasoning_effort": "low"    (production since 08-07, chosen from this probe)
  none  — "thinking": {"type": "disabled"}  (no reasoning phase at all)

Every tier is spelled out rather than described relative to production, because
"whatever production sends" is a moving target — see the CONDITIONS comment.

External evidence says thinking is neutral-to-harmful for rubric-judge tasks with
precision constraints (VERT arXiv:2604.03376; constraint-level splits arXiv:2606.09662),
but none of it measured V4 Flash — this probe is the local arbiter.

Repeats and stratification mirror guide_size_probe.py, and for the same reasons: verdict
noise within one condition is ~17-18% per draw, and the DB is dominated by truncated
snippets. Also tracked per condition: empty answers (a thinking-phase behavior of the
0731 build — "none" may eliminate them) and JSON parse failures.

Run:  python tests/validation/effort_probe.py [--reps N] [--per-stratum N]
Needs DEEPSEEK_API_KEY. Writes tests/validation/results/effort_probe_<stamp>.json.
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

RESULTS_DIR = Path(__file__).with_name("results")
SEED = 20260807            # same seed as guide_size_probe -> same 18 postings
SHORT_DESC = 1500
TIMEOUT = 300

# Overrides applied to evaluation.deepseek_request_body — every tier is spelled
# EXPLICITLY. An earlier version left "high" as {} to mean "whatever production
# sends"; production then moved to low and that column would have measured low
# while labelling it high, i.e. reported the two tiers identical.
CONDITIONS = {
    "high": {"reasoning_effort": "high"},
    "low": {"reasoning_effort": "low"},
    # None deletes the key, so this sends no effort at all beside thinking-disabled.
    "none": {"thinking": {"type": "disabled"}, "reasoning_effort": None},
}


def sample_postings(conn, per_stratum):
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
        for r in rng.sample(bucket, min(per_stratum, len(bucket))):
            picked.append((key, r))
    return picked


def call(api_key, model, system_prompt, user_msg, extra):
    t0 = time.time()
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
        })
    except Exception as e:
        out.update({"parsed": False, "parse_error": str(e)[:120]})
    return out


def majority(values):
    vals = [v for v in values if v is not None]
    return Counter(vals).most_common(1)[0][0] if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--per-stratum", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    cfg = core.load_config()
    model = cfg["settings"]["model"]
    api_key = core._ensure_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set")

    system_prompt = evaluation.build_system_prompt()
    conn = core.connect_db(cfg)
    conn.row_factory = sqlite3.Row
    picked = sample_postings(conn, args.per_stratum)

    total = len(picked) * len(CONDITIONS) * args.reps
    print(f"model={model}  reps={args.reps}  postings={len(picked)}  calls={total}")

    jobs = []
    for (verdict_class, length_class), row in picked:
        user_msg = evaluation.build_user_msg(row)
        for cond in CONDITIONS:
            for rep in range(args.reps):
                jobs.append({
                    "job_url": row["job_url"], "title": row["title"],
                    "company": row["company"], "stored_verdict": verdict_class,
                    "length_class": length_class, "condition": cond, "rep": rep,
                    "user_msg": user_msg,
                })

    done = [0]

    def run(job):
        res = call(api_key, model, system_prompt, job["user_msg"], CONDITIONS[job["condition"]])
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"  ... {done[0]}/{len(jobs)}")
        return {**{k: v for k, v in job.items() if k != "user_msg"}, **res}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        records = list(ex.map(run, jobs))
    print(f"completed in {time.time() - t0:.0f}s\n")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"effort_probe_{stamp}.json"
    out_path.write_text(json.dumps({
        "model": model, "reps": args.reps, "seed": SEED,
        "conditions": {k: v for k, v in CONDITIONS.items()},
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- summary -------------------------------------------------------------
    def mean(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return statistics.mean(xs) if xs else float("nan")

    conds = list(CONDITIONS)
    by = defaultdict(list)
    for r in records:
        by[(r["job_url"], r["condition"])].append(r)
    urls = sorted({r["job_url"] for r in records})

    print("=" * 78)
    print("PER-CONDITION TOTALS")
    print(f"{'cond':>6}{'complet':>9}{'reason':>8}{'empty':>8}{'parse✗':>8}{'sec':>6}"
          f"{'  $/eval (out only)':>20}")
    for c in conds:
        rs = [r for r in records if r["condition"] == c]
        n = len(rs)
        ct = mean([r.get("completion_tok") for r in rs])
        print(f"{c:>6}{ct:>9,.0f}{mean([r.get('reasoning_tok') for r in rs]):>8,.0f}"
              f"{sum(1 for r in rs if r.get('empty_answer')):>5}/{n:<3}"
              f"{sum(1 for r in rs if r.get('parsed') is False):>5}/{n:<3}"
              f"{mean([r.get('elapsed') for r in rs]):>6.0f}"
              f"{ct * 0.28 / 1e6:>20.6f}")

    print("\nWITHIN-CONDITION NOISE FLOOR (all-3-reps unanimous / rep0-vs-rep1 agree)")
    for c in conds:
        unan = tot = pair = ptot = 0
        for u in urls:
            rs = sorted(by[(u, c)], key=lambda r: r["rep"])
            vs = [r.get("verdict") for r in rs if r.get("verdict")]
            if vs:
                tot += 1
                unan += len(set(vs)) == 1
            if len(rs) >= 2 and rs[0].get("verdict") and rs[1].get("verdict"):
                ptot += 1
                pair += rs[0]["verdict"] == rs[1]["verdict"]
        print(f"  {c:>5}: unanimous {unan}/{tot} ({100*unan/tot:.0f}%)   "
              f"single-draw {pair}/{ptot} ({100*pair/ptot:.0f}%)")

    print("\nFIT SCORE (gates-passed draws)")
    for c in conds:
        scores = [r["fit_score"] for r in records
                  if r["condition"] == c and isinstance(r.get("fit_score"), int)]
        if scores:
            print(f"  {c:>5}: n={len(scores):>3}  mean={statistics.mean(scores):.2f}"
                  f"  median={statistics.median(scores)}  sd={statistics.pstdev(scores):.2f}")

    print("\nPER-POSTING MAJORITY VERDICT vs the 'high' condition (*** = differs)")
    print(f"{'stored':>14} {'len':>5} {'high':>15} {'low':>15} {'none':>15}  title")
    diff_counts = {c: 0 for c in conds if c != "high"}
    seen = set()
    ordered = []
    for r in records:
        if r["job_url"] not in seen:
            seen.add(r["job_url"])
            ordered.append(r)
    for meta in ordered:
        u = meta["job_url"]
        got = {c: majority([x.get("verdict") for x in by[(u, c)]]) for c in conds}
        marks = []
        for c in ("low", "none"):
            same = got[c] == got["high"]
            diff_counts[c] += not same
            marks.append("   " if same else "***")
        print(f"{str(meta['stored_verdict']):>14} {meta['length_class']:>5} "
              f"{str(got['high']):>15} {str(got['low']):>15} {str(got['none']):>15} "
              f"{marks[0]}{marks[1]} {(meta['title'] or '')[:32]}")
    n = len(ordered)
    for c in ("low", "none"):
        print(f"\n{c} vs high majority agreement: {n - diff_counts[c]}/{n} "
              f"({100*(n - diff_counts[c])/n:.0f}%)")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
