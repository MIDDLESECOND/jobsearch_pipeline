"""Adzuna overlap probe — would extra `what_phrase` queries surface roles the current sources miss?

The question this answers, and only this one: the ATS side's `title_any` opts into the AI
engineer/architect title family (`re:\\bai (engineer|architect)\\b`, `re:applied ai\\b`, added
2026-08-13), and the Adzuna side has no equivalent phrase. Two live ads surfaced through the
Outlook alert channel on 2026-08-18 (Veeco "AI Applications Engineer 2", Axon "AI Infrastructure
Engineer") sat inside the id range the pipeline was actively fetching that day and still never
entered the DB, because no configured phrase matches either title. Before buying a permanent
paid-eval lane for that family, measure it — the same bar Dice had to clear (dice_overlap_probe
measured ~210 new company+title keys/week before fetch_dice was written).

Method
- Each candidate phrase is one Adzuna call per page, exactly the params fetch_adzuna would send
  (`_adzuna_search`'s shape) plus pagination. The fetcher hardcodes /search/1 because it runs
  many times a day at max_days_old=1; this probe reads ONE 7-day window, so paging is required or
  a productive phrase silently truncates at 50 and reports a fake-low rate. Adzuna's own `count`
  is printed next to what was fetched, so truncation is always visible rather than inferred.
- The blocking key is chain._norm_company + chain._norm_title — deliberately the SAME key the
  Dice probe used. The decision bar is a number from that probe, so the two must be measured on
  one ruler. (Contrast dice_overlap_probe's parser, which is frozen and independent on purpose:
  there the fragile thing was the undocumented flight payload, not the normalization.)
  Location is excluded from the key for the reason dupe_candidates.py states: cross-source
  location strings rarely agree.
- CONTROL phrases (already in config.yaml, already paid for) run alongside the candidates. A raw
  "N new keys" has no scale; N against a lane already judged worth its cost does.

Reads jobs.db read-only and NEVER inserts, evaluates, or mutates config. Outputs land in
results/ (gitignored), never the repo root.

Usage:
    python tests/validation/adzuna_overlap_probe.py                 # 7-day window, default phrases
    python tests/validation/adzuna_overlap_probe.py --days 14 --pages 5
    python tests/validation/adzuna_overlap_probe.py --no-control    # candidates only
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from urllib import parse

from _common import DB_PATH, RESULTS_DIR

import chain
import core
import fetch

LOG_PATH = RESULTS_DIR / "adzuna_overlap_log.jsonl"

# The gap under test: the AI engineer/architect family the ATS `title_any` already opts into,
# plus the two exact title shapes the 2026-08-18 alert surfaced. NOTE what_phrase is an EXACT
# contiguous phrase — "AI engineer" does NOT match "AI Applications Engineer" or "AI
# Infrastructure Engineer", which is why those two shapes are probed as their own phrases
# rather than assumed covered by the base one.
CANDIDATE_PHRASES = [
    "AI engineer",
    "AI architect",
    "applied AI",
    "AI applications",
    "AI infrastructure",
]

# Already in config.yaml — the scale reference, not part of the proposed addition.
CONTROL_PHRASES = [
    "forward deployed",
    "AI implementation",
]


def _search_page(country, app_id, app_key, phrase, where, rpp, max_days, page):
    """One Adzuna call for `phrase` at `page`. Mirrors fetch._adzuna_search's params (sort_by=date
    so paging walks newest-first instead of re-serving one relevance top-N) and adds the page
    number the fetcher's hardcoded /search/1 cannot express. Returns (results, total_count).
    Raises on a wrong-shaped 200 for the same reason the fetcher does: a silent [] would read as
    'this phrase finds nothing', which is exactly the false negative this probe exists to avoid."""
    import urllib.request

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": rpp,
        "max_days_old": max_days,
        "sort_by": "date",
        "what_phrase": phrase,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    base = fetch.ADZUNA_SEARCH_URL.format(country=country).rsplit("/", 1)[0]
    # The app_key rides in the query string (Adzuna's requirement, see fetch._adzuna_search) —
    # this URL must never reach a log line or an exception the caller prints unredacted.
    url = base + "/" + str(page) + "?" + parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        shape = ("object with keys " + str(sorted(payload)[:10]) if isinstance(payload, dict)
                 else type(payload).__name__)
        raise ValueError("Adzuna response carried no results list (" + shape + ")")
    return results, payload.get("count")


def _known_keys_and_urls(days):
    """Everything the four configured sources already hold: the company+title key space (all of
    it — a role the pipeline saw six weeks ago is not 'new'), and the job_url set (what the PK
    would dedup on insert). Read-only, query_only: this script must never be the thing that
    writes to jobs.db."""
    con = sqlite3.connect("file:" + str(DB_PATH) + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    keys = set()
    for company, title in con.execute("SELECT company, title FROM jobs"):
        keys.add((chain._norm_company(company), chain._norm_title(title)))
    urls = {r[0] for r in con.execute("SELECT job_url FROM jobs")}
    # Per-source row counts inside the same window, for the cost comparison in the report.
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    per_day = dict(con.execute(
        "SELECT source, COUNT(*) FROM jobs WHERE substr(first_seen,1,10) >= ? GROUP BY source",
        (cutoff,)).fetchall())
    con.close()
    return keys, urls, per_day


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=7, help="posted-within window in days (default 7)")
    ap.add_argument("--pages", type=int, default=4, help="max pages per phrase (default 4 = 200 ads)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between calls (default 2)")
    ap.add_argument("--no-control", action="store_true", help="skip the already-configured control phrases")
    args = ap.parse_args()

    cfg = core.load_config()
    adz = (cfg["settings"].get("adzuna") or {})
    country = adz.get("country", "us")
    where = adz.get("where") or ""
    rpp = adz.get("results_per_search", 50)

    app_id = core._ensure_api_key("ADZUNA_APP_ID", label="adzuna")
    app_key = core._ensure_api_key("ADZUNA_APP_KEY", label="adzuna")
    if not (app_id and app_key):
        sys.exit("ADZUNA_APP_ID / ADZUNA_APP_KEY not available — nothing measured")

    now = datetime.now()
    run_stamp = now.isoformat(timespec="seconds")
    known_keys, known_urls, per_source = _known_keys_and_urls(args.days)
    print("jobs.db: " + str(len(known_keys)) + " distinct company+title keys, "
          + str(len(known_urls)) + " urls")
    print("window: " + str(args.days) + "d · pages/phrase: " + str(args.pages)
          + " · rpp: " + str(rpp) + " · country: " + country + "\n")

    plan = [(p, "candidate") for p in CANDIDATE_PHRASES]
    if not args.no_control:
        plan += [(p, "control") for p in CONTROL_PHRASES]

    header = ("%-24s %-10s %7s %5s %5s %7s %7s"
              % ("phrase", "role", "adzuna", "got", "trunc", "newurl", "NEWKEY"))
    print(header)
    print("-" * len(header))

    by_url, failures = {}, []
    for phrase, role in plan:
        fetched, total_count, new_keys_here, new_urls_here = 0, None, set(), 0
        try:
            for page in range(1, args.pages + 1):
                results, count = _search_page(country, app_id, app_key, phrase, where,
                                              rpp, args.days, page)
                if total_count is None:
                    total_count = count
                for r in results:
                    url = fetch._adzuna_job_url(r)
                    if not url or url in by_url:
                        continue
                    title = r.get("title") or ""
                    company = (r.get("company") or {}).get("display_name") or ""
                    key = (chain._norm_company(company), chain._norm_title(title))
                    rec = {
                        "url": url, "title": title, "company": company,
                        "location": (r.get("location") or {}).get("display_name") or "",
                        "posted": str(r.get("created") or ""),
                        "phrase": phrase, "role": role,
                        "key": "|".join(key),
                        "known_key": key in known_keys,
                        "known_url": url in known_urls,
                    }
                    by_url[url] = rec
                    if not rec["known_key"]:
                        new_keys_here.add(rec["key"])
                    if not rec["known_url"]:
                        new_urls_here += 1
                fetched += len(results)
                time.sleep(args.delay)
                if len(results) < rpp:
                    break
        except Exception as e:
            # One phrase's failure must not kill the probe; redact in case the URL rode along.
            failures.append((phrase, type(e).__name__ + ": "
                             + fetch._redact(str(e), app_id, app_key)))
        tc = "?" if total_count is None else str(total_count)
        trunc = "YES" if (total_count or 0) > fetched else ""
        print("%-24s %-10s %7s %5d %5s %7d %7d"
              % (phrase[:24], role, tc, fetched, trunc, new_urls_here, len(new_keys_here)))

    ads = list(by_url.values())
    if not ads:
        sys.exit("every phrase failed or returned nothing — no observation recorded")

    cand = [a for a in ads if a["role"] == "candidate"]
    cand_new_keys = {a["key"] for a in cand if not a["known_key"]}
    cand_new_urls = [a for a in cand if not a["known_url"]]
    ctrl = [a for a in ads if a["role"] == "control"]
    ctrl_new_keys = {a["key"] for a in ctrl if not a["known_key"]}

    print("-" * len(header))
    wk = 7.0 / args.days
    print("\nCANDIDATE phrases: " + str(len(cand)) + " unique ads")
    print("  NEW company+title keys : %d  (~%.0f/week)" % (len(cand_new_keys), len(cand_new_keys) * wk))
    print("  rows that would INSERT : %d  (~%.0f/day reaching paid eval)"
          % (len(cand_new_urls), len(cand_new_urls) / float(args.days)))
    if ctrl:
        print("CONTROL phrases (already configured): %d ads, %d new keys (~%.0f/week)"
              % (len(ctrl), len(ctrl_new_keys), len(ctrl_new_keys) * wk))
    print("\nDice precedent that justified a new source: ~210 new keys/week")
    print("current source volume in window: "
          + ", ".join(k + "=" + str(v) for k, v in sorted(per_source.items(), key=lambda kv: -kv[1])))
    if failures:
        print("\n" + str(len(failures)) + " phrase failure(s):")
        for phrase, err in failures:
            print("  " + phrase + ": " + err)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for a in ads:
            f.write(json.dumps(dict({"run": run_stamp}, **a), ensure_ascii=False) + "\n")

    snap = RESULTS_DIR / ("adzuna_overlap_" + now.strftime("%Y%m%d-%H%M") + ".md")
    lines = [
        "# Adzuna overlap probe — " + run_stamp,
        "",
        "Window %dd · %d pages/phrase · candidates %d ads / %d new keys (~%.0f/week) · "
        "would insert %d (~%.0f/day)"
        % (args.days, args.pages, len(cand), len(cand_new_keys), len(cand_new_keys) * wk,
           len(cand_new_urls), len(cand_new_urls) / float(args.days)),
        "",
        "Control phrases (already paid for): %d ads / %d new keys." % (len(ctrl), len(ctrl_new_keys)),
        "Dice precedent for adding a source: ~210 new keys/week.",
        "",
        "## New roles from CANDIDATE phrases (no matching company+title chain in jobs.db)",
        "",
        "| posted | company | title | location | phrase |",
        "|---|---|---|---|---|",
    ]
    seen_key = set()
    for a in sorted(cand, key=lambda x: x["posted"], reverse=True):
        if a["known_key"] or a["key"] in seen_key:
            continue
        seen_key.add(a["key"])
        cells = [a["posted"][:10], a["company"], a["title"], a["location"], a["phrase"]]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    if failures:
        lines += ["", "## Failures", ""] + ["- `" + p + "`: " + e for p, e in failures]
    snap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nwrote " + str(snap))
    print("appended " + str(len(ads)) + " rows to " + str(LOG_PATH))


if __name__ == "__main__":
    main()
