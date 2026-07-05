#!/usr/bin/env python3
"""The three posting source families: the LinkedIn scrape (python-jobspy guest endpoints), the
Adzuna REST API, and the per-company ATS board APIs (Greenhouse/Lever/Ashby — public, no auth).
All insert unseen postings as status='new' and are otherwise source-agnostic from then on — the
`source` column is provenance only. Imports core (the API-key resolver), chain (the
fingerprint/repost helpers), and filters (_pattern_matches, so the ATS title/location filters
speak the same pattern dialect as filters.yaml); nothing depends back on this module except
pipeline's `run`.
"""

import html
import json
import re
import sys
import time
from datetime import datetime

from core import _ensure_api_key, PARSE_MIN, PARSE_MAX, parse_iso
from chain import _norm_company, _norm_title, _fingerprint, _find_repost
from filters import _pattern_matches, validate_pattern  # one pattern dialect + validator
# for filters.yaml AND settings.ats


# ---------------------------------------------------------------------- fetch

def fetch_new_jobs(cfg, conn):
    """Run every configured search; insert unseen postings as status='new'."""
    from jobspy import scrape_jobs  # imported here so `report` works even if jobspy breaks

    s = cfg["settings"]
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0

    for search in cfg["searches"]:
        name = search["name"]
        print(f"[fetch] {name}: {search['term']}")
        try:
            df = scrape_jobs(
                site_name=["linkedin"],
                search_term=search["term"],
                location=s["location"],
                hours_old=s["hours_old"],
                results_wanted=s["results_per_search"],
                job_type=search.get("job_type"),
                linkedin_fetch_description=True,
                enforce_annual_salary=True,
                description_format="markdown",
            )
        except Exception as e:
            print(f"[fetch] {name} FAILED: {e}", file=sys.stderr)
            continue

        if df is None or df.empty:
            print(f"[fetch] {name}: 0 results")
            time.sleep(s["delay_between_searches"])
            continue

        for _, row in df.iterrows():
            url = row.get("job_url")
            if not isinstance(url, str) or not url:
                continue
            desc = row.get("description")
            if not isinstance(desc, str):  # pandas yields NaN (float) for empty cells
                desc = ""
            company, title, location = row.get("company"), row.get("title"), row.get("location")
            n, repost_of = _insert_posting(
                conn, url=url, title=title, company=company, location=location,
                search_name=name, tier=search.get("tier", "primary"),
                date_posted=_linkedin_date(row.get("date_posted")),
                first_seen=today_iso,
                salary_min=_num(row.get("min_amount")), salary_max=_num(row.get("max_amount")),
                description=desc[: s["max_description_chars"]], source="linkedin",
            )
            inserted += n
            if n and repost_of:
                reposts += 1
                print(f"[repost] {title} — {company} (relisting of {repost_of})")

        conn.commit()
        print(f"[fetch] {name}: {len(df)} returned")
        time.sleep(s["delay_between_searches"])

    print(f"[fetch] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    return inserted


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _as_list(v):
    """Normalize a scalar-or-list YAML config value to a list: None → [], scalar → [scalar].
    The guard every list-typed config knob needs — iterating a raw string would yield its
    CHARACTERS (a substring filter that matches everything, the flood-guard bypass)."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _redact(msg, *secrets):
    """Scrub secret values (the Adzuna app_id/app_key) from a string before it is printed,
    so a credential that travels in the Adzuna request URL can never reach a log line — even
    if a future exception type embeds the full URL. Only non-empty secrets are replaced
    (guards against blanking the whole string on an empty/missing key)."""
    out = msg
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _linkedin_date(v):
    """Day-granularity date_posted for LinkedIn rows. jobspy yields a date or None, but a
    pandas datetime64 column stringifies as "YYYY-MM-DD 00:00:00" — which parse_iso would
    read as a real MIDNIGHT timestamp (fake hour precision, unhedged age label). LinkedIn
    dates are day-granularity by nature, so keep ONLY the date part. Deliberately NOT routed
    through _ats_date: that helper PRESERVES time-of-day, which is right for boards that mean
    it and exactly wrong here. Non-date-ish values (None/NaT/nan) degrade to ""."""
    s = str(v or "")
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else ""


def _insert_posting(conn, *, url, title, company, location, search_name, tier,
                    date_posted, first_seen, salary_min, salary_max, description, source):
    """The shared normalize → fingerprint → repost-link → INSERT tail of every fetcher, so
    the jobs column list exists exactly once and the sources can't drift (same reasoning as
    chain.effective_decision having a single implementation). INSERT OR IGNORE on the
    job_url primary key is dedup layer one; _find_repost is layer two. Returns
    (inserted, repost_of) — inserted is 0 or 1."""
    norm_company = _norm_company(company)
    norm_title = _norm_title(title)
    fingerprint = _fingerprint(company, location)
    repost_of = _find_repost(conn, fingerprint, norm_title, exclude_url=url)
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs
           (job_url, title, company, location, search_name, tier, date_posted,
            first_seen, salary_min, salary_max, description, status,
            norm_company, norm_title, fingerprint, repost_of, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?)""",
        (url, title, company, location, search_name, tier, date_posted, first_seen,
         salary_min, salary_max, description, norm_company, norm_title, fingerprint,
         repost_of, source),
    )
    return cur.rowcount, repost_of


# ---------------------------------------------------------------- Adzuna fetch
#
# Adzuna is a sanctioned REST API (free tier) used as a SECOND source alongside the
# LinkedIn scrape. We added it after confirming Indeed/Glassdoor/ZipRecruiter/Google are
# all behind anti-bot walls; Adzuna's API is not. Two quirks shape the mapping below:
#   * descriptions are hard-capped at 500 chars by the API — a snippet, not the full JD —
#     so these rows are flagged in the report/UI (the eval judges them on thin text);
#   * salaries may be ML-PREDICTED (the `salary_is_predicted` flag). A predicted number must
#     not reach the deterministic salary filter, so we store it as NULL ("unstated", kept).
# Everything else flows through the same dedup/eval/report path as LinkedIn.

ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
# Adzuna keyword params we forward from a query block; anything else in the block is ignored.
# All are AND-combined by Adzuna; within `what_or`/`what_exclude` the words are any-of/none-of.
_ADZUNA_WHAT_KEYS = ("what", "what_and", "what_phrase", "what_or", "what_exclude")


def _adzuna_search(country, app_id, app_key, query, where, rpp, max_days):
    """One Adzuna API call. `query` is a dict of what_* params. Returns the results list."""
    import urllib.parse
    import urllib.request

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": rpp,
        "max_days_old": max_days,
        # Sort newest-first, not by relevance (Adzuna's default). With only one page fetched,
        # relevance sort would re-return the same top-N every run and never reach newer
        # lower-relevance postings; date sort makes each run surface what's actually new.
        "sort_by": "date",
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    for k in _ADZUNA_WHAT_KEYS:
        v = query.get(k)
        if v:
            params[k] = v
    # Adzuna authenticates via query-string params (app_id/app_key), not a header — that is the
    # API's requirement, not a choice. The key therefore lives in this URL string, so it must
    # never be logged; the caller's error path runs the exception message through _redact() as a
    # safety net in case a future exception type embeds the URL.
    url = ADZUNA_SEARCH_URL.format(country=country) + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp).get("results", [])


def fetch_adzuna(cfg, conn):
    """Fetch postings from the Adzuna API for every search that defines an `adzuna:` block;
    insert unseen ones as status='new', source='adzuna'. No-op (with a notice) if the
    ADZUNA_APP_ID / ADZUNA_APP_KEY credentials are absent, so `run` still works without it."""
    app_id = _ensure_api_key("ADZUNA_APP_ID", label="adzuna")
    app_key = _ensure_api_key("ADZUNA_APP_KEY", label="adzuna")
    if not (app_id and app_key):
        print("[adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping Adzuna source")
        return 0

    s = cfg["settings"]
    adz = s.get("adzuna") or {}
    country = adz.get("country", "us")
    where = adz.get("where") or ""
    rpp = adz.get("results_per_search", 50)
    max_days = adz.get("max_days_old", 1)
    delay = adz.get("delay_between_calls", 2)
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0

    for search in cfg["searches"]:
        block = search.get("adzuna")
        if not block:
            continue
        name = search["name"]
        # A block is one query dict, or a list of them (used to express OR-of-phrases —
        # Adzuna allows only a single what_phrase per call, so each variant is its own call).
        queries = _as_list(block)
        for query in queries:
            # A query with no what_* keys would match EVERYTHING — skip it rather than pull a
            # page of arbitrary jobs (guards against an empty/typo'd config block).
            if not any(query.get(k) for k in _ADZUNA_WHAT_KEYS):
                print(f"[adzuna] {name}: query block has no what_* keys — skipping", file=sys.stderr)
                continue
            label = query.get("what_phrase") or query.get("what") or query.get("what_or") or "?"
            print(f"[adzuna] {name}: {label}")
            try:
                results = _adzuna_search(country, app_id, app_key, query, where, rpp, max_days)
            except Exception as e:
                # Redact the credentials in case the exception message carries the request URL
                # (Adzuna auth is in the query string — see _adzuna_search).
                print(f"[adzuna] {name} ({label}) FAILED: {_redact(str(e), app_id, app_key)}",
                      file=sys.stderr)
                time.sleep(delay)
                continue

            for r in results:
                url = r.get("redirect_url")
                if not isinstance(url, str) or not url:
                    continue
                title = r.get("title")
                company = (r.get("company") or {}).get("display_name")
                location = (r.get("location") or {}).get("display_name")
                desc = r.get("description")
                if not isinstance(desc, str):
                    desc = ""
                # Predicted salaries are Adzuna's ML guess, not the posting's — drop to NULL so
                # the deterministic salary filter never rejects a real job on an estimate.
                # Accept any truthy encoding ("1"/1/True/"true"), not just the documented "1".
                predicted = str(r.get("salary_is_predicted") or "").strip().lower() in ("1", "true")
                n, repost_of = _insert_posting(
                    conn, url=url, title=title, company=company, location=location,
                    search_name=name, tier=search.get("tier", "primary"),
                    date_posted=str(r.get("created") or ""), first_seen=today_iso,
                    salary_min=None if predicted else _num(r.get("salary_min")),
                    salary_max=None if predicted else _num(r.get("salary_max")),
                    description=desc[: s["max_description_chars"]], source="adzuna",
                )
                inserted += n
                if n and repost_of:
                    reposts += 1
            conn.commit()
            print(f"[adzuna] {name} ({label}): {len(results)} returned")
            time.sleep(delay)

    print(f"[adzuna] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    return inserted


# ------------------------------------------------------------------- ATS fetch
#
# Third source family: per-company ATS boards (Greenhouse / Lever / Ashby), via their PUBLIC
# no-auth JSON APIs — sanctioned like Adzuna, but with no credentials at all, so the gate is
# config-only. Unlike LinkedIn/Adzuna these are per-company with no search query: a board
# returns every open role at the company, worldwide. The config (settings.ats) therefore
# carries a curated company list plus shared title_any / location_any filters that decide
# which postings enter the DB — the guard that keeps a 500-job board from flooding the paid
# eval. Descriptions are FULL text (Greenhouse/Lever HTML → stripped, Ashby already plain),
# unlike Adzuna's 500-char snippet. Salaries are stored NULL (boards rarely state comp
# uniformly; NULL = "unstated", which the salary filter keeps — the same convention as
# Adzuna's predicted salaries; Ashby's `compensation` field is a possible future source).
# ATS rows also sit outside the per-search min_salary floors structurally: apply_salary_filter
# keys on search_name matching a `searches:` entry, and these rows use 'ats:<slug>' names.
# No posting-age filter on purpose: a board lists only currently-open roles, so an old
# first_published is still an applyable job, and INSERT OR IGNORE makes re-fetching the whole
# board every run idempotent.

# Block-closing tags become newlines so paragraph/bullet structure survives for the eval.
_TAG_BREAK = re.compile(r"</(?:p|li|div|ul|ol|h[1-6])\s*>|<br\s*/?>", re.IGNORECASE)
_TAG_ANY = re.compile(r"<[^>]+>")


def _strip_html(s, escaped=False):
    """HTML → plain text. `escaped=True` is for Greenhouse, which ships `content`
    HTML-ESCAPED (&lt;p&gt;…): its markup needs one unescape BEFORE tag-stripping or the
    regex sees no tags. Lever content is RAW HTML and must NOT get that first pass — a
    once-escaped literal like "Travel: &lt;5%" would become a bare '<' and the tag regex
    would swallow the text after it. The final unescape resolves the entities that remain
    inside the text either way."""
    if not isinstance(s, str) or not s:
        return ""
    if escaped:
        s = html.unescape(s)
    s = _TAG_BREAK.sub("\n", s)
    s = _TAG_ANY.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _ats_date(v):
    """Normalize a board's posted-at value for the date_posted column, PRESERVING time-of-day
    when the board gives it — the recency triage (report._recency_dt) parses this strictly and
    sorts fresh postings by it, so truncating a real timestamp to a bare date would throw away
    exactly the intra-day precision that feature needs. Goes through core.parse_iso — the same
    parser the read side uses — so what fetch stores, report can always parse. Storage shapes:
    full timestamps (Greenhouse/Ashby ISO, Lever epoch-ms) → local-naive ISO seconds (the
    first_seen convention); bare calendar dates in ANY ISO form → YYYY-MM-DD (day granularity
    is honest — never invent a midnight); unparseable or absurd values (parse_iso's sanity
    window) degrade to "". bool is excluded explicitly — it passes isinstance(int) and would
    come back as 1969/1970; so is 0 (a zeroed Lever createdAt), the same epoch garbage."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not v:
            return ""
        try:
            dt = datetime.fromtimestamp(v / 1000)
        except (OverflowError, OSError, ValueError):
            return ""
        return dt.isoformat(timespec="seconds") if PARSE_MIN <= dt <= PARSE_MAX else ""
    if isinstance(v, str):
        s = v.strip()
        parsed = parse_iso(s)
        if parsed is None:
            # Unparseable as ISO but starting with a date (e.g. an exotic suffix): keep the
            # day — re-validated through parse_iso, so a range-rejected placeholder
            # ("9999-12-31") or an invalid calendar date stays "", not rescued by the regex.
            m = re.match(r"\d{4}-\d{2}-\d{2}", s)
            return m.group(0) if m and parse_iso(m.group(0)) else ""
        dt, day_only = parsed
        return dt.date().isoformat() if day_only else dt.isoformat(timespec="seconds")
    return ""


def _ats_rows_greenhouse(data, company):
    """{"jobs": [...]} → normalized posting dicts. Greenhouse is the one board whose payload
    carries the company name (company_name); the config-derived name is only the fallback.
    A wrong-shaped payload raises here — fetch_ats catches it and logs a FAILED line, so an
    API envelope change never masquerades as an empty board."""
    rows = []
    for j in data["jobs"]:
        location = (j.get("location") or {}).get("name") or ""
        rows.append({
            "url": j.get("absolute_url"),
            "title": j.get("title") or "",
            "company": j.get("company_name") or company,
            "location": location,
            "locations": [location],
            "date_posted": _ats_date(j.get("first_published")),
            "description": _strip_html(j.get("content"), escaped=True),
            # Greenhouse has no structured remote flag; a location that SAYS remote is
            # already caught by the location patterns, so no substring heuristic here.
            "remote": False,
        })
    return rows


def _ats_rows_lever(data, company):
    """Top-level list of postings → normalized dicts. descriptionPlain is only the intro —
    the requirements/responsibilities live in the `lists` sections and the closing blurb in
    additionalPlain, so all of them are joined or the eval would judge the role on its
    preamble. Lever payloads carry no company name; it comes from config. A wrong-shaped
    payload (e.g. an error dict instead of the postings list) raises in the loop —
    fetch_ats catches it and logs a FAILED line."""
    if not isinstance(data, list):
        raise ValueError(f"expected a postings list, got {type(data).__name__}")
    rows = []
    for j in data:
        cats = j.get("categories") or {}
        primary = cats.get("location") or ""
        # allLocations carries every posted location; filtering on the primary alone would
        # drop a role whose second location is the one the user wants. Drop blank entries
        # (like Ashby's list below) so a leading "" can't become the fingerprint/display value.
        locations = [l for l in _as_list(cats.get("allLocations")) if isinstance(l, str) and l.strip()]
        locations = locations or ([primary] if primary else [])
        parts = [j.get("descriptionPlain") or ""]
        for sec in j.get("lists") or []:
            parts.append((sec.get("text") or "").strip())
            parts.append(_strip_html(sec.get("content")))
        parts.append(j.get("additionalPlain") or "")
        rows.append({
            "url": j.get("hostedUrl"),
            "title": j.get("text") or "",
            "company": company,
            # Fall back to the first listed location so a role matched on a secondary
            # location doesn't display a blank primary in the report/UI.
            "location": primary or (locations[0] if locations else ""),
            "locations": locations,
            "date_posted": _ats_date(j.get("createdAt")),
            "description": "\n\n".join(p for p in parts if p),
            "remote": (j.get("workplaceType") or "").lower() == "remote",
        })
    return rows


def _ats_rows_ashby(data, company):
    """{"jobs": [...]} → normalized dicts. Unlisted postings (isListed=false) are skipped —
    they are drafts/hidden roles the board UI would not show either. descriptionPlain is
    already plain text. Ashby payloads carry no company name; it comes from config. A
    wrong-shaped payload raises here — fetch_ats catches it and logs a FAILED line."""
    rows = []
    for j in data["jobs"]:
        # Deliberately `is False`, not falsy: if Ashby ever drops/renames the field we fail
        # OPEN (rows still face the title/location filters) instead of silently emptying
        # the board.
        if j.get("isListed") is False:
            continue
        primary = j.get("location") or ""
        locations = [l for l in [primary] + [
            sl["location"] for sl in _as_list(j.get("secondaryLocations"))
            if isinstance(sl, dict) and isinstance(sl.get("location"), str)
        ] if l]
        rows.append({
            "url": j.get("jobUrl"),
            "title": j.get("title") or "",
            "company": company,
            # Fall back to the first listed location so a role matched on a secondary
            # location doesn't display a blank primary in the report/UI.
            "location": primary or (locations[0] if locations else ""),
            "locations": locations,
            "date_posted": _ats_date(j.get("publishedAt")),
            "description": j.get("descriptionPlain") or "",
            "remote": bool(j.get("isRemote")),
        })
    return rows


# One registry per board — (url template, payload extractor) — a single source of truth so
# the config-validity guard and the dispatch in fetch_ats can't disagree.
ATS_BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                   _ats_rows_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", _ats_rows_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", _ats_rows_ashby),
}


def _ats_clean_patterns(patterns, label):
    """Sanitize a user pattern list (title_any / location_any), dropping each unusable pattern
    with a stderr notice via the shared filters.validate_pattern: non-strings (YAML `- re: x`
    parses as a DICT, an unquoted number as an int), blanks, empty-body `re:` (which would
    match everything), and `re:` regexes that don't compile (which would match nothing). Same
    validator as `reject --pattern`, so the one dialect is checked identically everywhere."""
    out = []
    for p in _as_list(patterns):
        reason = validate_pattern(p)
        if reason:
            hint = " (quote `re:` patterns in YAML)" if not isinstance(p, str) else ""
            print(f"[ats] ignoring {label} pattern {p!r} — {reason}{hint}", file=sys.stderr)
            continue
        out.append(p)
    return out


def _ats_title_ok(title, title_any):
    """True if any pattern matches the title — the same dialect as filters.yaml
    (case-insensitive substring, or a `re:`-prefixed regex)."""
    return any(_pattern_matches(k, title or "") for k in title_any)


def _ats_location_ok(locations, remote, location_any):
    """No location_any → accept everything. Otherwise accept when any pattern matches any
    of the posting's location strings (primary + secondary; filters.yaml dialect: substring
    or `re:` regex); a remote-flagged posting whose locations don't match is accepted only
    when the list contains the exact term "remote" — a qualified term like "remote - us" is
    a location pattern, not a remote opt-in, so it never silently admits remote-anywhere
    roles."""
    if not location_any:
        return True
    if any(_pattern_matches(k, loc) for loc in locations if loc for k in location_any):
        return True
    return remote and any(k.strip().lower() == "remote" for k in location_any)


def _ats_get(url):
    """One board fetch. The explicit User-Agent matters: the default Python-urllib UA is a
    common CDN/anti-bot block trigger (Ashby fronts through Cloudflare). No credentials exist
    in these URLs, so unlike Adzuna there is nothing to redact on the error path."""
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (jobsearch-pipeline)",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_ats(cfg, conn):
    """Fetch postings from company ATS boards (the Greenhouse/Lever/Ashby public JSON APIs)
    for every company under settings.ats.companies; insert unseen postings matching the shared
    title/location filters as status='new', source='<board>'. No credentials — the gate is
    config-only: no companies (or an empty title_any) → no-op with a notice."""
    s = cfg["settings"]
    ats = s.get("ats") or {}
    companies = _as_list(ats.get("companies"))
    if not companies:
        print("[ats] no settings.ats.companies configured — skipping ATS source")
        return 0
    title_any = _ats_clean_patterns(ats.get("title_any"), "title_any")
    if not title_any:
        # Mirrors the Adzuna no-what_*-keys guard: with no usable title filter a board would
        # insert EVERY open role at the company and flood the paid eval.
        print("[ats] settings.ats.title_any is empty (or every pattern was dropped) — "
              "skipping (would insert every posting on every board)", file=sys.stderr)
        return 0
    location_raw = ats.get("location_any")
    location_any = _ats_clean_patterns(location_raw, "location_any")
    if location_raw and not location_any:
        # location_any was configured but every pattern was unusable. Falling through would
        # leave it [], which _ats_location_ok reads as "no filter → accept every location" —
        # silently widening a restrict-intent filter into a flood. Refuse loudly, like the
        # title_any guard above (an ABSENT location_any is still fine — that's `not location_raw`).
        print("[ats] every settings.ats.location_any pattern was unusable — skipping (an empty "
              "location filter would accept every location)", file=sys.stderr)
        return 0
    # _num tolerates a quoted "2" and a bare `delay_between_calls:` (None) — either would
    # otherwise TypeError inside time.sleep and abort the run.
    delay = _num(ats.get("delay_between_calls", 2))
    if delay is None:
        delay = 2
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0

    for entry in companies:
        # A non-dict entry (a bare `- examplecorp` in YAML) must not crash the run — skip it
        # with a notice like any other malformed entry.
        if not isinstance(entry, dict):
            print(f"[ats] bad companies entry {entry!r} (expected slug/board mapping) — skipping",
                  file=sys.stderr)
            continue
        slug = entry.get("slug")
        if slug and not isinstance(slug, str):
            slug = str(slug)  # a digit-only board slug parses as a YAML int
        board = entry.get("board")
        if not slug or board not in ATS_BOARDS:
            print(f"[ats] bad companies entry (slug={slug!r}, board={board!r}) — skipping",
                  file=sys.stderr)
            continue
        # Lever/Ashby payloads carry no company name, so the display name is config-derived;
        # a title-cased slug is the fallback when `name` is unset.
        name = entry.get("name") or slug.replace("-", " ").title()
        tier = entry.get("tier") or "primary"  # `or`, not a .get default: `tier: null` → None
        url_template, extract = ATS_BOARDS[board]
        # The whole board — fetch, extract, AND the filter/insert rows — is one failure
        # unit: a wrong-shaped 200 response or a single bad row logs FAILED and moves on to
        # the next company instead of aborting the run. The rollback discards any partial
        # board inserts so the next company's commit can't ship them.
        kept = board_inserted = board_reposts = 0
        try:
            data = _ats_get(url_template.format(slug=slug))
            rows = extract(data, name)
            for r in rows:
                url = r["url"]
                if not isinstance(url, str) or not url:
                    continue
                if not _ats_title_ok(r["title"], title_any):
                    continue
                if not _ats_location_ok(r["locations"], r["remote"], location_any):
                    continue
                kept += 1
                # Salaries stay NULL ("unstated", kept by the salary filter): boards rarely
                # state comp uniformly — Ashby's `compensation` field is a future enhancement.
                n, repost_of = _insert_posting(
                    conn, url=url, title=r["title"], company=r["company"],
                    location=r["location"], search_name=f"ats:{slug}", tier=tier,
                    date_posted=r["date_posted"], first_seen=today_iso,
                    salary_min=None, salary_max=None,
                    description=r["description"][: s["max_description_chars"]], source=board,
                )
                board_inserted += n
                if n and repost_of:
                    board_reposts += 1
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[ats] {slug} ({board}) FAILED: {e}", file=sys.stderr)
            time.sleep(delay)
            continue
        inserted += board_inserted
        reposts += board_reposts
        print(f"[ats] {slug} ({board}): {len(rows)} listed, {kept} matched filters")
        time.sleep(delay)

    print(f"[ats] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    return inserted
