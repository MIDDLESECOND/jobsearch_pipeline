#!/usr/bin/env python3
"""What does the ATS lane actually buy — unseen roles, or earlier ones?

The lane was bought on two claims: it surfaces requisitions the aggregators never carry,
and it surfaces the shared ones sooner. Both are measurable against rows already stored,
so this needs no run and no spend.

**"No advantage" is a permitted answer, and the script must be able to print it.** That is
the whole point of measuring rather than celebrating: a board that only re-reports what
LinkedIn already had, later, is a board worth removing.

Method: an ATS row is matched to non-ATS rows by the SAME normalized company+title the
dedup fingerprint uses (`chain._norm_company` / `chain._norm_title`), deliberately WITHOUT
location — cross-source location strings rarely agree (AGENTS.md's own caveat on the
within-source fingerprint), and including it would score the lane's coverage as artificially
unique. Dropping it is the conservative direction here: it can only make the lane look
WORSE at unique coverage, never better.

Earliness is `first_seen` (discovery), not `date_posted` — the question is when the pipeline
could have acted, and `date_posted` is exactly the field the 2026-08-20 evergreen work showed
is unreliable across sources.

Read-only (sqlite mode=ro — any write raises). No network.

Run:  python tests/validation/ats_lane_yield.py
"""
import collections
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

from chain import _norm_company as norm_company, _norm_title as norm_title  # noqa: E402
from core import parse_iso  # noqa: E402

ATS_SOURCES = ("greenhouse", "lever", "ashby", "workday")


def _key(company, title):
    return (norm_company(company or ""), norm_title(title or ""))


def _seen(value):
    parsed = parse_iso(value)
    return parsed[0] if parsed else None


def main():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT job_url, company, title, source, first_seen, fit_score, verdict, status "
        "FROM jobs WHERE company IS NOT NULL AND title IS NOT NULL").fetchall()

    other = collections.defaultdict(list)
    ats = []
    for r in rows:
        if r["source"] in ATS_SOURCES:
            ats.append(r)
        else:
            other[_key(r["company"], r["title"])].append(r)

    if not ats:
        print("No ATS rows in this DB — the lane has never run. Nothing to measure.")
        return

    by_source = collections.Counter(r["source"] for r in ats)
    print("=== the lane's footprint ===")
    for src, n in by_source.most_common():
        print("  %-12s %5d rows" % (src, n))
    print("  %-12s %5d rows total, %d distinct company+title keys"
          % ("ALL", len(ats), len({_key(r["company"], r["title"]) for r in ats})))
    print()

    unique, shared, earlier, later, same = [], [], [], [], []
    leads = []
    for r in ats:
        peers = other.get(_key(r["company"], r["title"]))
        if not peers:
            unique.append(r)
            continue
        shared.append(r)
        mine, theirs = _seen(r["first_seen"]), [_seen(p["first_seen"]) for p in peers]
        theirs = [t for t in theirs if t]
        if not mine or not theirs:
            continue
        lead = (min(theirs) - mine).total_seconds() / 86400.0
        leads.append(lead)
        (earlier if lead > 0.5 else later if lead < -0.5 else same).append((lead, r))

    print("=== claim 1: coverage — roles no aggregator carried ===")
    print("  only on the ATS board : %5d / %d rows (%.0f%%)"
          % (len(unique), len(ats), 100.0 * len(unique) / len(ats)))
    print("  also seen elsewhere   : %5d / %d rows (%.0f%%)"
          % (len(shared), len(ats), 100.0 * len(shared) / len(ats)))
    scored = [r for r in unique if (r["fit_score"] or 0) >= 15 and r["verdict"] == "PASS"]
    print("  ...of the unique ones, %d scored PASS at fit>=15 (the cold-apply bar)" % len(scored))
    for r in sorted(scored, key=lambda x: -(x["fit_score"] or 0))[:8]:
        print("       fit %-4s %-26s %-42s %s"
              % (r["fit_score"], (r["company"] or "")[:25], (r["title"] or "")[:41], r["source"]))
    print()

    print("=== claim 2: earliness — on the SHARED roles, who saw it first ===")
    if not leads:
        print("  no shared role has usable first_seen on both sides — earliness unmeasurable.")
    else:
        print("  ATS earlier : %4d   (median lead %.1f days)"
              % (len(earlier), statistics.median([l for l, _ in earlier]) if earlier else 0))
        print("  same day    : %4d" % len(same))
        print("  ATS later   : %4d   (median lag  %.1f days)"
              % (len(later), abs(statistics.median([l for l, _ in later])) if later else 0))
        print("  overall median lead: %+.1f days over %d comparable rows"
              % (statistics.median(leads), len(leads)))
        if earlier:
            print("  biggest leads:")
            for lead, r in sorted(earlier, key=lambda x: -x[0])[:6]:
                print("       %+5.0fd  %-26s %-42s" % (lead, (r["company"] or "")[:25],
                                                       (r["title"] or "")[:41]))
    print()
    print("Read both claims together. High unique coverage with a negative median lead means")
    print("the lane is a DISCOVERY channel, not a speed one; the reverse means it is a speed")
    print("channel and the boards duplicate the aggregators. Either can justify the lane —")
    print("neither is assumed.")


if __name__ == "__main__":
    main()
