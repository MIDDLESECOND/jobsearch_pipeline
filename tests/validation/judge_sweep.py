"""Run a roster of judge columns over the FROZEN stratified sample, N draws each.

`compare_models.py` samples the DB live (LIMIT 25, newest-ish) — right for a quick
cross-model spot check, wrong for a selection decision, where every candidate must
face the same postings as every other candidate, including candidates measured weeks
apart. This script pins the population to `judge_sample.local.json`
(`make_judge_sample.py`, frozen 2026-08-12: 50 rows at the real fit>=15 source mix)
and reuses compare_models' callers/parsing/pricing so a column measured here is
directly comparable to the 9-column matrix already in results/.

Draws are separate files (`<name>_run{n}.json`, same record shape as
`judge_matrix_run*.json`), and an existing file is SKIPPED rather than overwritten:
a sweep that dies at draw 4 resumes without re-paying for draws 1-3. That already
happened once — the crash is why this instrument lives in the repo instead of a
scratchpad, where the previous one was lost.

    python tests/validation/judge_sweep.py pro0813 --draws 2

Rosters are named because the columns ARE the experiment: `pro0813` measures the
V4 Pro GA build across the reasoning ladder, with `low` as the bridge column (the
tier both the 2026-08-11 Pro measurement and production flash ran at, so a shift
there is a build change, not a knob change).
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import compare_models as cm  # noqa: E402  (its import side effects build SYSTEM/clients)
import evaluation  # noqa: E402
from _common import RESULTS_DIR, DB_PATH  # noqa: E402

SAMPLE_PATH = pathlib.Path(__file__).with_name("judge_sample.local.json")
CONCURRENCY = 6

# (label, provider, model, extra request params)
ROSTERS = {
    # DeepSeek V4 Pro GA (0813). The API validates reasoning_effort against
    # none/minimal/low/medium/high/xhigh/max (probed 2026-08-13; an unknown value is a
    # loud 400, never a silent coerce), and the tier is not just a speed knob — the
    # 2026-08-07 flash work found non-thinking behaves as a materially stricter judge.
    # So each tier is its own candidate configuration, not a tuning of one column.
    "pro0813": [
        ("pro-low", "deepseek", "deepseek-v4-pro", {"reasoning_effort": "low"}),
        ("pro-high", "deepseek", "deepseek-v4-pro", {"reasoning_effort": "high"}),
        ("pro-max", "deepseek", "deepseek-v4-pro", {"reasoning_effort": "max"}),
    ],
}


def load_sample_rows():
    urls = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))["job_urls"]
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    marks = ",".join("?" * len(urls))
    rows = con.execute(
        f"SELECT * FROM jobs WHERE job_url IN ({marks})", urls).fetchall()
    con.close()
    by_url = {r["job_url"]: r for r in rows}
    missing = [u for u in urls if u not in by_url]
    if missing:
        # Frozen means frozen: a pruned description would silently change the
        # experiment, so refuse rather than measure a different population.
        sys.exit(f"{len(missing)} frozen sample row(s) no longer in the DB: {missing[:3]}")
    return [by_url[u] for u in urls]


def run_draw(rows, roster, out_path):
    def one(idx_row):
        i, r = idx_row
        user_msg = evaluation.build_user_msg(r)
        rec = {"title": r["title"], "company": r["company"],
               "search": r["search_name"], "models": {}}
        for label, provider, model, extra in roster:
            rec["models"][label] = cm.evaluate(provider, model, user_msg, extra)
        done = [rec["models"][lbl].get("verdict") if rec["models"][lbl]["ok"] else "ERR"
                for lbl, *_ in roster]
        print(f"  [{i:>2}/{len(rows)}] {(r['title'] or '')[:34]:<34} "
              + " ".join(f"{lbl}={str(v):<14}" for (lbl, *_), v in zip(roster, done)),
              flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(one, enumerate(rows, 1)))
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return results


def tally(results, roster):
    """Per-column spend and parse health — the cost half of the decision."""
    for label, _p, model, _e in roster:
        pin, pout, ok = 0, 0, 0
        for rec in results:
            m = rec["models"].get(label) or {}
            if m.get("ok"):
                ok += 1
                pin += m.get("in_tok", 0)
                pout += m.get("out_tok", 0)
            price_in, price_out = cm.PRICES.get(model, (0.0, 0.0))
        cost = pin * price_in + pout * price_out
        n = len(results)
        print(f"    {label:<10} parsed {ok}/{n}  in {pin:>8,}  out {pout:>8,}  "
              f"${cost:.3f}  (${cost / max(n, 1) * 1000:.2f}/1k postings)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("roster", choices=sorted(ROSTERS))
    ap.add_argument("--draws", type=int, default=2)
    ap.add_argument("--name", help="output prefix (default: the roster name)")
    args = ap.parse_args()

    roster = ROSTERS[args.roster]
    name = args.name or f"judge_{args.roster}"
    rows = load_sample_rows()
    print(f"frozen sample: {len(rows)} postings | roster {args.roster}: "
          f"{', '.join(l for l, *_ in roster)} | {args.draws} draw(s)\n")

    for draw in range(1, args.draws + 1):
        out = RESULTS_DIR / f"{name}_run{draw}.json"
        if out.exists():
            print(f"draw {draw}: {out.name} exists — skipping (resume)\n")
            continue
        print(f"draw {draw} -> {out.name}")
        t0 = time.monotonic()
        results = run_draw(rows, roster, out)
        print(f"  draw {draw} done in {(time.monotonic() - t0) / 60:.1f} min")
        tally(results, roster)
        print()


if __name__ == "__main__":
    main()
