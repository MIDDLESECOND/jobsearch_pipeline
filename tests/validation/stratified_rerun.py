#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Stratified, repeated model comparison — the audit follow-up to the 2026-07-31
runs, whose 25-posting sample turned out to be 100% truncated Adzuna snippets
from one search (see CHANGELOG 2026-08-01).

Design:
  - 15 full-text postings (12 LinkedIn + 3 ATS, evaluated, >1500 chars) +
    10 truncated Adzuna snippets (<=520 chars), always including the four known
    boundary/flip cases.
  - Models: ds-flash (incumbent), luna default, luna reasoning_effort=high.
  - REPS=3 per (posting, model): every number gets an error bar, and the
    luna-vs-luna-high comparison is no longer a single-draw claim.

Writes results/stratified_results.json. Reuses compare_models' callers/prompt.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_models as cm  # noqa: E402  (loads keys + SYSTEM at import)
import evaluation  # noqa: E402
from _common import RESULTS_DIR, DB_PATH  # noqa: E402

REPS = 3
WORKERS = 6
MODELS = [  # (label, caller, model, extra)
    ("ds-flash",  cm.call_deepseek, "deepseek-v4-flash", None),
    ("luna",      cm.call_openai,   "gpt-5.6-luna", None),
    ("luna-high", cm.call_openai,   "gpt-5.6-luna", {"reasoning_effort": "high"}),
]
# The known boundary/flip cases are forced into the truncated stratum. They name
# real postings from the private jobs.db, so they live in a LOCAL file (gitignored):
# tests/validation/boundary_cases.local.json — a JSON list of
# [company_like, title_like] pairs. No file -> no forced picks; the truncated
# stratum fills from recent rows alone.
def _load_boundary():
    p = Path(__file__).with_name("boundary_cases.local.json")
    if p.exists():
        return [tuple(x) for x in json.loads(p.read_text(encoding="utf-8"))]
    return []


BOUNDARY = _load_boundary()


def pick_sample(conn):
    base = ("SELECT * FROM jobs WHERE status='evaluated' AND verdict IS NOT NULL "
            "AND length(trim(description)) {} ")
    full = conn.execute(
        base.format("> 1500") + "AND source='linkedin' "
        "ORDER BY first_seen DESC, job_url LIMIT 12").fetchall()
    full += conn.execute(
        base.format("> 1500") + "AND source IN ('greenhouse','lever','ashby') "
        "ORDER BY first_seen DESC, job_url LIMIT 3").fetchall()
    trunc, seen = [], set()
    for comp, tit in BOUNDARY:
        r = conn.execute(
            base.format("<= 520") + "AND company LIKE ? AND title LIKE ? LIMIT 1",
            (f"%{comp}%", f"%{tit}%")).fetchone()
        if r and r["job_url"] not in seen:
            trunc.append(r)
            seen.add(r["job_url"])
    for r in conn.execute(
            base.format("<= 520") + "AND source='adzuna' "
            "ORDER BY first_seen DESC, job_url LIMIT 20").fetchall():
        if len(trunc) >= 10:
            break
        if r["job_url"] not in seen:
            trunc.append(r)
            seen.add(r["job_url"])
    return [("full", r) for r in full] + [("truncated", r) for r in trunc]


def one_call(caller, model, extra, user_msg):
    t0 = time.monotonic()
    try:
        text, tin, tout = caller(model, user_msg, extra)
        parsed = evaluation.normalize_result(evaluation.parse_eval_json(text))
        return {"ok": True, "verdict": parsed.get("verdict"),
                "failed_gate": parsed.get("failed_gate"),
                "fit_score": parsed.get("fit_score"),
                "in_tok": tin, "out_tok": tout,
                "latency": round(time.monotonic() - t0, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160],
                "latency": round(time.monotonic() - t0, 1)}


def main():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sample = pick_sample(conn)
    n_full = sum(1 for s, _ in sample if s == "full")
    print(f"sample: {len(sample)} postings ({n_full} full-text, {len(sample) - n_full} truncated), "
          f"{len(MODELS)} models x {REPS} reps = {len(sample) * len(MODELS) * REPS} calls\n",
          flush=True)

    tasks = []  # (posting_idx, label, rep)
    for pi, (_, row) in enumerate(sample):
        for label, caller, model, extra in MODELS:
            for rep in range(REPS):
                tasks.append((pi, label, caller, model, extra, rep))

    results = [{"slice": s, "title": r["title"], "company": r["company"],
                "source": r["source"], "desc_len": len(r["description"] or ""),
                "models": {lab: [None] * REPS for lab, *_ in MODELS}}
               for s, r in sample]

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one_call, caller, model, extra,
                          evaluation.build_user_msg(sample[pi][1])): (pi, label, rep)
                for pi, label, caller, model, extra, rep in tasks}
        for fut in as_completed(futs):
            pi, label, rep = futs[fut]
            results[pi]["models"][label][rep] = fut.result()
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(tasks)} calls done", flush=True)

    with open(RESULTS_DIR / "stratified_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    summarize(results)


def majority(verds):
    """Majority verdict across reps; None if all distinct (fully unstable)."""
    from collections import Counter
    ok = [v for v in verds if v]
    if not ok:
        return None
    v, n = Counter(ok).most_common(1)[0]
    return v if n > 1 or len(ok) == 1 else None


def summarize(results):
    labs = [m[0] for m in MODELS]
    print("\n" + "=" * 70)
    for sl in ("full", "truncated"):
        rows = [r for r in results if r["slice"] == sl]
        print(f"\n--- {sl.upper()} slice ({len(rows)} postings x {REPS} reps) ---")
        for lab in labs:
            evs = [m for r in rows for m in r["models"][lab] if m and m["ok"]]
            from collections import Counter
            cnt = Counter(m["verdict"] for m in evs)
            nerr = sum(1 for r in rows for m in r["models"][lab] if m and not m["ok"])
            unstable = sum(
                1 for r in rows
                if len({m["verdict"] for m in r["models"][lab] if m and m["ok"]}) > 1)
            otok = sum(m["out_tok"] for m in evs) / max(len(evs), 1)
            print(f"  {lab:<9} PASS {cnt.get('PASS', 0):>2}  RO {cnt.get('RECRUITER_ONLY', 0):>2}  "
                  f"GF {cnt.get('GATE_FAIL', 0):>2}  ERR {nerr}  "
                  f"| unstable postings {unstable}/{len(rows)}  avg out_tok {otok:>5.0f}")
        # majority-verdict agreement vs the incumbent
        for lab in labs[1:]:
            both = agree = 0
            for r in rows:
                mf = majority([m["verdict"] for m in r["models"]["ds-flash"] if m and m["ok"]])
                ml = majority([m["verdict"] for m in r["models"][lab] if m and m["ok"]])
                if mf and ml:
                    both += 1
                    agree += (mf == ml)
            pct = 100 * agree / both if both else 0
            print(f"  majority-verdict agreement {lab} vs ds-flash: {agree}/{both} ({pct:.0f}%)")

    print("\n--- luna vs luna-high, per-posting rep detail where their majorities differ ---")
    any_diff = False
    for r in results:
        ml = majority([m["verdict"] for m in r["models"]["luna"] if m and m["ok"]])
        mh = majority([m["verdict"] for m in r["models"]["luna-high"] if m and m["ok"]])
        if ml != mh:
            any_diff = True
            lv = [m["verdict"] if m and m["ok"] else "ERR" for m in r["models"]["luna"]]
            hv = [m["verdict"] if m and m["ok"] else "ERR" for m in r["models"]["luna-high"]]
            print(f"  [{r['slice'][:5]:<5}] {r['title'][:44]:<44} luna={lv} high={hv}")
    if not any_diff:
        print("  none — same majority verdict on every posting")


if __name__ == "__main__":
    main()
