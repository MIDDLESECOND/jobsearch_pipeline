#!/usr/bin/env python3
"""Can the Adzuna API tell us an ad is dead? Measured 2026-08-20: NO. Do not rebuild this.

Context. The 2026-07-13 decision rejected automatic liveness verification on three
premises -- scripted page fetches 403, ~19k rows/14d, and "it's an apply-time problem".
By 2026-08-20 two of those had shifted: the decision-relevant population is ~302 rows
(adzuna, evaluated, undecided, PASS, fit>=15, <=14d), not 19k, and the deepdive batch now
pays the cost BEFORE apply-time (measured the same day: 8 of 51 browser completions in one
batch landed on dead pages, and 5 of 12 in a random sample of that 302-row population).
That reopened exactly one question, which this probe closes:

    api.adzuna.com is keyed, sanctioned and free -- the 403 finding was about scraping
    adzuna.com WEB pages, not the API. Can the API decide liveness cheaply?

Two mechanisms existed. Both are now closed.

MECHANISM 1 -- get-by-id. There is none. /v1/api/jobs/us/{get,job,details}/<id> all
return 404; the API surface is the search endpoint `_adzuna_search` already uses.

MECHANISM 2 -- absence from a reproducible search. Tested against a hand-labelled set of
12 ads whose live/dead state was confirmed the same hour by opening each Adzuna details
page in the browser (4 dead: the page reads "this job is no longer available"; 8 alive).
Query per ad: what_phrase=<exact stored title>, where=<first comma-field of the stored
location>, max_days_old=30, results_per_page=50, sort_by=date -- i.e. the fetcher's own
call shape, widened.

    prediction "absent from search" => dead        dead(4)   alive(8)
      absent                                         4         4      <-- 4 FALSE DEAD
      present                                        0         4

    4/4 dead correctly absent, and 4/8 LIVE ads also absent. Causes, one per row:
      Cooley Godward Kronish  where="Hayes Valley"  count=0    -- Adzuna's own stored
      ARK Infotech Spectrum   where="Pineridge"     count=0       location strings are
                                                                  neighbourhoods, not
                                                                  searchable `where` values
      Goodwin Procter         where="US"            count=5301 -- ad sits past page 1 of 107
      Public Consulting Group where="Glendale"      count=3    -- exact-phrase title miss

Why this is not fixable by a better query. Dropping `where` and paging until found turns
one liveness check into a crawl whose cost scales with the result set -- 107 pages for the
Goodwin ad alone, times a 302-row zone. And the structural defect survives any amount of
paging: absence is evidence only if the search was EXHAUSTIVE, and exhaustive is most
expensive precisely for the ads that are hardest to retrieve. You can never separate
"absent because dead" from "absent because I stopped".

Why the error direction settles it even if the rate improved. A false dead HIDES A LIVE
ROLE. The standing position for every discovery surface here is 过量呈现 + 排序好 -- never
an automatic filter that can bury a real opportunity (same rule that keeps needs_attention
and dupe_candidates presentation-only). A 50% false-dead rate is disqualifying; so would
10% be.

What was done instead: the deepdive batch already opens every posting page, so it already
observes deadness for free. That observation is now RECORDED rather than discarded -- a
required dead-link section in the batch report, a `dead_links` odometer in
.deepdive_state.json, and `expired` proposed to the user (never auto-run: it marks the
chain passed, which is a decision). The fix and the measurement are the same action.

This script re-runs the labelled test. It exists so the next person to ask "why don't we
just check Adzuna?" gets a number instead of an argument. Re-labelling is manual: the
ground truth comes from opening each details page in the browser pane.

Run:  python tests/validation/adzuna_liveness_probe.py
"""
import io
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "jobs.db"

from core import _ensure_api_key  # noqa: E402

# Ground truth, 2026-08-20: each label set by opening https://www.adzuna.com/details/<id>
# in the browser pane and reading whether it said "this job is no longer available".
LABELLED = {
    "5831485915": "dead",   # J.E. Dunn Construction — AI Solutions Data Analyst
    "5830498029": "dead",   # Joseph Michaels International — Legal Innovation Technology Analyst
    "5839129000": "dead",   # Parsons Corporation — Power Platform Developer
    "5835904974": "dead",   # State of Maine — Senior Programmer Analyst
    "5849613326": "alive",  # Cooley Godward Kronish — Collaboration Technology and Workflow Engineer
    "5831477781": "alive",  # Humana — Senior AI Implementation Lead
    "5834636749": "alive",  # Compass Behavioral Group — AI Implementation & Adoption Specialist
    "5844850041": "alive",  # Public Consulting Group — Power Platform Engineer
    "5834607184": "alive",  # Goodwin Procter — Automation Engineer (Adzuna alive; employer req gone)
    "5830294920": "alive",  # Town of Greenwich — I.T. Automation Specialist
    "5831651257": "alive",  # Crisp Recruit — Technology Applications Manager
    "5849806036": "alive",  # ARK Infotech Spectrum — Azure Integration
}
SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"


def _search(app_id, app_key, title, where, days=30, rpp=50):
    """The fetcher's own call shape (`fetch._adzuna_search`), widened to 30 days.

    app_id/app_key ride in the query string because that is Adzuna's auth requirement --
    so the built URL must never be printed or logged (same rule as fetch.py's note).
    """
    params = {
        "app_id": app_id, "app_key": app_key,
        "results_per_page": rpp, "max_days_old": days,
        "sort_by": "date", "content-type": "application/json",
        "what_phrase": title,
    }
    if where:
        params["where"] = where
    with urllib.request.urlopen(
            SEARCH_URL + "?" + urllib.parse.urlencode(params), timeout=30) as resp:
        return json.load(resp)


def main():
    app_id = _ensure_api_key("ADZUNA_APP_ID", label="adzuna")
    app_key = _ensure_api_key("ADZUNA_APP_KEY", label="adzuna")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("%-11s %-6s %-9s %-6s %-7s %-24s %s"
          % ("id", "label", "present?", "ret", "count", "company", "where"))
    matrix = {("dead", True): 0, ("dead", False): 0, ("alive", True): 0, ("alive", False): 0}
    for ad, label in LABELLED.items():
        row = conn.execute(
            "SELECT company, title, location FROM jobs WHERE job_url LIKE '%'||?||'%'",
            (ad,)).fetchone()
        if row is None:
            print("%-11s %-6s  (not in this DB -- skipped)" % (ad, label))
            continue
        where = (row["location"] or "").split(",")[0].strip()
        try:
            payload = _search(app_id, app_key, row["title"], where)
            ids = [str(r.get("id")) for r in payload.get("results", [])]
            present, ret, count = ad in ids, len(ids), payload.get("count")
        except Exception as exc:                      # noqa: BLE001 - probe, report and move on
            print("%-11s %-6s  ERROR %s" % (ad, label, type(exc).__name__))
            continue
        matrix[(label, present)] += 1
        print("%-11s %-6s %-9s %-6d %-7s %-24s %s"
              % (ad, label, present, ret, count, (row["company"] or "")[:23], where))
        time.sleep(0.4)                               # be polite to a free API

    dead_absent, dead_present = matrix[("dead", False)], matrix[("dead", True)]
    alive_present, alive_absent = matrix[("alive", True)], matrix[("alive", False)]
    print()
    print('prediction "absent from search" => dead')
    print("  dead  & absent  (correct kill) : %d" % dead_absent)
    print("  dead  & present (missed)       : %d" % dead_present)
    print("  alive & present (correct keep) : %d" % alive_present)
    print("  alive & absent  (FALSE DEAD)   : %d   <-- the disqualifying cell" % alive_absent)
    total_alive = alive_present + alive_absent
    if total_alive:
        print()
        print("false-dead rate: %d/%d = %.0f%%.  A false dead hides a live role, which is the"
              % (alive_absent, total_alive, 100.0 * alive_absent / total_alive))
        print("one failure this repo's discovery surfaces are not allowed to have. See docstring.")


if __name__ == "__main__":
    main()
