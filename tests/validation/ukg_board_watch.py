#!/usr/bin/env python3
"""Report-only watcher over the six law firms on UKG/UltiPro boards.

Why this exists instead of a `fetch_ats` reader (probed 2026-08-21): UKG's
`JobBoardView/LoadSearchResults` is a clean public JSON list — real `PostedDate`
timestamps, `RequisitionNumber` (requisition identity), city lists, and it honors an
`OpportunityIds` filter — but the FULL job description has no reachable public endpoint:
the detail page is an SPA shell whose data call never fires outside a real session, four
endpoint batteries and a 12MB bundle-mining pass found no candidate-side data route, and
the apply flow redirects to login (which this repo never does). `BriefDescription` is a
single sentence (~150 chars, measured) — feeding that to the evaluator is the snippet-
inflation failure measured on 2026-08-20, only worse. So no rows are inserted; this
script only REPORTS, in the `outlook_shadow` shape: probe, compare read-only, write a
gitignored-adjacent results file, never mutate.

What to do with a finding: open the detail URL in a browser, and if the role is real,
run manual intake (the sanctioned path — same salary floor, normal eval next run). The
`lawboard-sweep` skill runs this script first so the browser only opens for hits.

Title scope: the SAME vocabulary the ATS lane's law boards use — shared
`settings.ats.title_any` UNION the config's `lawfirm_ai_titles` anchor — read from
config.yaml at runtime so the two can't drift.

Read-only (sqlite mode=ro). Politely paced. Run:
    python tests/validation/ukg_board_watch.py
"""
import datetime as dt
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import load_config          # noqa: E402
from filters import _pattern_matches  # noqa: E402
from chain import _norm_company, _norm_title  # noqa: E402

DB_PATH = BASE_DIR / "jobs.db"
OUT_DIR = Path(__file__).resolve().parent / "results"

# host prefix / tenant / board guid — read off each firm's own careers page 2026-08-21
# (the coordinates are not guessable; recruiting vs recruiting2 varies per tenant).
BOARDS = {
    "Stinson":           ("recruiting2", "STI1009STIS", "162b1480-f2fa-4444-8ad7-f750cb200b46"),
    "Husch Blackwell":   ("recruiting2", "HUS1001HUSCH", "b6637591-8c8d-49b8-b093-5524b203b157"),
    "Akerman":           ("recruiting",  "AKE1000ASEPA", "b855fc7e-c6e0-90cc-b829-ddbebeb6f274"),
    "Fish & Richardson": ("recruiting",  "FIS1004",      "2ae7bc70-49c5-7a57-eda1-bf2535cb53bc"),
    "GrayRobinson":      ("recruiting",  "GRA1010GRAY",  "8d18afb5-245e-4c7e-a198-6a682201a90e"),
    "Potter Anderson":   ("recruiting",  "POT1003PACL",  "d4cef977-dbf6-4f34-b8f2-46aa29f8029d"),
}
DELAY = 1.0
PAGE = 50


def _board_url(host, tenant, guid):
    return f"https://{host}.ultipro.com/{tenant}/JobBoard/{guid}"


def _search(board_url, skip):
    payload = {
        "opportunitySearch": {
            "Top": PAGE, "Skip": skip, "QueryString": "",
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                         "Ascending": False}],
            "Filters": [],
        },
        "matchCriteria": {"PreferredJobs": [], "Educations": [],
                          "LicenseAndCertifications": [], "Skills": [],
                          "hasNoLicenses": False, "SkippedSkills": []},
    }
    req = urllib.request.Request(
        board_url + "/JobBoardView/LoadSearchResults",
        data=json.dumps(payload).encode(), method="POST",
        headers={"User-Agent": "Mozilla/5.0 (jobsearch-pipeline)",
                 "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _vocabulary(cfg):
    ats = cfg["settings"]["ats"]
    shared = [p for p in (ats.get("title_any") or []) if isinstance(p, str)]
    extra = []
    for comp in ats.get("companies") or []:
        if isinstance(comp, dict) and comp.get("title_any_extra"):
            extra = [p for p in comp["title_any_extra"] if isinstance(p, str)]
            break
    return shared + [p for p in extra if p not in shared]


def main():
    vocab = _vocabulary(load_config())
    assert any("ai" in p.lower() for p in vocab), "law vocabulary missing from config"
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    known = {( _norm_company(c or ""), _norm_title(t or "") )
             for c, t in conn.execute("SELECT company, title FROM jobs")}

    today = dt.date.today().isoformat()
    lines = [f"# UKG board watch — {today}", "",
             "Report-only (module docstring: why there is no fetch_ats reader). A NEW row",
             "means: open the URL, judge it, and if real run manual intake.", ""]
    total_seen = total_scope = total_new = 0
    for firm, (host, tenant, guid) in BOARDS.items():
        burl = _board_url(host, tenant, guid)
        opps, skip = [], 0
        try:
            while True:
                d = _search(burl, skip)
                batch = d.get("opportunities") or []
                opps.extend(batch)
                skip += PAGE
                if len(batch) < PAGE or skip >= (d.get("totalCount") or 0):
                    break
                time.sleep(DELAY)
        except Exception as exc:
            print(f"  {firm:<20} FAILED {type(exc).__name__}: {exc}")
            lines.append(f"## {firm} — FAILED ({type(exc).__name__})")
            continue
        in_scope = [o for o in opps
                    if any(_pattern_matches(p, o.get("Title") or "") for p in vocab)]
        total_seen += len(opps); total_scope += len(in_scope)
        rows = []
        for o in in_scope:
            title = o.get("Title") or ""
            fresh = (_norm_company(firm), _norm_title(title)) not in known
            total_new += fresh
            locs = ", ".join(
                ((l.get("Address") or {}).get("City") or l.get("LocalizedName") or "")
                for l in (o.get("Locations") or [])[:3] if isinstance(l, dict))
            url = f"{burl}/OpportunityDetail?opportunityId={o.get('Id')}"
            rows.append((fresh, title, o.get("PostedDate") or "",
                         o.get("RequisitionNumber") or "", locs, url))
        print("  %-20s board=%-3d in-scope=%-2d new=%d"
              % (firm, len(opps), len(in_scope), sum(1 for r in rows if r[0])))
        lines.append(f"## {firm} — {len(opps)} listed, {len(in_scope)} in scope")
        lines.append("")
        for fresh, title, posted, reqno, locs, url in rows:
            tag = "**NEW**" if fresh else "known in DB"
            lines.append(f"- {tag} · {title} · {locs} · posted {posted[:10]} · req {reqno}")
            lines.append(f"  {url}")
        lines.append("")
        time.sleep(DELAY)

    lines.insert(3, f"odometer: {len(BOARDS)} boards · {total_seen} roles listed · "
                    f"{total_scope} in scope · {total_new} NEW vs jobs.db")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"ukg_watch_{today.replace('-', '')}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
