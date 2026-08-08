#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Sentinel canary for silent judge drift (the 0731 problem, made same-week visible).

DeepSeek serves ONE moving build per model name: the 2026-07-31 swap changed output
tokens 1.2k->3.4k, added 50-100 JSON retries/day, and slid the fit mean 10.9->9.6 —
and it was caught a week later by a manual population audit. This script turns that
into a scheduled measurement: a FIXED set of sentinel postings, frozen at --init
(inputs are copied into the set file, so later DB pruning or relisting churn cannot
change what the judge is asked), re-evaluated with the EXACT production request body
(evaluation.deepseek_request_body + build_user_msg), with each run appended to
results/canary_history.jsonl and compared against the baseline entry.

Usage:
  python tests/validation/canary.py --init [--per-class 8] [--force]  # freeze the set
  python tests/validation/canary.py                                   # run + compare
  python tests/validation/canary.py --rebaseline   # run, and make THIS entry the new
                                                   # baseline (after an accepted change:
                                                   # new model, new guide, new band)

Alert thresholds (exit code 2 when any trips) — calibrated to THIS SET's measured
noise floor, not the population's. The 8/8/8 stratified set is boundary-heavy by
design, and per-class pairwise agreement between clean temp-0 reruns (08-07 noise
probe) is GATE_FAIL 0.85 / PASS 0.83 / RECRUITER_ONLY 0.67 -> stratified expectation
~0.78, 2sd lower bound ~0.61 at n=24. The first live comparison (2026-08-08, two runs
30 min apart) hit 67% agreement and a -2.88 UNPAIRED fit-mean shift — both inside
noise once cut correctly (paired delta was -0.88): unpaired means mix in composition
shift when postings flip in/out of the scored set, so the fit alert is PAIRED-only.
  - verdict agreement vs baseline < 60%
  - |paired fit delta| > 1.2 (mean over postings scored in BOTH runs; per-pair sd
    1.97 -> mean sd ~0.6 at typical pair counts; 0731-scale drift measured -1.3)
  - completion-token median ratio outside 0.6-1.6x  (0731 measured x2.8)
  - parse failures + empty answers > 10% of draws   (0731: 0 -> 50-100/day)
One tripped alert is a SIGNAL to run noise_probe.py / backtest_v2.py, not proof by
itself. Cost per run: ~n x $0.001. `--recheck` re-runs the comparison of the LAST
history entry against the baseline offline (no API, no append) — for threshold work.

The set file names real postings -> *.local.json (gitignored), same split as
backtest_cases.local.json. DeepSeek-only by design: this watches the production judge.
"""
import argparse
import json
import statistics
import sys
import time
from collections import Counter
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
from states import VERDICTS
from _common import RESULTS_DIR

SET_PATH = Path(__file__).with_name("canary_set.local.json")
HISTORY_PATH = RESULTS_DIR / "canary_history.jsonl"
TIMEOUT = 300

AGREE_FLOOR = 0.60
PAIRED_FIT_DELTA = 1.2
TOK_RATIO = (0.6, 1.6)
BAD_DRAW_CAP = 0.10

# Frozen per sentinel — everything build_user_msg reads, plus the context columns.
FROZEN_COLS = ("job_url", "title", "company", "location", "search_name", "tier",
               "salary_min", "salary_max", "description")


def _init_set(per_class, seed, force):
    import random
    import sqlite3
    if SET_PATH.exists() and not force:
        sys.exit(f"{SET_PATH.name} already exists — a canary only works if the set "
                 f"stays FIXED. --force to rebuild (then --rebaseline the next run).")
    conn = core.connect_db(core.load_config())
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    sentinels = []
    # Fixed count per verdict class, not population proportions: the canary wants
    # SENSITIVITY in every class (a drift that only moves PASS rows must not drown
    # in the population's GATE_FAIL majority).
    for verdict in VERDICTS:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status='evaluated' AND verdict=? "
            "AND description IS NOT NULL AND length(trim(description)) > 200 "
            "ORDER BY job_url", (verdict,)).fetchall()
        if len(rows) < per_class:
            print(f"[canary] only {len(rows)} {verdict} rows available "
                  f"(wanted {per_class})", file=sys.stderr)
        for r in rng.sample(rows, min(per_class, len(rows))):
            s = {c: r[c] for c in FROZEN_COLS}
            s["stored_verdict"] = r["verdict"]
            s["stored_fit"] = r["fit_score"]
            sentinels.append(s)
    SET_PATH.write_text(json.dumps({
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed, "per_class": per_class, "sentinels": sentinels,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"froze {len(sentinels)} sentinels "
          f"({Counter(s['stored_verdict'] for s in sentinels)}) -> {SET_PATH.name}")
    print("now run without --init to seed the baseline entry.")


def _call(api_key, model, system_prompt, user_msg):
    """Production request body; one retry on any failure (the canary must not
    confuse a transient network drop with judge drift)."""
    for attempt in (0, 1):
        try:
            r = httpx.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=evaluation.deepseek_request_body(model, system_prompt, user_msg),
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            break
        except Exception as e:
            if attempt:
                return {"transport_error": f"{type(e).__name__}: {e}"[:160]}
            time.sleep(5)
    d = r.json()
    u = d.get("usage", {})
    content = d["choices"][0]["message"].get("content") or ""
    cache_read = u.get("prompt_cache_hit_tokens", 0)
    out = {"completion_tok": u.get("completion_tokens", 0),
           "fresh_in_tok": u.get("prompt_tokens", 0) - cache_read,
           "cache_read_tok": cache_read,
           "empty_answer": len(content.strip()) == 0}
    try:
        res = evaluation.normalize_result(evaluation.parse_eval_json(content))
        out.update({"parsed": True, "verdict": res.get("verdict"),
                    "fit_score": res.get("fit_score")})
    except Exception as e:
        out.update({"parsed": False, "parse_error": str(e)[:120]})
    return out


def _load_history():
    if not HISTORY_PATH.exists():
        return []
    return [json.loads(line) for line in
            HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _compare(entry, base, stored_by_url):
    """Print the drift comparison, return the tripped alerts. Pure over the two
    history entries + the set's stored-class map — the same code path serves the
    live run and --recheck, so threshold work can't drift from production."""
    alerts = []
    if base["model"] != entry["model"] or base["effort"] != entry["effort"]:
        print(f"NOTE: baseline is {base['model']}/{base['effort']} — an agreement "
              f"break is EXPECTED; --rebaseline after accepting the change.")
    shared = [u for u, v in entry["verdicts"].items() if v and base["verdicts"].get(u)]
    agree = sum(entry["verdicts"][u] == base["verdicts"][u] for u in shared)
    rate = agree / len(shared) if shared else 0.0
    print(f"vs baseline {base['ts']} ({base['model']}/{base['effort']}):")
    print(f"  verdict agreement: {agree}/{len(shared)} = {rate:.0%}  "
          f"(alert < {AGREE_FLOOR:.0%}; stratified noise floor ~78%)")
    by_class = Counter()
    n_class = Counter()
    for u in shared:
        c = stored_by_url.get(u, "?")
        n_class[c] += 1
        by_class[c] += entry["verdicts"][u] == base["verdicts"][u]
    for c in sorted(n_class):
        print(f"    {c:>15}: {by_class[c]}/{n_class[c]} agree")
    if rate < AGREE_FLOOR:
        alerts.append(f"verdict agreement {rate:.0%}")
    for u in shared:
        if entry["verdicts"][u] != base["verdicts"][u]:
            print(f"    {base['verdicts'][u]:>14} -> {entry['verdicts'][u]:<14} {u}")

    paired = [(base["fits"][u], entry["fits"][u]) for u in entry.get("fits", {})
              if base.get("fits", {}).get(u) is not None
              and entry["fits"][u] is not None]
    if paired:
        delta = statistics.mean(b - a for a, b in paired)
        print(f"  paired fit delta: {delta:+.2f} over {len(paired)} postings scored "
              f"in both (alert |Δ| > {PAIRED_FIT_DELTA}; unpaired means "
              f"{base.get('fit_mean')} -> {entry.get('fit_mean')} carry composition "
              f"shift — reported, never alerted on)")
        if abs(delta) > PAIRED_FIT_DELTA:
            alerts.append(f"paired fit delta {delta:+.2f}")
    if entry.get("tok_median") and base.get("tok_median"):
        ratio = entry["tok_median"] / base["tok_median"]
        print(f"  tok median: {base['tok_median']:,.0f} -> {entry['tok_median']:,.0f} "
              f"(x{ratio:.2f}; alert outside {TOK_RATIO[0]}-{TOK_RATIO[1]})")
        if not TOK_RATIO[0] <= ratio <= TOK_RATIO[1]:
            alerts.append(f"tok median x{ratio:.2f}")
    return alerts


def _stored_classes():
    if not SET_PATH.exists():
        return {}
    return {s["job_url"]: s["stored_verdict"]
            for s in json.loads(SET_PATH.read_text(encoding="utf-8"))["sentinels"]}


def _baseline(history):
    for entry in reversed(history):
        if entry.get("rebaseline"):
            return entry
    return history[0] if history else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--per-class", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260808)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rebaseline", action="store_true",
                    help="mark THIS run as the new comparison baseline")
    ap.add_argument("--recheck", action="store_true",
                    help="re-compare the LAST history entry offline (no API call)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    if args.init:
        _init_set(args.per_class, args.seed, args.force)
        return

    if args.recheck:
        history = _load_history()
        if len(history) < 2:
            sys.exit("recheck needs at least 2 history entries")
        base = _baseline(history[:-1])
        alerts = _compare(history[-1], base, _stored_classes())
        if alerts:
            for a in alerts:
                print(f"ALERT: {a}")
            sys.exit(2)
        print("  no drift alerts.")
        return

    if not SET_PATH.exists():
        sys.exit(f"no {SET_PATH.name} — run with --init first")
    sentinels = json.loads(SET_PATH.read_text(encoding="utf-8"))["sentinels"]

    cfg = core.load_config()
    model = cfg["settings"]["model"]
    api_key = core._ensure_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set")
    system_prompt = evaluation.build_system_prompt()
    print(f"canary: {len(sentinels)} sentinels via {model} "
          f"(effort={evaluation.DEEPSEEK_EFFORT})")

    def run(s):
        return {"job_url": s["job_url"], "stored_verdict": s["stored_verdict"],
                **_call(api_key, model, system_prompt, evaluation.build_user_msg(s))}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        draws = list(ex.map(run, sentinels))
    print(f"completed in {time.time() - t0:.0f}s")

    ok = [d for d in draws if d.get("parsed") and not d.get("empty_answer")]
    bad = len(draws) - len(ok)
    fits = [d["fit_score"] for d in ok if d.get("fit_score") is not None]
    toks = [d["completion_tok"] for d in draws if d.get("completion_tok")]
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "effort": evaluation.DEEPSEEK_EFFORT,
        "n": len(draws), "bad_draws": bad,
        "verdict_counts": dict(Counter(d.get("verdict") for d in ok)),
        "fit_mean": round(statistics.mean(fits), 2) if fits else None,
        "tok_median": statistics.median(toks) if toks else None,
        "verdicts": {d["job_url"]: d.get("verdict") for d in draws},
        "fits": {d["job_url"]: d.get("fit_score") for d in ok},
        "rebaseline": bool(args.rebaseline),
    }

    history = _load_history()
    base = _baseline(history)
    alerts = []
    if bad / len(draws) > BAD_DRAW_CAP:
        alerts.append(f"bad draws {bad}/{len(draws)} (> {BAD_DRAW_CAP:.0%})")

    if base is None or args.rebaseline:
        which = "REBASELINED" if base is not None else "baseline seeded"
        print(f"\n{which}: fit_mean={entry['fit_mean']} tok_median={entry['tok_median']} "
              f"verdicts={entry['verdict_counts']}")
    else:
        print()
        alerts.extend(_compare(entry, base, _stored_classes()))

    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    fresh = sum(d.get("fresh_in_tok", 0) for d in draws)
    cache = sum(d.get("cache_read_tok", 0) for d in draws)
    out_t = sum(d.get("completion_tok", 0) for d in draws)
    pin, pout = evaluation.MODEL_PRICES.get(model, (0.0, 0.0))
    print(f"  cost ~${(fresh + cache * 0.1) * pin + out_t * pout:.3f} | "
          f"history: {len(history) + 1} entries in {HISTORY_PATH.name}")

    if alerts:
        print("\n" + "!" * 66)
        for a in alerts:
            print(f"ALERT: {a}")
        print("next: python tests/validation/noise_probe.py  (is it variance?)")
        print("      python tests/validation/backtest_v2.py  (did judgments move?)")
        print("!" * 66)
        sys.exit(2)
    print("  no drift alerts.")


if __name__ == "__main__":
    main()
