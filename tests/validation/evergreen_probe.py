#!/usr/bin/env python3
"""How often does a "fresh" row belong to a requisition that has been open for months?

An aggregator can re-stamp a long-open posting with a new date. Verified against the
employers' own systems on 2026-08-20, on two rows that had already been analysed and
recommended that same day:

    PNM Gen AI Technologist Lead   Adzuna 2026-08-19  vs careers.txnmenergy.com "Jun 3"  = 78d
    WSGR Innovations & AI Sol Eng  Adzuna 2026-08-18  vs Workday startDate 2026-06-03    = 76d

`core.recency_dt` reads Adzuna in mode='posted' and TRUSTS it, so the standing allocation's
`<= 14 days` cold-apply leg ran against numbers eleven weeks wrong.

This measures the network-free detector that ships in `report.evergreen_floors`: rows whose
stored `description` is byte-identical but whose `date_posted` differs are one requisition
re-stamped, so the earliest date in that group is a floor on the requisition's true age.

Arm 2 is the point of the script. A detector that only prints its hits is a detector you
cannot size, so this one re-runs the two employer-verified cases and prints **whether it
catches them** — including the one it misses. Read that table before quoting any rate from
arm 1.

Read-only (sqlite mode=ro — any write raises). No network.

Run:  python tests/validation/evergreen_probe.py
"""
import collections
import datetime as dt
import hashlib
import io
import sqlite3
import statistics
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "jobs.db"

from core import parse_iso, recency_dt  # noqa: E402

FRESH_DAYS = 14   # the standing allocation's cold-apply freshness leg
FIT_BAR = 15      # its fit bar

# Ground truth, 2026-08-20, each read off the EMPLOYER's own system in a browser.
GROUND_TRUTH = [
    ("PNM Gen AI Technologist Lead", "5848204893", dt.date(2026, 6, 3),
     "careers.txnmenergy.com — 'Date Posted: Jun 3, 2026'"),
    ("WSGR Innovations & AI Solutions Eng", "5847669175", dt.date(2026, 6, 3),
     "Workday CXS jobPostingInfo.startDate = 2026-06-03"),
]


def _day(value):
    parsed = parse_iso(value)
    return parsed[0].date() if parsed else None


def build_floors(conn):
    floors = {}
    for desc, dp in conn.execute(
        "SELECT description, date_posted FROM jobs "
        "WHERE description IS NOT NULL AND length(description) > 200 "
        "AND date_posted IS NOT NULL AND date_posted <> ''"
    ):
        day = _day(dp)
        if not day:
            continue
        key = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        if key not in floors or day < floors[key]:
            floors[key] = day
    return floors


def arm1_population(conn, floors):
    print("=== arm 1: how common is re-stamping, and what does it cost the bar ===")
    groups = collections.defaultdict(list)
    for desc, dp in conn.execute(
        "SELECT description, date_posted FROM jobs WHERE source='adzuna' "
        "AND description IS NOT NULL AND length(description) > 200 "
        "AND date_posted IS NOT NULL AND date_posted <> ''"
    ):
        day = _day(dp)
        if day:
            groups[hashlib.sha256(desc.encode("utf-8")).hexdigest()].append(day)
    spans = sorted((max(v) - min(v)).days for v in groups.values()
                   if len(v) > 1 and (max(v) - min(v)).days > 0)
    print("identical-JD adzuna groups with a nonzero date spread: %d" % len(spans))
    if spans:
        print("  span days: median %.0f  max %d   (max is censored by how far back the "
              "corpus goes, not by the effect)" % (statistics.median(spans), spans[-1]))
        for thr in (14, 30, 60):
            k = sum(1 for s in spans if s > thr)
            print("    span > %2dd : %4d groups (%.0f%%)" % (thr, k, 100.0 * k / len(spans)))

    today = dt.date.today()
    rows = conn.execute(
        """SELECT job_url, company, title, date_posted, first_seen, fit_score, description
           FROM jobs
           WHERE status='evaluated' AND filter_source IS NULL AND app_status IS NULL
             AND verdict='PASS' AND fit_score >= ?
             AND description IS NOT NULL AND length(description) > 200""", (FIT_BAR,)).fetchall()
    in_band, busted, in_h, bust_h, worst = 0, 0, set(), set(), []
    for r in rows:
        eff, mode = recency_dt(r["date_posted"], r["first_seen"])
        if mode is None:
            continue
        age = (today - eff.date()).days
        if not (0 <= age <= FRESH_DAYS):
            continue
        key = hashlib.sha256(r["description"].encode("utf-8")).hexdigest()
        in_band += 1
        in_h.add(key)
        floor = floors.get(key)
        if floor and (today - floor).days > FRESH_DAYS:
            busted += 1
            bust_h.add(key)
            worst.append(((today - floor).days, age, r))
    print()
    print("zone rows reading <=%dd through recency_dt : %d rows / %d distinct JD texts"
          % (FRESH_DAYS, in_band, len(in_h)))
    print("...provably older via an identical JD dated earlier: %d rows / %d texts"
          % (busted, len(bust_h)))
    if in_band and in_h:
        print("=> per-ROW %.0f%%   per-REQUISITION %.0f%%   (quote both; one requisition"
              " mass-posted across cities inflates the row figure)"
              % (100.0 * busted / in_band, 100.0 * len(bust_h) / len(in_h)))
    print()
    print("worst 8 (floor age  vs  the age the bar currently sees):")
    for true_age, age, r in sorted(worst, key=lambda x: -x[0])[:8]:
        print("  >=%3dd true / %3dd seen   %-26s %-38s fit %s"
              % (true_age, age, (r["company"] or "")[:25], (r["title"] or "")[:37],
                 r["fit_score"]))
    print()


def arm2_ground_truth(conn, floors):
    print("=== arm 2: does the detector catch the cases we verified by hand? ===")
    print("%-38s %-12s %-12s %-10s %s"
          % ("case", "stored", "floor", "employer", "verdict"))
    for label, sub, truth, source in GROUND_TRUTH:
        r = conn.execute(
            "SELECT date_posted, description FROM jobs WHERE job_url LIKE '%'||?||'%'",
            (sub,)).fetchone()
        if not r:
            print("%-38s (not in this DB)" % label[:37])
            continue
        key = hashlib.sha256((r["description"] or "").encode("utf-8")).hexdigest()
        floor = floors.get(key)
        stored = (r["date_posted"] or "")[:10]
        if floor is None or floor >= _day(r["date_posted"] or ""):
            verdict = "MISS — its text appears once, so there is no floor to find"
            shown = "-"
        elif floor > truth:
            verdict = "HIT, but only to a FLOOR (%d days short of the truth)" % (floor - truth).days
            shown = floor.isoformat()
        else:
            verdict = "HIT, at or past the employer's date"
            shown = floor.isoformat()
        print("%-38s %-12s %-12s %-10s %s"
              % (label[:37], stored, shown, truth.isoformat(), verdict))
        print("%-38s   employer source: %s" % ("", source))
    print()
    print("This detector is a ONE-DIRECTIONAL LOWER BOUND: it can only reveal a row as older,")
    print("never fresher, and it under-reports. That is why report.posting_age APPENDS the")
    print("floor instead of substituting it, and why nothing filters on it — a bound must")
    print("never be allowed to hide a live role.")


def main():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    floors = build_floors(conn)
    arm1_population(conn, floors)
    arm2_ground_truth(conn, floors)


if __name__ == "__main__":
    main()
