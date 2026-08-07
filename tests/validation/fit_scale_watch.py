#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Watch the judge's scale for drift: daily fit-score mean, verdict mix, response
verbosity, and per-eval output tokens.

Why this exists: the 2026-07-31 V4-Flash build change shifted the fit-score level
(~10.9 -> ~9.6 over a week) without breaking routing, and the cold-apply bar was
lowered 15 -> 14 to compensate (CHANGELOG 2026-08-07). The standing watch item is
"if the scale keeps sliding, move the bar again, not the scoring rules" — this
script is the instrument for that check, so nobody has to hand-write the queries
each time.

Everything here is read-only and descriptive:
- fit-score mean/median over gates-passed rows, bucketed by date(first_seen) — an
  approximation of eval date (rows are normally evaluated within a day of insert)
- verdict mix per day (GATE_FAIL / PASS / RECRUITER_ONLY shares)
- median eval_json length per day (answer verbosity; reasoning is not stored)
- per-eval output tokens per day, parsed from logs/pipeline-*.log "[eval] done"
  lines paired with "postings to evaluate" counts (unmatched starts — crashed or
  overlapping runs — are dropped from the pairing, so days with an interrupted run
  under-count evals slightly)

Interpretation guardrails, learned the hard way (see CHANGELOG 2026-08-07 and the
probe scripts next door): single-day wiggles are noise — the judge's verdict noise
floor is ~17-18% per draw and completion tokens vary ~60% CoV on identical input.
Read TRENDS over 5+ days, not points. This script never changes the bar itself;
that stays a human decision recorded in the guide + CHANGELOG.

Run:  python tests/validation/fit_scale_watch.py [--days N]   (default 21)
"""
import argparse
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core

DONE_RE = re.compile(
    r"\[eval\] done \| tokens: (\d+) in, (\d+) cache-read, (\d+) cache-write, "
    r"(\d+) out \| est\. cost \$([\d.]+)")
START_RE = re.compile(r"\[eval\] (\d+) postings to evaluate")


def db_daily(conn, since):
    """{day: {n, fit_mean, fit_median, mix, ejson_median}} for gates-passed rows."""
    rows = conn.execute(
        """SELECT date(first_seen) AS d, verdict, fit_score, length(eval_json) AS L
           FROM jobs
           WHERE status='evaluated' AND date(first_seen) >= ?""",
        (since,)).fetchall()
    days = defaultdict(lambda: {"fits": [], "mix": defaultdict(int), "lens": []})
    for r in rows:
        b = days[r["d"]]
        b["mix"][r["verdict"]] += 1
        if r["L"]:
            b["lens"].append(r["L"])
        if r["verdict"] != "GATE_FAIL" and isinstance(r["fit_score"], int):
            b["fits"].append(r["fit_score"])
    return days


def log_daily(since):
    """{day: (evals, out_tokens)} from the dated pipeline logs; pairs each done-line
    with the smallest unmatched start counts (an interrupted run has a start but no
    done and must not inflate the denominator)."""
    out = {}
    for f in sorted(core.BASE_DIR.glob("logs/pipeline-*.log")):
        day = f.stem.replace("pipeline-", "")
        if day < since:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        starts = sorted(int(m) for m in START_RE.findall(text))
        dones = DONE_RE.findall(text)
        n_done = len(dones)
        evals = sum(starts[:n_done]) if len(starts) > n_done else sum(starts)
        out[day] = (evals, sum(int(d[3]) for d in dones))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    args = ap.parse_args()
    since = (date.today() - timedelta(days=args.days)).isoformat()

    cfg = core.load_config()
    conn = core.connect_db(cfg)
    conn.row_factory = sqlite3.Row

    days = db_daily(conn, since)
    logs = log_daily(since)

    print(f"scale watch — last {args.days} days (fit over gates-passed rows; "
          f"out/eval from logs)\n")
    print(f"{'day':<12}{'evald':>6}{'fit mean':>9}{'med':>5}{'GF%':>6}{'PASS%':>7}"
          f"{'RO%':>6}{'ejson med':>10}{'out/eval':>9}")
    fit_series = []
    for d in sorted(days):
        b = days[d]
        n = sum(b["mix"].values())
        fits = b["fits"]
        fm = statistics.mean(fits) if fits else float("nan")
        if fits:
            fit_series.append((d, fm))
        gf = 100 * b["mix"].get("GATE_FAIL", 0) / n if n else 0
        pa = 100 * b["mix"].get("PASS", 0) / n if n else 0
        ro = 100 * b["mix"].get("RECRUITER_ONLY", 0) / n if n else 0
        ej = statistics.median(b["lens"]) if b["lens"] else float("nan")
        ev, ot = logs.get(d, (0, 0))
        ope = f"{ot/ev:,.0f}" if ev else "-"
        print(f"{d:<12}{n:>6}{fm:>9.2f}{statistics.median(fits) if fits else float('nan'):>5.0f}"
              f"{gf:>6.0f}{pa:>7.0f}{ro:>6.0f}{ej:>10.0f}{ope:>9}")

    if len(fit_series) >= 10:
        half = len(fit_series) // 2
        a = statistics.mean(v for _, v in fit_series[:half])
        b = statistics.mean(v for _, v in fit_series[half:])
        print(f"\nfirst-half fit mean {a:.2f}  ->  second-half {b:.2f}  "
              f"(delta {b - a:+.2f})")
        print("reminder: the bar moved 15 -> 14 on 2026-08-07 for a ~-1.3 level "
              "shift; a further sustained slide of similar size is the trigger to "
              "revisit the bar (guide Part 2.5), not the scoring rules.")


if __name__ == "__main__":
    main()
