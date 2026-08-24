#!/usr/bin/env python3
"""Old vs new containment operator for the re-apply guard's A2 cross-source branch.

Why this exists. The A2 branch decides "are these two rows the same requisition" by
normalizing both descriptions and testing containment. The operator shipped in
.claude/skills/deepdive/SKILL.md anchors the probe at the HEAD of the shorter string:

    probe = short[:-20] if len(short) < 600 else short      # OLD

That survives a prefix on the LONG side — the PTG/Courser case it was built for, where the
stored LinkedIn text opens with a `**Brief Description**` header the Adzuna feed drops — but
it cannot survive a prefix on the SHORT side. Measured 2026-08-20 on live data:

    Bertelsmann | Power Platform Developer   (Louisville, adzuna, undecided)
    Arvato      | Power Platform Developer   (Louisville, linkedin, APPLIED 2026-07-07)

are one requisition (Arvato is Bertelsmann's subsidiary; the Adzuna copy prepends
"Company Description "). All four guard tiers returned None. A missed A2 is not a cosmetic
gap — the failure mode it exists to prevent is applying twice to the same job.

The proposed operator takes the probe from the MIDDLE of the shorter string. Mid-text
should also be MORE specific than head-text, because the head is where boilerplate lives
("about us", "company description", the firm blurb) — which is exactly the material that
produced this branch's known false pairs. That is a claim, so this script measures it
instead of asserting it.

Two arms, and the second is the one that can veto the change:

  1. PINNED PAIRS — operator-level, on real stored text pulled by url. Immune to whether the
     rows have since been dupe-linked (a linked pair leaves the candidate set entirely — the
     "A=9 -> A=0 after linking" effect the skill already documents), which is why this arm
     tests the operator directly rather than driving the whole tier flow.
  2. POPULATION SWEEP — run both operators over the live candidate set and print
     old-only / new-only / both. EVERY new-only hit must be hand-adjudicated: a new hit that
     is not the same requisition is a regression, because acting on it propagates `applied`
     across unrelated chains. A bigger number here is not a better result.

Read-only (sqlite mode=ro — any write raises).

Run:  python tests/validation/reapply_probe_compare.py
      python tests/validation/reapply_probe_compare.py --limit 400   # smaller sweep
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "jobs.db"

MIN_CONTAIN = 300          # normalized chars; below this a "containment" is boilerplate
CUTOFF_DAYS = 60           # the guard's applied-history window


def norm_text(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def probe_old(short):
    """Head-anchored — the operator currently in SKILL.md."""
    return short[:-20] if len(short) < 600 else short


def probe_new(short):
    """Mid-anchored. Size scales with the text (quarter-length) with a 60-char floor and a
    150-char cap, so a 480-char Adzuna snippet probes ~240 mid chars and a 300-char floor
    case still probes 150 — always well above the noise level, never the boilerplate head."""
    half = max(60, min(150, len(short) // 4))
    mid = len(short) // 2
    return short[mid - half:mid + half]


def contains(mk, a_norm, b_norm):
    short, long_ = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    p = mk(short)
    return bool(p) and p in long_


# --- arm 1: pinned pairs -------------------------------------------------------------
# (label, url-substring A, url-substring B, expected). Ground truth set by reading both
# stored JDs on 2026-08-20; the first is the live miss this change exists for.
PINNED = [
    ("Bertelsmann / Arvato (APPLIED 07-07)", "5848187645", "4434569091", True),
    ("Cooley Godward Kronish / Cooley LLP", "5849613326", "5847202493", True),
    ("Constellation / Exelon Generation", "5847653888", "4452535069", True),
]


def run_pinned(conn):
    print("=== arm 1: pinned pairs (operator-level) ===")
    print("%-38s %-9s %-9s %s" % ("pair", "OLD", "NEW", "expected"))
    ok = True
    for label, a, b, expect in PINNED:
        ra = conn.execute("SELECT description FROM jobs WHERE job_url LIKE '%'||?||'%'", (a,)).fetchone()
        rb = conn.execute("SELECT description FROM jobs WHERE job_url LIKE '%'||?||'%'", (b,)).fetchone()
        if not ra or not rb:
            print("%-38s  (a row is not in this DB — skipped)" % label[:37])
            continue
        na, nb = norm_text(ra["description"]), norm_text(rb["description"])
        if min(len(na), len(nb)) < MIN_CONTAIN:
            print("%-38s  (below MIN_CONTAIN — branch would not run)" % label[:37])
            continue
        o, n = contains(probe_old, na, nb), contains(probe_new, na, nb)
        flag = "" if n == expect else "   <<< NEW FAILS THE PINNED EXPECTATION"
        if n != expect:
            ok = False
        print("%-38s %-9s %-9s %s%s" % (label[:37], o, n, expect, flag))
    print()
    return ok


# --- arm 2: population sweep ---------------------------------------------------------
def run_sweep(conn, limit):
    print("=== arm 2: population sweep (old vs new over the live candidate set) ===")
    applied = conn.execute(f"""
        SELECT company, title, norm_company, norm_title, status_date, description,
               COALESCE(repost_of, job_url) AS root
        FROM jobs
        WHERE app_status='applied' AND status_date >= date('now','localtime','-{CUTOFF_DAYS} day')
          AND length(description) > 200
          AND norm_title IS NOT NULL AND norm_title <> ''
          AND norm_company IS NOT NULL AND norm_company <> ''
        ORDER BY status_date DESC""").fetchall()
    by_title = {}
    for a in applied:
        by_title.setdefault(a["norm_title"], []).append((a, norm_text(a["description"])))
    print("applied set: %d rows / %d titles (%d-day window)"
          % (len(applied), len(by_title), CUTOFF_DAYS))

    cands = conn.execute("""
        SELECT job_url, company, title, norm_company, norm_title, description,
               COALESCE(repost_of, job_url) AS root
        FROM jobs
        WHERE status='evaluated' AND filter_source IS NULL AND app_status IS NULL
          AND length(description) > 200
          AND norm_title IS NOT NULL AND norm_title <> ''
        ORDER BY first_seen DESC LIMIT ?""", (limit,)).fetchall()
    print("candidates swept: %d (newest first)" % len(cands))
    print()

    old_only, new_only, both = [], [], []
    for c in cands:
        nc = norm_text(c["description"])
        if len(nc) < MIN_CONTAIN:
            continue
        for a, na in by_title.get(c["norm_title"], ()):
            # Same title required; same company is tier A's job. Same root is self-match.
            if a["root"] == c["root"] or a["norm_company"] == c["norm_company"]:
                continue
            o, n = contains(probe_old, nc, na), contains(probe_new, nc, na)
            if o and n:
                both.append((c, a))
            elif o:
                old_only.append((c, a))
            elif n:
                new_only.append((c, a))
            if o or n:
                break

    print("both operators agree (hit) : %d" % len(both))
    print("OLD only  (new operator REGRESSES here — investigate) : %d" % len(old_only))
    print("NEW only  (the delta — EVERY ONE needs hand-adjudication) : %d" % len(new_only))
    print()
    for label, rows in (("OLD-ONLY", old_only), ("NEW-ONLY", new_only), ("BOTH", both)):
        if not rows:
            continue
        print("--- %s ---" % label)
        for c, a in rows[:40]:
            print("  %-26s %-38s" % ((c["company"] or "")[:25], (c["title"] or "")[:37]))
            print("     vs APPLIED %-24s %-34s %s"
                  % ((a["company"] or "")[:23], (a["title"] or "")[:33], a["status_date"]))
            print("     %s" % c["job_url"])
        if len(rows) > 40:
            print("  ... %d more" % (len(rows) - 40))
        print()
    return old_only, new_only, both


def run_control(conn, limit):
    """POSITIVE CONTROL. The sweep's normal filters (undecided rows only) legitimately
    exclude every pair we already know about — a linked pair leaves the candidate set, and a
    `passed` one fails `app_status IS NULL`. So a clean sweep and a silently-broken sweep
    both print zero, and after 2026-08-20 that is not a distinction this repo gets to skip:
    two verifications that day reported success on total failures because nothing in their
    output could have looked different if they were broken.

    This arm re-admits decided rows and asserts the machinery can still find SOMETHING. A
    zero here means the sweep itself is broken, not that the population is clean.
    """
    applied = conn.execute(f"""
        SELECT company, title, norm_company, norm_title, status_date, description,
               COALESCE(repost_of, job_url) AS root
        FROM jobs
        WHERE app_status='applied' AND status_date >= date('now','localtime','-{CUTOFF_DAYS} day')
          AND length(description) > 200 AND norm_title <> '' AND norm_company <> ''""").fetchall()
    by_title = {}
    for a in applied:
        by_title.setdefault(a["norm_title"], []).append((a, norm_text(a["description"])))
    cands = conn.execute("""
        SELECT job_url, company, title, norm_company, norm_title, description,
               COALESCE(repost_of, job_url) AS root
        FROM jobs
        WHERE status='evaluated' AND length(description) > 200 AND norm_title <> ''
        ORDER BY first_seen DESC LIMIT ?""", (limit,)).fetchall()
    hits = []
    for c in cands:
        nc = norm_text(c["description"])
        if len(nc) < MIN_CONTAIN:
            continue
        for a, na in by_title.get(c["norm_title"], ()):
            if a["root"] == c["root"] or a["norm_company"] == c["norm_company"]:
                continue
            if contains(probe_new, nc, na):
                hits.append((c, a))
                break
    print("=== arm 3: positive control (decided rows re-admitted, %d candidates) ===" % len(cands))
    print("machinery found %d pair(s) — a ZERO here means the sweep is broken, not clean" % len(hits))
    for c, a in hits[:8]:
        print("   %-26s %-34s  vs APPLIED %-22s %s"
              % ((c["company"] or "")[:25], (c["title"] or "")[:33],
                 (a["company"] or "")[:21], a["status_date"]))
    print()
    return len(hits)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=3000, help="candidate rows to sweep")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pinned_ok = run_pinned(conn)
    old_only, new_only, _ = run_sweep(conn, args.limit)
    control = run_control(conn, max(args.limit, 8000))
    print("=" * 72)
    print("VERDICT INPUTS (this script does not decide — you do):")
    print("  pinned expectations met by NEW : %s" % ("yes" if pinned_ok else "NO"))
    print("  regressions (old-only)         : %d  — must be 0, or the new probe lost a real pair"
          % len(old_only))
    print("  new-only hits to adjudicate    : %d  — each must be the SAME requisition"
          % len(new_only))
    print("  positive control found         : %d  — if this is 0 the sweep above means nothing"
          % control)


if __name__ == "__main__":
    main()
