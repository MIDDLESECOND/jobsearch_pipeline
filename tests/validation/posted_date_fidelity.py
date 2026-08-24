#!/usr/bin/env python3
"""Does an Adzuna row's `date_posted` mean what the cold-apply bar assumes it means?

The question, and only this one: the standing allocation gates cold applies on
"posted <= 14 days". `core.recency_dt` reads Adzuna rows in mode='posted' -- it TRUSTS
their `date_posted` -- and 100% of Adzuna rows carry one (measured 2026-08-20:
55,736/55,736 inside a 45-day window; LinkedIn is the opposite, 23,703/23,713 carry
none and fall back to first_seen). So the bar is never evaluated on a missing value.
It is evaluated on a value that may not be the employer's posting date at all.

Two readings already exist and disagree in magnitude, which is why this probe exists:

  - Aggregator vs aggregator (DB-only, n=398 shared requisitions, 2026-08-20):
    Adzuna's date vs Dice's `postedDate`, median -0.1d, but 12% (47/398) are >3 days
    later on the Adzuna side, p90 4.0d, max 13.6d.
  - Aggregator vs EMPLOYER (browser, n=2, 2026-08-20): PNM Resources "Gen AI
    Technologist Lead" -- Adzuna 2026-08-19, employer board (careers.txnmenergy.com)
    "Date Posted: Jun 3, 2026" = 78 days. Wilson Sonsini "Innovations and AI Solutions
    Engineer" -- Adzuna 2026-08-18, Dice "Posted 30+ days ago", Workday req R1579 same.

n=2 is not a rate. This probe turns it into one.

Population under test is deliberately NOT all Adzuna rows. It is the rows a cold-apply
decision could actually hinge on: evaluated, undecided, unfiltered, PASS at or above the
allocation's fit bar, and reading <= FRESH_DAYS through the same `recency_dt` the report,
the UI, and second_judge.pending_rows all use. Sampling rows that LOOK fresh is the point
-- "how often does a fresh-looking row turn out stale" is the decision-relevant question,
and a sample drawn from all Adzuna rows would answer a question nobody asks.

TWO PHASES, because employer boards cannot be fetched by script. Scripted fetches of
Adzuna/employer pages were probed and 403'd (decision 2026-07-13); the browser pane is
the only sanctioned reader. So:

  1. `--sample` picks the rows deterministically and writes a worksheet with, per row,
     the Adzuna date, first_seen, and the Adzuna land URL to open. Reproducible: same DB
     state + same --seed gives the same rows.
  2. A human (or an agent driving the browser pane) opens each land URL, follows it to
     the employer's own surface, and fills in `employer_posted` + `evidence` + `ats`.
  3. `--report` reads the filled worksheet and computes the gap distribution.

Nothing here writes to jobs.db -- the connection is opened mode=ro, so a write raises.

Run:
    python tests/validation/posted_date_fidelity.py --sample -n 12
    ... fill in results/posted_date_fidelity_<stamp>.json ...
    python tests/validation/posted_date_fidelity.py --report results/posted_date_fidelity_<stamp>.json
"""
import argparse
import datetime as dt
import io
import json
import random
import sqlite3
import statistics
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "jobs.db"
RESULTS = Path(__file__).resolve().parent / "results"

from core import recency_dt  # noqa: E402  (needs BASE_DIR on the path first)

FRESH_DAYS = 14   # the standing allocation's cold-apply freshness leg
FIT_BAR = 15      # the standing allocation's cold-apply fit bar
LAND = "https://www.adzuna.com/land/ad/{ad_id}"


def _connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _ad_id(job_url):
    """The numeric id in an Adzuna details URL; the land URL is built from it."""
    tail = job_url.rstrip("/").rsplit("/", 1)[-1]
    return tail.split("?")[0]


def sample(n, seed):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT job_url, company, title, location, date_posted, first_seen,
                  fit_score, verdict, search_name
           FROM jobs
           WHERE source='adzuna' AND status='evaluated'
             AND app_status IS NULL AND filter_source IS NULL
             AND verdict='PASS' AND fit_score >= ?
             AND date_posted IS NOT NULL AND date_posted <> ''""",
        (FIT_BAR,),
    ).fetchall()
    today = dt.date.today()
    fresh = []
    for r in rows:
        eff, mode = recency_dt(r["date_posted"], r["first_seen"])
        # mode is 'posted' for every row here by construction; keep the guard anyway so a
        # future stored-shape change shows up as a smaller population, not a silent skew.
        if mode != "posted":
            continue
        age = (today - eff.date()).days
        if 0 <= age <= FRESH_DAYS:
            fresh.append((r, age))
    rng = random.Random(seed)
    rng.shuffle(fresh)
    picked = fresh[:n]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = {
        "probe": "posted_date_fidelity",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "population": {
            "definition": f"adzuna, evaluated, undecided, unfiltered, PASS, fit>={FIT_BAR}, "
                          f"recency_dt age 0..{FRESH_DAYS}d",
            "size": len(fresh),
            "sampled": len(picked),
        },
        "rows": [
            {
                "job_url": r["job_url"],
                "company": r["company"],
                "title": r["title"],
                "location": r["location"],
                "search_name": r["search_name"],
                "fit_score": r["fit_score"],
                "adzuna_date_posted": r["date_posted"],
                "first_seen": r["first_seen"],
                "adzuna_age_days": age,
                "land_url": LAND.format(ad_id=_ad_id(r["job_url"])),
                # --- fill these in from the employer's own surface ---
                "employer_posted": None,   # "YYYY-MM-DD", or ">=YYYY-MM-DD" for "30+ days ago"
                "evidence": None,          # what the surface literally said
                "ats": None,               # workday | icims | greenhouse | lever | ashby | other | dead
                "resolved_url": None,
            }
            for r, age in picked
        ],
    }
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"posted_date_fidelity_{stamp}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"population (fresh-looking, decision-relevant adzuna rows): {len(fresh)}")
    print(f"sampled {len(picked)} -> {path}")
    print()
    for row in out["rows"]:
        print("  %-28s %-42s adzuna=%s (%dd)" % (
            row["company"][:27], row["title"][:41],
            row["adzuna_date_posted"][:10], row["adzuna_age_days"]))
        print("     ", row["land_url"])
    return path


def _parse_employer(value):
    """'YYYY-MM-DD' -> (date, False); '>=YYYY-MM-DD' -> (date, True) meaning a FLOOR.

    A board that says "Posted 30+ Days Ago" gives a bound, not a date. Recording it as a
    floor keeps the gap it produces honest: the true gap is at least this, never less.
    """
    if not value:
        return None, False
    floor = value.startswith(">=")
    return dt.date.fromisoformat(value.lstrip(">=")), floor


def report(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    gaps, floors, unresolved, ats = [], 0, [], {}
    for row in data["rows"]:
        ats[row.get("ats") or "unresolved"] = ats.get(row.get("ats") or "unresolved", 0) + 1
        emp, is_floor = _parse_employer(row.get("employer_posted"))
        if emp is None:
            unresolved.append(row)
            continue
        adz = dt.date.fromisoformat(row["adzuna_date_posted"][:10])
        gap = (adz - emp).days
        gaps.append((gap, is_floor, emp, row))
        floors += 1 if is_floor else 0

    print(f"probe: {data['probe']}   created {data['created']}   seed {data['seed']}")
    print(f"population: {data['population']['size']}  sampled: {data['population']['sampled']}")
    print(f"resolved: {len(gaps)}   unresolved: {len(unresolved)}   (floors among resolved: {floors})")
    print()
    if not gaps:
        print("nothing resolved yet -- fill employer_posted in the worksheet first.")
        return
    v = sorted(g for g, _, _, _ in gaps)
    n = len(v)
    print("GAP = adzuna_date_posted - employer_posted, in days (positive = Adzuna says NEWER)")
    print("  median %.1f   mean %.1f   min %d   max %d" % (
        statistics.median(v), statistics.mean(v), v[0], v[-1]))
    for thresh in (3, 14, 30):
        k = sum(1 for g in v if g > thresh)
        print("  gap > %2dd : %2d/%d  (%.0f%%)" % (thresh, k, n, 100.0 * k / n))
    print()
    # The decision-relevant count: rows the bar admitted that the employer's own date excludes.
    busted = [(g, f, e, r) for g, f, e, r in gaps
              if (dt.date.today() - e).days > FRESH_DAYS]
    print("rows inside the <=%dd bar by Adzuna's date but OUTSIDE it by the employer's: %d/%d (%.0f%%)"
          % (FRESH_DAYS, len(busted), n, 100.0 * len(busted) / n))
    for g, f, e, r in sorted(busted, key=lambda x: -x[0]):
        true_age = (dt.date.today() - e).days
        print("   %+5d%s  %-26s %-38s adzuna %s -> employer %s (真实 %d 天)" % (
            g, ">" if f else " ", r["company"][:25], r["title"][:37],
            r["adzuna_date_posted"][:10], r["employer_posted"], true_age))
    print()
    print("ATS providers seen (free byproduct -- feeds the 'is a Workday/iCIMS fetcher worth it' question):")
    for k, c in sorted(ats.items(), key=lambda kv: -kv[1]):
        print("   %-12s %d" % (k, c))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="draw a worksheet")
    ap.add_argument("-n", type=int, default=12, help="sample size (default 12)")
    ap.add_argument("--seed", type=int, default=20260820, help="sampling seed")
    ap.add_argument("--report", metavar="WORKSHEET", help="summarize a filled worksheet")
    args = ap.parse_args()
    if args.sample:
        sample(args.n, args.seed)
    elif args.report:
        p = Path(args.report)
        report(p if p.is_absolute() else Path(__file__).resolve().parent / p)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
