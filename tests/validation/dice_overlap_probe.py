"""Dice overlap probe — would a Dice fetcher surface roles the current sources miss?

Decision instrument for the "add Dice as a fourth source?" question (2026-08-09): before
building a fetcher, measure how many Dice postings from the CONFIGURED searches map to
company+title chains jobs.db has never seen. Run it once a day for about a week, then read
the week-to-date number.

- Read-only against jobs.db (opened with mode=ro); no pipeline state is touched.
- Queries Dice's public search pages (no login, no keys): the job list is embedded in the
  HTML as an escaped Next.js flight payload; we parse it directly.
- The overlap key is normalized company+title via chain._norm_company/_norm_title — the
  same dialect as the stored norm_company/norm_title columns and the dupe-candidates queue,
  so "new" here means "no chain in the DB would block-match it".
- Coverage is bounded (first --pages pages per query, 7-day posted window server-side);
  every truncation is printed (totalResults vs fetched), never silent.

Outputs (all under results/, gitignored):
- dice_overlap_log.jsonl  — one record per unique job observed per run (append-only);
  the week-to-date distinct-new-keys summary is computed from this log.
- dice_overlap_<stamp>.md — today's new-role list for eyeballing relevance.

Usage:  python tests/validation/dice_overlap_probe.py [--pages 3] [--days 7] [--delay 2.0]
"""
import argparse
import html as html_mod
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from _common import DB_PATH, RESULTS_DIR

import chain
import core

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SEARCH_URL = "https://www.dice.com/jobs?q={q}&filters.postedDate=SEVEN&page={page}"
LOG_PATH = RESULTS_DIR / "dice_overlap_log.jsonl"

# The flight payload escapes JSON quotes as \" — these patterns match that escaped text.
#
# This is deliberately a SECOND, independent parser: fetch.py decodes the flight chunks
# first and reads real JSON, while this one pattern-matches the raw escaped HTML. Keep it
# that way. This script is a frozen decision instrument — the recorded overlap log is the
# evidence for adding Dice at all — so rewriting it to import fetch.py would let a later
# fetcher change retroactively alter what an already-recorded measurement meant.
_ANCHOR = re.compile(r'jobList\\":\{\\"data\\":\[')
_TOTAL_RESULTS = re.compile(r'totalResults\\":(\d+)')
_TOTAL_PAGES = re.compile(r'totalPages\\":(\d+)')
_GUID = re.compile(r'\\"guid\\":\\"([0-9a-fA-F-]{36})\\"')


def _field(obj, name):
    m = re.search(r'\\"%s\\":\\"(.*?)\\"' % name, obj)
    return _unescape(m.group(1)) if m else ""


def _unescape(v):
    v = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), v)
    v = v.replace('\\\\"', '"').replace("\\/", "/").replace("\\\\", "\\")
    # HTML entities (&amp; etc.) would pollute the normalized key: "A &amp; B" normalizes
    # to "a amp b" and stops matching the DB's "a b" — unescape BEFORE keying.
    return html_mod.unescape(v)


def _split_job_objects(html):
    """Yield each escaped job-object substring from the jobList data array.

    Braces appear unescaped in the payload, so a depth counter splits the array's
    top-level objects; jobLocation etc. are nested and stay inside their object.
    """
    m = _ANCHOR.search(html)
    if not m:
        return []
    out, depth, start = [], 0, None
    for i in range(m.end(), len(html)):
        c = html[i]
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(html[start : i + 1])
                start = None
        elif depth == 0 and c == "]":
            break
    return out


def _parse_jobs(html):
    """Return (jobs, n_objects) — n_objects lets the caller detect a partial parse.

    Guids also appear in page furniture outside the jobList array (observed: 4 per page),
    so the parse check compares against the split objects, never the page-wide guid count.
    """
    jobs = []
    objects = _split_job_objects(html)
    if not objects and _GUID.search(html):
        # Fallback if the anchor/array shape ever shifts: a fixed window after each guid.
        objects = [
            html[m.start() : m.start() + 6000] for m in _GUID.finditer(html)
        ]
    for obj in objects:
        guid = _field(obj, "guid")
        if not guid:
            continue
        loc = ""
        mloc = re.search(r'\\"jobLocation\\":\{(.*?)\}', obj)
        if mloc:
            loc = _field(mloc.group(1), "displayName")
        jobs.append(
            {
                "guid": guid,
                "url": _field(obj, "detailsPageUrl") or f"https://www.dice.com/job-detail/{guid}",
                "title": _field(obj, "title"),
                "company": _field(obj, "companyName"),
                "location": loc,
                "posted": _field(obj, "postedDate"),
                # Composition facts for the go/no-go read: "Third Party, Contract" marks
                # C2C staffing flow, the population a Dice fetcher would need to filter.
                "employment": _field(obj, "employmentType"),
                "salary_text": _field(obj, "salary"),
                "remote": bool(re.search(r'\\"isRemote\\":true', obj)),
            }
        )
    return jobs, len(objects)


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def dice_queries(cfg):
    """(search_name, phrase) per configured search, phrase-deduped across searches.

    Adzuna blocks already state each search as plain phrases (Dice can't take LinkedIn
    boolean syntax either), so they are the query source of truth; searches without one
    fall back to the term's first quoted phrase, then the raw term.
    """
    seen, out = {}, []
    for s in cfg.get("searches", []):
        name = s.get("name", "?")
        adz = s.get("adzuna")
        blocks = adz if isinstance(adz, list) else ([adz] if isinstance(adz, dict) else [])
        phrases = [p for b in blocks if isinstance(b, dict) and (p := b.get("what_phrase"))]
        if not phrases:
            term = s.get("term", "")
            quoted = re.findall(r'"([^"]+)"', term)
            phrases = quoted[:1] or ([term] if term else [])
        for p in phrases:
            key = p.lower().strip()
            if key in seen:
                continue
            seen[key] = name
            out.append((name, p))
    return out


def _parse_posted(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--pages", type=int, default=3, help="max pages per query (default 3)")
    ap.add_argument("--days", type=int, default=7, help="posted-within window in days (default 7)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default 2)")
    args = ap.parse_args()

    cfg = core.load_config()
    queries = dice_queries(cfg)
    if not queries:
        sys.exit("no searches in config.yaml — nothing to probe")

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    known = set(con.execute("SELECT DISTINCT norm_company, norm_title FROM jobs").fetchall())
    n_rows = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    con.close()

    now = datetime.now(timezone.utc)
    run_stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = now - timedelta(days=args.days)

    print(f"== Dice overlap probe {run_stamp} ==")
    print(f"DB: {n_rows} rows, {len(known)} distinct company+title keys")
    print(f"queries: {len(queries)} (phrase-deduped), pages<={args.pages}, window={args.days}d\n")

    by_guid = {}       # guid -> job record (first query that saw it wins attribution)
    failures = []
    header = f"{'search':<26} {'query':<34} {'total':>5} {'fetch':>5} {'inWin':>5} {'new':>4}"
    print(header)
    print("-" * len(header))

    for name, phrase in queries:
        q = urllib.parse.quote(f'"{phrase}"')
        total_results = total_pages = None
        fetched, in_window, new_here = 0, 0, 0
        try:
            for page in range(1, args.pages + 1):
                if total_pages is not None and page > total_pages:
                    break
                html = _fetch(SEARCH_URL.format(q=q, page=page))
                if total_results is None:
                    mt = _TOTAL_RESULTS.search(html)
                    mp = _TOTAL_PAGES.search(html)
                    total_results = int(mt.group(1)) if mt else -1
                    total_pages = int(mp.group(1)) if mp else args.pages
                jobs, n_objects = _parse_jobs(html)
                if n_objects and len(jobs) < n_objects:
                    print(f"  ! partial parse: {len(jobs)}/{n_objects} objects on {name} p{page}")
                if not jobs:
                    break
                fetched += len(jobs)
                for j in jobs:
                    posted = _parse_posted(j["posted"])
                    if posted is None or posted < cutoff:
                        continue
                    in_window += 1
                    if j["guid"] in by_guid:
                        continue
                    key = (chain._norm_company(j["company"]), chain._norm_title(j["title"]))
                    j["key"] = "|".join(key)
                    j["known"] = key in known
                    j["search"] = name
                    j["query"] = phrase
                    if not j["known"]:
                        new_here += 1
                    by_guid[j["guid"]] = j
                time.sleep(args.delay)
        except Exception as e:  # one query's failure must not kill the probe
            failures.append((name, phrase, f"{type(e).__name__}: {e}"))
        total_str = "?" if total_results in (None, -1) else str(total_results)
        print(f"{name:<26} {phrase[:34]:<34} {total_str:>5} {fetched:>5} {in_window:>5} {new_here:>4}")

    jobs_today = list(by_guid.values())
    new_today = [j for j in jobs_today if not j["known"]]
    print("-" * len(header))
    print(
        f"unique jobs today: {len(jobs_today)} | known chains: {len(jobs_today) - len(new_today)}"
        f" | NEW company+title keys: {len({j['key'] for j in new_today})}"
    )
    emp = {}
    for j in new_today:
        emp[j["employment"] or "?"] = emp.get(j["employment"] or "?", 0) + 1
    print("new-role employment mix: " + ", ".join(f"{k}={v}" for k, v in sorted(emp.items(), key=lambda kv: -kv[1])))
    if failures:
        print(f"\n{len(failures)} query failure(s):")
        for name, phrase, err in failures:
            print(f"  {name} ({phrase}): {err}")
    if not jobs_today and failures:
        sys.exit("every query failed — no observation recorded")

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for j in jobs_today:
            f.write(json.dumps({"run": run_stamp, **j}, ensure_ascii=False) + "\n")

    # Week-to-date across runs: distinct keys never seen in the DB at their observation time.
    week_keys, week_runs = set(), set()
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_dt = _parse_posted(rec.get("run", ""))
            if run_dt is None or run_dt < now - timedelta(days=7):
                continue
            week_runs.add(rec["run"])
            if not rec.get("known"):
                week_keys.add(rec.get("key"))
    print(f"week-to-date distinct new keys: {len(week_keys)} over {len(week_runs)} run(s)")

    snap = RESULTS_DIR / f"dice_overlap_{now.strftime('%Y%m%d-%H%M')}.md"
    lines = [
        f"# Dice overlap probe — {run_stamp}",
        "",
        f"Unique jobs observed: {len(jobs_today)} · known chains: {len(jobs_today) - len(new_today)}"
        f" · new keys: {len({j['key'] for j in new_today})}",
        "",
        "## New roles (no matching company+title chain in jobs.db)",
        "",
        "| posted | company | title | location | type | search | url |",
        "|---|---|---|---|---|---|---|",
    ]
    for j in sorted(new_today, key=lambda x: x["posted"], reverse=True):
        cells = [j["posted"][:10], j["company"], j["title"], j["location"],
                 j["employment"], j["search"], j["url"]]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    snap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"new-role list: {snap}")


if __name__ == "__main__":
    main()
