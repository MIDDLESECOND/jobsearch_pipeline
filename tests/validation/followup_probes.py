#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Round-2-audit follow-ups (2026-08-01), two probes in one run:

A. luna reasoning_effort=xhigh on the SAME 10 truncated postings the stratified
   rerun used (3 reps) — completes the effort dial; "effort-invariant" is only
   claimable if xhigh also leaves the fail-open behavior in place.
B. 10 fresh RANDOM truncated postings (deterministically pseudo-random, the 4
   known boundary cases and the stratified slice excluded) x 3 models x 3 reps —
   an unbiased truncated-slice agreement estimate (the stratified slice was 40%
   hand-picked boundary cases, so its 50% agreement number is depressed).

Writes results/followup_results.json. Reuses compare_models callers + stratified pick.
"""
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_models as cm  # noqa: E402
import stratified_rerun as sr  # noqa: E402
from _common import RESULTS_DIR, DB_PATH  # noqa: E402

REPS = 3
WORKERS = 6


def main():
    import sqlite3
    # Probe A's summary needs the stratified run's output — check BEFORE spending
    # ~120 paid model calls, not after (the load at the bottom would otherwise be
    # the first thing to notice it's missing).
    strat_path = RESULTS_DIR / "stratified_results.json"
    if not strat_path.exists():
        sys.exit(f"missing {strat_path} — run stratified_rerun.py first")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # --- probe A rows: the stratified run's truncated slice, re-materialized
    strat_sample = sr.pick_sample(conn)
    trunc_a = [r for s, r in strat_sample if s == "truncated"]
    used_urls = {r["job_url"] for _, r in strat_sample}

    # --- probe B rows: fresh truncated picks, deterministic pseudo-random order
    # (url tails are Adzuna id/hash noise — arbitrary w.r.t. content but stable).
    trunc_b = []
    for r in conn.execute(
            "SELECT * FROM jobs WHERE status='evaluated' AND verdict IS NOT NULL "
            "AND length(trim(description)) <= 520 AND source='adzuna' "
            "ORDER BY substr(job_url, -9), job_url LIMIT 40").fetchall():
        if r["job_url"] not in used_urls and len(trunc_b) < 10:
            trunc_b.append(r)
    print(f"probe A: {len(trunc_a)} boundary-slice postings x luna-xhigh x {REPS}")
    print(f"probe B: {len(trunc_b)} fresh random truncated postings x 3 models x {REPS}\n",
          flush=True)

    MODELS_B = [
        ("ds-flash",  cm.call_deepseek, "deepseek-v4-flash", None),
        ("luna",      cm.call_openai,   "gpt-5.6-luna", None),
        ("luna-high", cm.call_openai,   "gpt-5.6-luna", {"reasoning_effort": "high"}),
    ]
    XHIGH = ("luna-xhigh", cm.call_openai, "gpt-5.6-luna", {"reasoning_effort": "xhigh"})

    tasks = []
    for pi, row in enumerate(trunc_a):
        for rep in range(REPS):
            tasks.append(("A", pi, XHIGH, rep, row))
    for pi, row in enumerate(trunc_b):
        for m in MODELS_B:
            for rep in range(REPS):
                tasks.append(("B", pi, m, rep, row))

    res_a = [{"title": r["title"], "company": r["company"],
              "reps": [None] * REPS} for r in trunc_a]
    res_b = [{"title": r["title"], "company": r["company"],
              "models": {m[0]: [None] * REPS for m in MODELS_B}} for r in trunc_b]

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {}
        for probe, pi, (lab, caller, model, extra), rep, row in tasks:
            fut = ex.submit(sr.one_call, caller, model, extra, sr.user_msg_for(row))
            futs[fut] = (probe, pi, lab, rep)
        for fut in as_completed(futs):
            probe, pi, lab, rep = futs[fut]
            r = fut.result()
            if probe == "A":
                res_a[pi]["reps"][rep] = r
            else:
                res_b[pi]["models"][lab][rep] = r
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(tasks)} calls done", flush=True)

    with open(RESULTS_DIR / "followup_results.json", "w", encoding="utf-8") as f:
        json.dump({"probe_a_xhigh": res_a, "probe_b_random_truncated": res_b},
                  f, indent=2, ensure_ascii=False)

    # ---- probe A summary: xhigh vs the stratified luna/luna-high truncated rows
    print("\n" + "=" * 70)
    print("PROBE A — same 10 boundary-slice postings, luna effort dial complete")
    strat = json.load(open(strat_path, encoding="utf-8"))
    strunc = [r for r in strat if r["slice"] == "truncated"]
    for lab, evs in [
        ("luna(default)", [m for r in strunc for m in r["models"]["luna"] if m and m["ok"]]),
        ("luna-high",     [m for r in strunc for m in r["models"]["luna-high"] if m and m["ok"]]),
        ("luna-xhigh",    [m for r in res_a for m in r["reps"] if m and m["ok"]]),
    ]:
        cnt = Counter(m["verdict"] for m in evs)
        otok = sum(m["out_tok"] for m in evs) / max(len(evs), 1)
        lat = sum(m["latency"] for m in evs) / max(len(evs), 1)
        print(f"  {lab:<13} PASS {cnt.get('PASS', 0):>2}  RO {cnt.get('RECRUITER_ONLY', 0):>2}  "
              f"GF {cnt.get('GATE_FAIL', 0):>2}  | avg out_tok {otok:>5.0f}  avg lat {lat:>5.1f}s")

    # ---- probe B summary: unbiased truncated slice
    print("\nPROBE B — 10 random truncated postings (boundary cases excluded)")
    for lab, *_ in MODELS_B:
        evs = [m for r in res_b for m in r["models"][lab] if m and m["ok"]]
        cnt = Counter(m["verdict"] for m in evs)
        nerr = sum(1 for r in res_b for m in r["models"][lab] if m and not m["ok"])
        unstable = sum(1 for r in res_b
                       if len({m["verdict"] for m in r["models"][lab] if m and m["ok"]}) > 1)
        print(f"  {lab:<9} PASS {cnt.get('PASS', 0):>2}  RO {cnt.get('RECRUITER_ONLY', 0):>2}  "
              f"GF {cnt.get('GATE_FAIL', 0):>2}  ERR {nerr}  | unstable {unstable}/10")
    for lab in ("luna", "luna-high"):
        both = agree = 0
        for r in res_b:
            mf = sr.majority([m["verdict"] for m in r["models"]["ds-flash"] if m and m["ok"]])
            ml = sr.majority([m["verdict"] for m in r["models"][lab] if m and m["ok"]])
            if mf and ml:
                both += 1
                agree += (mf == ml)
        pct = 100 * agree / both if both else 0
        print(f"  majority agreement {lab} vs ds-flash: {agree}/{both} ({pct:.0f}%)")


if __name__ == "__main__":
    main()
