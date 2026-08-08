#!/usr/bin/env python3
"""Re-cut an existing noise_probe result by ACTION boundaries, not verdict strings.

noise_probe.py measured the population verdict-flip rate (~18% on 2026-08-07). But a
flip only costs the user something when it crosses a line an action hangs on:

  - cold_apply:       verdict PASS  and fit >= --apply-bar (13, the manual triage bar)
  - recruiter_route:  verdict RECRUITER_ONLY and fit >= --route-bar (15, the Action
                      Center queue's default min score)
  - none:             everything else — a scored row below both bars and a GATE_FAIL
                      row differ in *visibility*, but neither is acted on.

This script answers two questions the raw flip rate can't:

  1. TIER A rate — what fraction of postings would change ACTION on a rerun
     (draws disagree on the cold_apply / recruiter_route / none mapping)?
     TIER B — visibility-or-verdict flips that never touch an action.
  2. TRIGGER TABLE — for each candidate "should production arbitrate this draw?"
     predicate, treating each rep in turn as the one production draw: how often it
     fires (cost) vs how many Tier-A postings it fires on (catch). This is the
     targeting data for the boundary-band majority-vote mechanism in evaluation.py.

Reads the newest results/noise_probe_*.json by default. Pure offline re-analysis —
no DB, no API calls, no cost. Writes results/flip_consequence_<stamp>.json.
"""
import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from states import VERDICT_PASS, VERDICT_RECRUITER_ONLY, VERDICT_GATE_FAIL  # noqa: E402

RESULTS_DIR = Path(__file__).with_name("results")

# deepseek-v4-flash output price (evaluation.MODEL_PRICES) — for the per-100-postings
# cost line only; arbitration cost is ~all output tokens (input is cache-hit).
OUT_PRICE = 0.28 / 1e6


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def action(verdict, fit, apply_bar, route_bar):
    """Map one draw to the action it would produce. fit=None on a scored verdict
    (model omitted the score) can't reach a bar — 'none', same as below-bar."""
    if verdict == VERDICT_PASS and fit is not None and fit >= apply_bar:
        return "cold_apply"
    if verdict == VERDICT_RECRUITER_ONLY and fit is not None and fit >= route_bar:
        return "recruiter_route"
    return "none"


def make_triggers(apply_bar, route_bar):
    """Candidate 'arbitrate this draw?' predicates over (verdict, fit).
    Bands surround the two bars; the GATE_FAIL variants price the leak where a
    wrong first draw is a GATE_FAIL (no fit score to band on)."""
    def band(lo, hi):
        return lambda v, f: v != VERDICT_GATE_FAIL and f is not None and lo <= f <= hi

    triggers = {
        f"fit {apply_bar - 2}-{route_bar + 2}": band(apply_bar - 2, route_bar + 2),
        f"fit {apply_bar - 1}-{route_bar + 1}": band(apply_bar - 1, route_bar + 1),
        f"fit {apply_bar - 2}-{route_bar + 1}": band(apply_bar - 2, route_bar + 1),
        "all scored": lambda v, f: v != VERDICT_GATE_FAIL,
        "scored, fit>=10": lambda v, f: v != VERDICT_GATE_FAIL
        and f is not None and f >= 10,
        "always (cost ceiling)": lambda v, f: True,
    }
    wide = band(apply_bar - 2, route_bar + 2)
    triggers[f"fit {apply_bar - 2}-{route_bar + 2} OR gate_fail"] = (
        lambda v, f: v == VERDICT_GATE_FAIL or wide(v, f))
    return triggers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=Path, default=None,
                    help="noise_probe result JSON (default: newest in results/)")
    ap.add_argument("--apply-bar", type=int, default=13)
    ap.add_argument("--route-bar", type=int, default=15)
    args = ap.parse_args()

    probe = args.probe
    if probe is None:
        candidates = sorted(RESULTS_DIR.glob("noise_probe_*.json"))
        if not candidates:
            sys.exit("no results/noise_probe_*.json — run noise_probe.py first")
        probe = candidates[-1]
    data = json.loads(probe.read_text(encoding="utf-8"))
    print(f"probe: {probe.name}  (model={data['model']} effort={data['effort']} "
          f"n={data['n']} reps={data['reps']})")
    print(f"bars: cold_apply = PASS & fit>={args.apply_bar}   "
          f"recruiter_route = RECRUITER_ONLY & fit>={args.route_bar}\n")

    by = defaultdict(list)
    for r in data["records"]:
        by[r["job_url"]].append(r)

    postings = []
    unmeasured = 0
    for url, rs in by.items():
        draws = [r for r in rs if r.get("verdict")]
        if len(draws) < 2:
            unmeasured += 1
            continue
        acts = [action(r["verdict"], r.get("fit_score"),
                       args.apply_bar, args.route_bar) for r in draws]
        verds = [r["verdict"] for r in draws]
        postings.append({
            "job_url": url,
            "stored_verdict": rs[0]["stored_verdict"],
            "stored_fit": rs[0]["stored_fit"],
            "draws": [{"verdict": r["verdict"], "fit": r.get("fit_score"),
                       "action": a} for r, a in zip(draws, acts)],
            "verdict_flip": len(set(verds)) > 1,
            "tier_a": len(set(acts)) > 1,
        })

    n = len(postings)
    vflips = [p for p in postings if p["verdict_flip"]]
    tier_a = [p for p in postings if p["tier_a"]]
    tier_b = [p for p in postings if p["verdict_flip"] and not p["tier_a"]]
    lo, hi = wilson(len(tier_a), n)
    vlo, vhi = wilson(len(vflips), n)

    print("=" * 72)
    print(f"verdict flips (noise_probe's number): {len(vflips)}/{n} = "
          f"{100 * len(vflips) / n:.0f}%  (Wilson 95% {100 * vlo:.0f}-{100 * vhi:.0f}%)")
    print(f"TIER A — action would change:        {len(tier_a)}/{n} = "
          f"{100 * len(tier_a) / n:.0f}%  (Wilson 95% {100 * lo:.0f}-{100 * hi:.0f}%)")
    print(f"TIER B — verdict/visibility only:    {len(tier_b)}/{n} = "
          f"{100 * len(tier_b) / n:.0f}%")
    if unmeasured:
        print(f"  ({unmeasured} postings with <2 parseable draws excluded)")

    print("\nTier A cases (the flips worth paying to stabilize):")
    for p in tier_a:
        d = ", ".join(f"{x['verdict']}/{x['fit']}→{x['action']}" for x in p["draws"])
        print(f"  stored {p['stored_verdict']}/{p['stored_fit']}: {d}")

    # ---- trigger table: each rep in turn plays "the one production draw" ----
    triggers = make_triggers(args.apply_bar, args.route_bar)
    n_draws = sum(len(p["draws"]) for p in postings)
    tier_a_draws = sum(len(p["draws"]) for p in tier_a)
    toks = [r["completion_tok"] for r in data["records"] if r.get("completion_tok")]
    med_tok = statistics.median(toks) if toks else 0

    print("\ntrigger table (fire = % of first draws arbitrated; catch = % of Tier-A")
    print("postings whose first draw would have triggered, averaged over reps):")
    print(f"  {'trigger':<28} {'fire':>6} {'catch':>7}   est. extra $/100 postings")
    table = {}
    for name, fn in triggers.items():
        fires = sum(1 for p in postings for d in p["draws"]
                    if fn(d["verdict"], d["fit"]))
        catches = sum(1 for p in tier_a for d in p["draws"]
                      if fn(d["verdict"], d["fit"]))
        fire = fires / n_draws
        catch = catches / tier_a_draws if tier_a_draws else 0.0
        # 2 extra draws per fire, ~median output tokens each; input is cache-hit.
        cost100 = fire * 100 * 2 * med_tok * OUT_PRICE
        table[name] = {"fire": fire, "catch": catch, "extra_usd_per_100": cost100}
        print(f"  {name:<28} {100 * fire:>5.0f}% {100 * catch:>6.0f}%   ${cost100:.3f}")

    print(f"\n(cost basis: median completion {med_tok:,.0f} tok/draw, "
          f"${OUT_PRICE * 1e6:.2f}/M out, input assumed cache-hit)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"flip_consequence_{stamp}.json"
    out_path.write_text(json.dumps({
        "probe": probe.name, "apply_bar": args.apply_bar, "route_bar": args.route_bar,
        "n": n, "verdict_flips": len(vflips), "tier_a": len(tier_a),
        "tier_b": len(tier_b), "tier_a_ci": [lo, hi], "triggers": table,
        "tier_a_cases": tier_a,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
