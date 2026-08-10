#!/usr/bin/env python3
"""The four posting source families: the LinkedIn scrape (python-jobspy guest endpoints), the
Adzuna REST API, the per-company ATS board APIs (Greenhouse/Lever/Ashby — public, no auth), and
the Dice search pages (public, logged-out — the job list is embedded in the page HTML).
All insert unseen postings as status='new' and are otherwise source-agnostic from then on — the
`source` column is provenance only. Imports core (the API-key resolver), posting_store (the
shared normalize/fingerprint/insert path), health (the per-target attempt facts every fetcher
records), and filters (_pattern_matches + validate_pattern, so the ATS/Dice
config filters speak the same pattern dialect as filters.yaml); nothing depends back on this
module except pipeline's `run`.
"""

import html
import json
import re
import sys
import time
from urllib import parse
from datetime import datetime

from core import _ensure_api_key, PARSE_MIN, PARSE_MAX, parse_iso
from filters import _pattern_matches, validate_pattern  # one pattern dialect + validator
from health import (FetchSummary, fetch_definition_hash, fetch_error_kind,
                    record_active_fetch_attempt, utc_now_iso)
from posting_store import insert_posting as _insert_posting


# ---------------------------------------------------------------------- fetch

def fetch_new_jobs(cfg, conn) -> FetchSummary:
    """Run every configured search; insert unseen postings as status='new'."""
    from jobspy import scrape_jobs  # imported here so `report` works even if jobspy breaks

    s = cfg["settings"]
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0
    units = successes = failures = 0

    for search in cfg["searches"]:
        units += 1
        name = search["name"]
        attempt_started = utc_now_iso()
        definition_hash = fetch_definition_hash({
            "source": "linkedin",
            "search": {key: search.get(key) for key in ("name", "term", "job_type")},
            "settings": {key: s.get(key) for key in (
                "location", "hours_old", "results_per_search"
            )},
        })
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
            failures += 1
            record_active_fetch_attempt(
                conn, source_family="linkedin", target_kind="search", target_label=name,
                definition_hash=definition_hash, status="failed",
                error_kind=fetch_error_kind(e), started_at=attempt_started,
            )
            print(f"[fetch] {name} FAILED: {e}", file=sys.stderr)
            # A failure is often the rate-limiter talking — pause before the next search,
            # same as the 0-results path, instead of hammering the endpoint while it's sore.
            time.sleep(s["delay_between_searches"])
            continue

        if df is None or df.empty:
            successes += 1
            record_active_fetch_attempt(
                conn, source_family="linkedin", target_kind="search", target_label=name,
                definition_hash=definition_hash, status="success", returned_count=0,
                eligible_count=0, inserted_count=0, repost_count=0,
                started_at=attempt_started,
            )
            print(f"[fetch] {name}: 0 results")
            time.sleep(s["delay_between_searches"])
            continue

        search_inserted = search_reposts = eligible = 0
        try:
            for _, row in df.iterrows():
                url = row.get("job_url")
                if not isinstance(url, str) or not url:
                    continue
                eligible += 1
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
                search_inserted += n
                if n and repost_of:
                    search_reposts += 1
                    print(f"[repost] {title} — {company} (relisting of {repost_of})")
            record_active_fetch_attempt(
                conn, source_family="linkedin", target_kind="search", target_label=name,
                definition_hash=definition_hash, status="success", returned_count=len(df),
                eligible_count=eligible, inserted_count=search_inserted,
                repost_count=search_reposts, started_at=attempt_started, commit=False,
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            failures += 1
            record_active_fetch_attempt(
                conn, source_family="linkedin", target_kind="search", target_label=name,
                definition_hash=definition_hash, status="failed",
                error_kind=fetch_error_kind(e), started_at=attempt_started,
            )
            print(f"[fetch] {name} FAILED: {e}", file=sys.stderr)
            time.sleep(s["delay_between_searches"])
            continue
        successes += 1
        inserted += search_inserted
        reposts += search_reposts
        print(f"[fetch] {name}: {len(df)} returned")
        time.sleep(s["delay_between_searches"])

    print(f"[fetch] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    if not units:
        record_active_fetch_attempt(
            conn, source_family="linkedin", target_kind="family", target_label="linkedin",
            definition_hash=None, status="skipped", skip_reason="no configured searches",
        )
        return FetchSummary.skipped("no configured searches")
    return FetchSummary(
        inserted, units=units, successes=successes, failures=failures
    )


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


# ---------------------------------------------------------------- Adzuna fetch
#
# Adzuna is a sanctioned REST API (free tier) used as a SECOND source alongside the
# LinkedIn scrape. We added it after confirming Indeed/Glassdoor/ZipRecruiter/Google are
# all behind anti-bot walls; Adzuna's API is not. Two quirks shape the mapping below:
#   * descriptions are hard-capped at 500 chars by the API — a snippet, not the full JD —
#     so these rows are flagged in the report/UI (the eval judges them on thin text);
#   * salaries may be ML-PREDICTED (the `salary_is_predicted` flag). A predicted number must
#     not reach the deterministic salary filter, so we store it as NULL ("unstated", kept).
#   * `redirect_url` embeds a PER-REQUEST tracking token (?se=...), so the same ad gets a
#     different URL on every API call — stored raw it defeats the job_url primary key and the
#     same ad re-inserts (and re-bills the eval) every run. We therefore store a canonical URL
#     built from the stable ad id instead (see _adzuna_job_url).
# Everything else flows through the same dedup/eval/report path as LinkedIn.

ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
# Ad id as it appears in either observed redirect_url shape: /land/ad/<id>?se=... (tracked)
# and /details/<id>?utm_... — [0-9] on purpose (\d also matches Unicode digits, which the
# id-field guard below rejects; the two paths must agree or one ad splits across two PKs).
_ADZUNA_AD_ID = re.compile(r"/(?:land/ad|details)/([0-9]+)")


def _adzuna_job_url(r):
    """The canonical, stable job_url for an Adzuna result dict: built from the ad id (the `id`
    field, else parsed out of redirect_url's PATH) and redirect_url's own host — the host
    follows the configured country (adzuna.co.uk for gb, ...), so hardcoding www.adzuna.com
    would bake wrong-site links into the PK for non-us configs. Re-serves of the same ad then
    dedup on the primary key at insert. Falls back to the raw redirect_url whenever a canonical
    can't be built confidently (unparseable/scheme-less/malformed URL, no id) — a churny row
    beats a dropped posting or a wrong-host PK (the fingerprint still catches its reposts).
    Returns None when the result has no redirect_url at all: same skip as before this helper
    existed (an id-only degenerate result carries no title/company/description worth a row)."""
    redirect = r.get("redirect_url")
    if not isinstance(redirect, str) or not redirect:
        return None
    try:
        parts = parse.urlsplit(redirect)
    except ValueError:
        # Malformed authority (e.g. an unclosed IPv6 bracket) — one bad row must not abort
        # the whole Adzuna batch via _run_fetch_stage.
        return redirect
    # fullmatch on ASCII digits (str.isdigit also accepts Unicode digits, which would mint a
    # URL the regex-parsed form of the same ad never matches). `or ""` also rejects id=0/None.
    ad_id = str(r.get("id") or "")
    if not re.fullmatch(r"[0-9]+", ad_id):
        # Search the PATH only: an id inside the query string (?return_to=/details/999) is some
        # OTHER page's id — minting a canonical from it would collide distinct ads on one PK.
        m = _ADZUNA_AD_ID.search(parts.path)
        if not m:
            return redirect
        ad_id = m.group(1)
    if not parts.hostname:
        return redirect  # scheme-less/relative URL: no trustworthy host — keep the raw URL
    return f"https://{parts.hostname}/details/{ad_id}"
# Adzuna keyword params we forward from a query block; anything else in the block is ignored.
# All are AND-combined by Adzuna; within `what_or`/`what_exclude` the words are any-of/none-of.
_ADZUNA_WHAT_KEYS = ("what", "what_and", "what_phrase", "what_or", "what_exclude")


def _adzuna_search(country, app_id, app_key, query, where, rpp, max_days):
    """One Adzuna API call. `query` is a dict of what_* params. Returns the results list."""
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
    url = ADZUNA_SEARCH_URL.format(country=country) + "?" + parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp).get("results", [])


def fetch_adzuna(cfg, conn) -> FetchSummary:
    """Fetch postings from the Adzuna API for every search that defines an `adzuna:` block;
    insert unseen ones as status='new', source='adzuna'. No-op (with a notice) if the
    ADZUNA_APP_ID / ADZUNA_APP_KEY credentials are absent, so `run` still works without it."""
    if not any(isinstance(search, dict) and search.get("adzuna")
               for search in cfg.get("searches") or []):
        print("[adzuna] no searches define an adzuna block — skipping Adzuna source")
        record_active_fetch_attempt(
            conn, source_family="adzuna", target_kind="family", target_label="adzuna",
            definition_hash=None, status="skipped", skip_reason="no configured queries",
        )
        return FetchSummary.skipped("no configured queries")
    app_id = _ensure_api_key("ADZUNA_APP_ID", label="adzuna")
    app_key = _ensure_api_key("ADZUNA_APP_KEY", label="adzuna")
    if not (app_id and app_key):
        print("[adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping Adzuna source")
        record_active_fetch_attempt(
            conn, source_family="adzuna", target_kind="family", target_label="adzuna",
            definition_hash=None, status="skipped", skip_reason="credentials unavailable",
        )
        return FetchSummary.skipped("credentials unavailable")

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
    units = successes = failures = 0

    for search in cfg["searches"]:
        block = search.get("adzuna")
        if not block:
            continue
        name = search["name"]
        # A block is one query dict, or a list of them (used to express OR-of-phrases —
        # Adzuna allows only a single what_phrase per call, so each variant is its own call).
        queries = _as_list(block)
        for query in queries:
            units += 1
            attempt_started = utc_now_iso()
            definition_hash = fetch_definition_hash({
                "source": "adzuna", "search_name": name, "query": query,
                "settings": {"country": country, "where": where,
                             "results_per_search": rpp, "max_days_old": max_days},
            })
            # A query with no what_* keys would match EVERYTHING — skip it rather than pull a
            # page of arbitrary jobs (guards against an empty/typo'd config block).
            if not isinstance(query, dict) or not any(query.get(k) for k in _ADZUNA_WHAT_KEYS):
                failures += 1
                record_active_fetch_attempt(
                    conn, source_family="adzuna", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="failed",
                    error_kind="parse_or_validation", started_at=attempt_started,
                )
                print(f"[adzuna] {name}: query block has no what_* keys — skipping", file=sys.stderr)
                continue
            label = query.get("what_phrase") or query.get("what") or query.get("what_or") or "?"
            print(f"[adzuna] {name}: {label}")
            try:
                results = _adzuna_search(country, app_id, app_key, query, where, rpp, max_days)
                query_inserted = query_reposts = eligible = 0
                for r in results:
                    url = _adzuna_job_url(r)
                    if not url:
                        continue
                    eligible += 1
                    title = r.get("title")
                    company = (r.get("company") or {}).get("display_name")
                    location = (r.get("location") or {}).get("display_name")
                    desc = r.get("description")
                    if not isinstance(desc, str):
                        desc = ""
                    # Predicted salaries are Adzuna's ML guess, not the posting's — drop to NULL so
                    # the deterministic salary filter never rejects a real job on an estimate.
                    predicted = str(r.get("salary_is_predicted") or "").strip().lower() in ("1", "true")
                    n, repost_of = _insert_posting(
                        conn, url=url, title=title, company=company, location=location,
                        search_name=name, tier=search.get("tier", "primary"),
                        date_posted=str(r.get("created") or ""), first_seen=today_iso,
                        salary_min=None if predicted else _num(r.get("salary_min")),
                        salary_max=None if predicted else _num(r.get("salary_max")),
                        description=desc[: s["max_description_chars"]], source="adzuna",
                    )
                    query_inserted += n
                    if n and repost_of:
                        query_reposts += 1
                record_active_fetch_attempt(
                    conn, source_family="adzuna", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="success",
                    returned_count=len(results), eligible_count=eligible,
                    inserted_count=query_inserted, repost_count=query_reposts,
                    started_at=attempt_started, commit=False,
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                failures += 1
                record_active_fetch_attempt(
                    conn, source_family="adzuna", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="failed",
                    error_kind=fetch_error_kind(e), started_at=attempt_started,
                )
                # Redact credentials in case an exception embeds the query-string URL.
                print(f"[adzuna] {name} ({label}) FAILED: {_redact(str(e), app_id, app_key)}",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            successes += 1
            inserted += query_inserted
            reposts += query_reposts
            print(f"[adzuna] {name} ({label}): {len(results)} returned")
            time.sleep(delay)

    print(f"[adzuna] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    if not units:
        record_active_fetch_attempt(
            conn, source_family="adzuna", target_kind="family", target_label="adzuna",
            definition_hash=None, status="skipped", skip_reason="no configured queries",
        )
        return FetchSummary.skipped("no configured queries")
    return FetchSummary(
        inserted, units=units, successes=successes, failures=failures
    )


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
# first_published is still an applyable job, and the job_url-conflict skip in _insert_posting
# makes re-fetching the whole board every run idempotent.

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


def _ats_clean_patterns(patterns, label, prefix="ats"):
    """Sanitize a user pattern list (title_any / location_any / Dice's employment_exclude),
    dropping each unusable pattern
    with a stderr notice via the shared filters.validate_pattern: non-strings (YAML `- re: x`
    parses as a DICT, an unquoted number as an int), blanks, empty-body `re:` (which would
    match everything), and `re:` regexes that don't compile (which would match nothing). Same
    validator as `reject --pattern`, so the one dialect is checked identically everywhere.
    `prefix` names the calling source in the notice, so a scheduled log attributes a Dice
    config error to Dice rather than to the ATS boards."""
    out = []
    for p in _as_list(patterns):
        reason = validate_pattern(p)
        if reason:
            hint = " (quote `re:` patterns in YAML)" if not isinstance(p, str) else ""
            print(f"[{prefix}] ignoring {label} pattern {p!r} — {reason}{hint}",
                  file=sys.stderr)
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


def fetch_ats(cfg, conn) -> FetchSummary:
    """Fetch postings from company ATS boards (the Greenhouse/Lever/Ashby public JSON APIs)
    for every company under settings.ats.companies; insert unseen postings matching the shared
    title/location filters as status='new', source='<board>'. No credentials — the gate is
    config-only: no companies (or an empty title_any) → no-op with a notice."""
    s = cfg["settings"]
    ats = s.get("ats") or {}
    companies = _as_list(ats.get("companies"))
    if not companies:
        print("[ats] no settings.ats.companies configured — skipping ATS source")
        record_active_fetch_attempt(
            conn, source_family="ats", target_kind="family", target_label="ats",
            definition_hash=None, status="skipped", skip_reason="no configured boards",
        )
        return FetchSummary.skipped("no configured boards")
    title_any = _ats_clean_patterns(ats.get("title_any"), "title_any")
    if not title_any:
        # Mirrors the Adzuna no-what_*-keys guard: with no usable title filter a board would
        # insert EVERY open role at the company and flood the paid eval.
        print("[ats] settings.ats.title_any is empty (or every pattern was dropped) — "
              "skipping (would insert every posting on every board)", file=sys.stderr)
        record_active_fetch_attempt(
            conn, source_family="ats", target_kind="family", target_label="ats",
            definition_hash=fetch_definition_hash({"title_any": ats.get("title_any")}),
            status="failed", error_kind="parse_or_validation",
        )
        return FetchSummary.failed("ValueError")
    location_raw = ats.get("location_any")
    location_any = _ats_clean_patterns(location_raw, "location_any")
    if location_raw and not location_any:
        # location_any was configured but every pattern was unusable. Falling through would
        # leave it [], which _ats_location_ok reads as "no filter → accept every location" —
        # silently widening a restrict-intent filter into a flood. Refuse loudly, like the
        # title_any guard above (an ABSENT location_any is still fine — that's `not location_raw`).
        print("[ats] every settings.ats.location_any pattern was unusable — skipping (an empty "
              "location filter would accept every location)", file=sys.stderr)
        record_active_fetch_attempt(
            conn, source_family="ats", target_kind="family", target_label="ats",
            definition_hash=fetch_definition_hash({"location_any": location_raw}),
            status="failed", error_kind="parse_or_validation",
        )
        return FetchSummary.failed("ValueError")
    # _num tolerates a quoted "2" and a bare `delay_between_calls:` (None) — either would
    # otherwise TypeError inside time.sleep and abort the run.
    delay = _num(ats.get("delay_between_calls", 2))
    if delay is None:
        delay = 2
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0
    units = successes = failures = 0

    for index, entry in enumerate(companies, 1):
        units += 1
        attempt_started = utc_now_iso()
        # A non-dict entry (a bare `- examplecorp` in YAML) must not crash the run — skip it
        # with a notice like any other malformed entry.
        if not isinstance(entry, dict):
            failures += 1
            record_active_fetch_attempt(
                conn, source_family="ats", target_kind="board",
                target_label=f"entry {index}", definition_hash=fetch_definition_hash(entry),
                status="failed", error_kind="parse_or_validation",
                started_at=attempt_started,
            )
            print(f"[ats] bad companies entry {entry!r} (expected slug/board mapping) — skipping",
                  file=sys.stderr)
            continue
        slug = entry.get("slug")
        if slug and not isinstance(slug, str):
            slug = str(slug)  # a digit-only board slug parses as a YAML int
        board = entry.get("board")
        if not slug or board not in ATS_BOARDS:
            failures += 1
            record_active_fetch_attempt(
                conn, source_family="ats", target_kind="board",
                target_label=str(slug or f"entry {index}"),
                definition_hash=fetch_definition_hash(entry), status="failed",
                error_kind="parse_or_validation", started_at=attempt_started,
            )
            print(f"[ats] bad companies entry (slug={slug!r}, board={board!r}) — skipping",
                  file=sys.stderr)
            continue
        # Lever/Ashby payloads carry no company name, so the display name is config-derived;
        # a title-cased slug is the fallback when `name` is unset.
        name = entry.get("name") or slug.replace("-", " ").title()
        tier = entry.get("tier") or "primary"  # `or`, not a .get default: `tier: null` → None
        url_template, extract = ATS_BOARDS[board]
        definition_hash = fetch_definition_hash({
            "source": "ats", "company": entry,
            "filters": {"title_any": title_any, "location_any": location_any},
        })
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
            record_active_fetch_attempt(
                conn, source_family="ats", target_kind="board", target_label=slug,
                definition_hash=definition_hash, status="success", returned_count=len(rows),
                eligible_count=kept, inserted_count=board_inserted,
                repost_count=board_reposts, started_at=attempt_started, commit=False,
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            failures += 1
            record_active_fetch_attempt(
                conn, source_family="ats", target_kind="board", target_label=slug,
                definition_hash=definition_hash, status="failed",
                error_kind=fetch_error_kind(e), started_at=attempt_started,
            )
            print(f"[ats] {slug} ({board}) FAILED: {e}", file=sys.stderr)
            time.sleep(delay)
            continue
        successes += 1
        inserted += board_inserted
        reposts += board_reposts
        print(f"[ats] {slug} ({board}): {len(rows)} listed, {kept} matched filters")
        time.sleep(delay)

    print(f"[ats] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    return FetchSummary(
        inserted, units=units, successes=successes, failures=failures
    )


# ------------------------------------------------------------------ Dice fetch
#
# Fourth source family: Dice's public search pages, read logged-out with a browser
# User-Agent — probed 2026-08-09 with no bot wall (unlike Indeed/Glassdoor/ZipRecruiter/
# Google, which stay off-limits). There is no sanctioned API and jobspy has no Dice
# scraper, so this parses the page itself: the job list ships INSIDE the HTML as an
# escaped Next.js flight payload (script chunks of `self.__next_f.push([1,"..."])`, each a
# valid JSON string literal — decode and join them, then read real JSON). Per-query like
# Adzuna, gated per-search by a `dice:` block (one phrase or a list; Dice cannot parse
# LinkedIn boolean syntax, so phrases are the whole query dialect — each is sent quoted).
# The quirks that shape the code:
#   * the search payload has NO description, and the eval must see one — so each genuinely
#     new URL costs one detail-page fetch. Already-known URLs are skipped BEFORE that
#     request, which keeps the recurring re-crawl polite and cheap;
#   * postedDate is a precise UTC timestamp (to the second). Through _ats_date it lands as
#     local-naive seconds, so the chain's oldest observation becomes a real lower bound on
#     req age — the instrument that catches a LinkedIn relisting claiming "day 1";
#   * employmentType marks the C2C staffing flow ("Third Party", plus plain "Contract").
#     The config-side employment_exclude patterns keep that population out of the DB and
#     the paid eval — the same flood-guard role ATS title_any plays for boards. Because it
#     is a flood guard, the code refuses a sweep in which NO row carries an employment type
#     at all: a renamed payload field would disable the filter silently, and the population
#     it holds back is the one that costs money;
#   * salary is display text ("Depends on Experience", "USD 65.00 - 70.00 per hour") →
#     stored NULL ("unstated", kept by the salary filter — the Adzuna/ATS convention). The
#     detail page's schema.org baseSalary is a possible future source, deliberately unused
#     until its provenance (employer-stated vs imputed) is established;
#   * results are relevance-ranked and a sort=date param is silently ignored (probed), so
#     page depth inside the posted-window is the completeness bound — totalResults vs
#     fetched prints per query, so a truncated sweep is never silent.

DICE_SEARCH_URL = "https://www.dice.com/jobs?q={q}&filters.postedDate={window}&page={page}"
_DICE_WINDOWS = {1: "ONE", 3: "THREE", 7: "SEVEN"}  # the filter values Dice's UI offers
# A real browser UA: the stock urllib UA is a stock CDN block trigger (same lesson as
# _ats_get, sharper here because this is a page CDN, not a JSON API).
_DICE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_DICE_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_DICE_LIST = re.compile(r'"jobList":\s*\{')
_DICE_DATA = re.compile(r'"data":\s*\[')
_DICE_JSONLD = re.compile(r'"@type"\s*:\s*"JobPosting"')
_DICE_TOTAL = re.compile(r'"totalResults":\s*(\d+)')
_DICE_PAGES = re.compile(r'"totalPages":\s*(\d+)')
# One dead detail page is ordinary attrition; an all-fail sweep over a real sample is the
# signature of a changed detail-page shape, which must not read as "everything was known".
_DICE_MIN_DETAIL_SAMPLE = 3
_DICE_MAX_PAGES = 20  # ~30 postings/page; an unbounded sweep is spend, not thoroughness
# A truncated page has no closing brace, so the object scanner would walk the whole
# remaining flight (seconds on a multi-MB page, per detail fetch). Past this it is malformed.
_DICE_MAX_OBJECT_SCAN = 2_000_000


def _dice_get(url):
    import urllib.request

    req = urllib.request.Request(
        url, headers={"User-Agent": _DICE_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _dice_flight(page_html):
    """Join a page's flight chunks into one decoded text. Each chunk is a complete JSON
    string literal even when the payload it carries is split mid-object across chunks, so
    decoding chunk-by-chunk and joining in document order reassembles the stream."""
    return "".join(json.loads(c) for c in _DICE_CHUNK.findall(page_html))


def _dice_array_objects(text, start):
    """Slice the top-level {...} objects of the JSON array opening at text[start] == '['.
    A hand scanner (depth counter with in-string/escape awareness) instead of a JSON parse
    of the whole array: the array sits inside a larger flight row we neither need nor want
    to bind to, and titles legally contain braces."""
    objs, depth, in_str, esc, obj_start = [], 0, False, False, None
    for i in range(start + 1, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                objs.append(text[obj_start:i + 1])
                obj_start = None
        elif c == "]" and depth == 0:
            break
    return objs


def _dice_object_at(text, start):
    """The complete {...} slice of the JSON object opening at text[start] == '{', or None if
    it never closes within _DICE_MAX_OBJECT_SCAN. Same string/escape-aware scan as
    _dice_array_objects, and for the same reason: the object sits inside a larger flight row
    that isn't valid JSON on its own."""
    depth, in_str, esc = 0, False, False
    for i in range(start, min(len(text), start + _DICE_MAX_OBJECT_SCAN)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _dice_search_page(page_html):
    """Parse one search page → (job dicts, total_results, total_pages). Totals come from
    the FIRST totalResults/totalPages after the jobList anchor: unrelated widgets later in
    the flight carry their own (observed: a site-wide 6k figure 68KB downstream), and they
    only feed the printed truncation check plus the page-loop bound, never row selection.
    Field text is html.unescape'd: Dice serializes entities (&amp;) INSIDE the JSON
    strings, and a polluted company would break the normalized key against the other
    sources' clean spelling. Each job keeps both its detailsPageUrl (the stored job_url —
    the stable identity the search payload keeps returning) and a detail_url built from
    the guid: some postings link a direct-apply shell page that carries no JD, while
    job-detail/<guid> serves it for every posting kind (probed 2026-08-09).

    Everything is read from INSIDE the sliced jobList object, which matters twice:
      * key order stops mattering. Anchoring on `"jobList":{"data":[` required "data" to be
        the FIRST key, so Dice adding one ahead of it would stop every row parsing;
      * the totals are jobList's OWN. Taking the first totalResults anywhere after the
        anchor picked up an unrelated widget's site-wide figure (observed: 6033, 68KB
        downstream) whenever jobList's own was absent — which made a page that parsed ZERO
        rows report a healthy total, the exact "0 returned looks fine forever" failure.
    total_results is None ONLY when the envelope is absent or unterminated, so the caller
    can distinguish "nothing posted this window" from "this is not a Dice search page"."""
    flight = _dice_flight(page_html)
    m = _DICE_LIST.search(flight)
    if not m:
        return [], None, None
    envelope = _dice_object_at(flight, m.end() - 1)
    if envelope is None:  # truncated/dropped chunk — an envelope we cannot trust
        return [], None, None
    jobs = []
    md = _DICE_DATA.search(envelope)
    if md:
        for obj in _dice_array_objects(envelope, md.end() - 1):
            try:
                r = json.loads(obj)
            except ValueError:
                continue
            guid = r.get("guid")
            url = r.get("detailsPageUrl") or (
                f"https://www.dice.com/job-detail/{guid}" if guid else "")
            loc = r.get("jobLocation")
            jobs.append({
                "url": url,
                "detail_url": (f"https://www.dice.com/job-detail/{guid}"
                               if guid else url),
                "title": html.unescape(str(r.get("title") or "")),
                "company": html.unescape(str(r.get("companyName") or "")),
                "location": html.unescape(str((loc or {}).get("displayName") or "")
                                          if isinstance(loc, dict) else ""),
                "date_posted": str(r.get("postedDate") or ""),
                "employment": str(r.get("employmentType") or ""),
            })
    mt = _DICE_TOTAL.search(envelope)
    mp = _DICE_PAGES.search(envelope)
    # The envelope IS present here, so a missing count falls back to what we parsed rather
    # than to None — None is reserved for "no usable envelope".
    total_results = int(mt.group(1)) if mt else len(jobs)
    total_pages = int(mp.group(1)) if mp else None
    return jobs, total_results, total_pages


def _dice_description(page_html, max_chars):
    """The detail page's JD, read from the page's schema.org JobPosting block ONLY.
    HTML → plain text via _strip_html; "" when the page yields no anchored description.

    Anchored, deliberately NOT longest-wins. A detail page carries several other
    "description" values — a short meta description, and a flight component prop — and any
    of them can outgrow the JD. Nothing downstream can detect a substitution: the wrong text
    reaches the paid eval, the verdict caches onto the chain, and marking the role applied
    freezes it as immutable application evidence via materials.snapshot_jd.

    The anchor is the JSON-LD block Dice embeds for search engines (`"@type":
    "JobPosting"`), NOT a Next.js internal key. That matters: the JSON-LD is a public
    schema.org contract that survives the framework renaming its component props, and it is
    the same block the page's baseSalary/hiringOrganization live in. Verified against live
    pages 2026-08-10 — exactly one marker per page, and its description reproduces
    byte-for-byte what the earlier longest-wins reader had stored for those postings.

    More than one marker means the page grew a second JobPosting (a recommendations block),
    which would make "the first match" a coin flip; zero means the shape changed. Both yield
    "" rather than a guess — the caller counts that as a missing JD, skips the insert, and
    the still-unseen URL retries next run."""
    try:
        flight = _dice_flight(page_html)
    except ValueError:
        return ""
    if len(_DICE_JSONLD.findall(flight)) != 1:
        return ""
    m = _DICE_JSONLD.search(flight)
    assert m is not None
    start = flight.rfind("{", 0, m.start())  # the JSON-LD object's own opening brace
    if start == -1:
        return ""
    obj = _dice_object_at(flight, start)
    if obj is None:
        return ""
    try:
        data = json.loads(obj)
    except ValueError:
        return ""
    # The walk-back landed on the wrong brace if this is not the posting object itself.
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        return ""
    desc = data.get("description")
    return _strip_html(desc)[:max_chars] if isinstance(desc, str) else ""


def _dice_int(settings, key, default, low, high):
    """One validated integer knob, warning on ANYTHING unusable. `_num` alone is not enough:
    it happily turns a YAML bool into 1.0 (`results_pages: yes` → one page) and 2.9 into 2,
    both silently. A swallowed value here fails in one of two invisible directions — fetching
    nothing while still recording a healthy success, or removing a spend ceiling."""
    raw = settings.get(key, default)
    num = None if isinstance(raw, bool) else _num(raw)
    if num is None or num != int(num) or not low <= int(num) <= high:
        print(f"[dice] {key} must be a whole number {low}..{high} — got {raw!r}, "
              f"using {default}", file=sys.stderr)
        return default
    return int(num)


def _dice_phrases(block):
    """A search's dice: block → usable phrase list. Non-string/blank entries drop with a
    notice (the YAML footguns _as_list exists for), so one typo'd entry doesn't kill the
    search's other phrases."""
    phrases = []
    for p in _as_list(block):
        if not isinstance(p, str) or not p.strip():
            print(f"[dice] ignoring phrase {p!r} — expected a non-empty string",
                  file=sys.stderr)
            continue
        phrases.append(p.strip())
    return phrases


def fetch_dice(cfg, conn) -> FetchSummary:
    """Fetch postings from Dice search pages for every search that defines a `dice:` block;
    insert unseen postings (one detail-page fetch each for the JD) as status='new',
    source='dice'. Config-only gate, like ATS: no dice: blocks → no-op with a notice."""
    if not any(isinstance(search, dict) and search.get("dice")
               for search in cfg.get("searches") or []):
        print("[dice] no searches define a dice block — skipping Dice source")
        record_active_fetch_attempt(
            conn, source_family="dice", target_kind="family", target_label="dice",
            definition_hash=None, status="skipped", skip_reason="no configured queries",
        )
        return FetchSummary.skipped("no configured queries")

    s = cfg["settings"]
    d = s.get("dice") or {}
    pages_limit = _dice_int(d, "results_pages", 2, 1, _DICE_MAX_PAGES)
    max_days = _dice_int(d, "max_days_old", 7, 1, max(_DICE_WINDOWS))
    if max_days not in _DICE_WINDOWS:
        print(f"[dice] max_days_old must be one of {sorted(_DICE_WINDOWS)} "
              f"(Dice's own filter steps) — got {max_days!r}, using 7", file=sys.stderr)
        max_days = 7
    window = _DICE_WINDOWS[max_days]
    delay_raw = d.get("delay_between_calls", 2)
    delay = None if isinstance(delay_raw, bool) else _num(delay_raw)
    if delay is None or delay < 0:  # a negative delay raises inside time.sleep, mid-query
        print(f"[dice] delay_between_calls must be a number >= 0 — got {delay_raw!r}, "
              f"using 2", file=sys.stderr)
        delay = 2
    # Absent → default C2C exclusion; explicitly [] → user opted into everything. A
    # configured-but-all-unusable list refuses loudly (the ats location_any lesson: a
    # silently emptied restrict-intent filter is a flood, here straight into the paid eval).
    exclude_raw = d.get("employment_exclude")
    if exclude_raw is None:
        exclude_raw = ["third party"]
    employment_exclude = _ats_clean_patterns(exclude_raw, "employment_exclude",
                                             prefix="dice")
    # `_as_list`, not truthiness: an explicit [] is the opt-into-everything case, but a
    # scalar that expresses restrict-intent and cleans to nothing (employment_exclude: ""
    # or 0) is the flood this guard exists to refuse.
    if _as_list(exclude_raw) and not employment_exclude:
        print("[dice] every employment_exclude pattern was unusable — skipping (an empty "
              "filter would admit the C2C flood)", file=sys.stderr)
        record_active_fetch_attempt(
            conn, source_family="dice", target_kind="family", target_label="dice",
            definition_hash=fetch_definition_hash({"employment_exclude": exclude_raw}),
            status="failed", error_kind="parse_or_validation",
        )
        return FetchSummary.failed("ValueError")

    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    reposts = 0
    units = successes = failures = 0

    for search in cfg["searches"]:
        block = search.get("dice")
        if not block:
            continue
        name = search["name"]
        for phrase in _dice_phrases(block) or [None]:
            units += 1
            attempt_started = utc_now_iso()
            definition_hash = fetch_definition_hash({
                "source": "dice", "search_name": name, "phrase": phrase,
                "settings": {"results_pages": pages_limit, "max_days_old": max_days,
                             "employment_exclude": employment_exclude},
            })
            if phrase is None:  # the block existed but no phrase survived _dice_phrases
                failures += 1
                record_active_fetch_attempt(
                    conn, source_family="dice", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="failed",
                    error_kind="parse_or_validation", started_at=attempt_started,
                )
                print(f"[dice] {name}: dice block has no usable phrases — skipping",
                      file=sys.stderr)
                continue
            print(f"[dice] {name}: {phrase}")
            q = parse.quote(f'"{phrase}"')
            returned = eligible = query_inserted = query_reposts = 0
            detail_attempts = detail_parse_fails = detail_fetch_errors = 0
            total_results = total_pages = None
            try:
                # PHASE 1 — all network I/O, no writes. The other three fetchers finish
                # their requests before the first INSERT as a side effect of fetching whole
                # pages; Dice fetches a detail page PER ROW, so inserting as it goes would
                # hold the WAL writer lock across every remaining request and sleep —
                # minutes at the shipped defaults, against core.py's 30s busy_timeout,
                # whose comment assumes writer-vs-writer contention is brief. The local UI
                # and an overlapping run both write to this DB.
                pending = []
                seen_urls = set()
                urlless = blank_employment = 0
                for page in range(1, pages_limit + 1):
                    if total_pages is not None and page > total_pages:
                        break
                    page_jobs, page_total, page_pages = _dice_search_page(
                        _dice_get(DICE_SEARCH_URL.format(q=q, window=window, page=page)))
                    if total_results is None:
                        total_results, total_pages = page_total, page_pages
                        if total_pages is None and total_results and page_jobs:
                            # Live pages carry totalResults but no totalPages inside the
                            # envelope (probed 2026-08-10). Derive the bound from the first
                            # page's size, or the sweep requests a page past the end — and
                            # that page comes back an EMPTY envelope still advertising the
                            # full total, which is the shape of a real parse failure.
                            total_pages = -(-total_results // len(page_jobs))
                    if not page_jobs and (page_total is None or page_total > 0):
                        # A genuine no-results page parses as an envelope with totalResults
                        # 0. No envelope (None), or an envelope claiming rows that none of
                        # them parsed, means the page shape changed or a 200-status block
                        # page came back. Same rule the Greenhouse reader states: a
                        # wrong-shaped 200 must never read as an empty board, because
                        # "0 returned" then looks healthy forever.
                        why = ("carried no jobList envelope" if page_total is None
                               else f"listed {page_total} result(s) but none parsed")
                        if page == 1:
                            raise ValueError(f"Dice search page {why}")
                        # Page >= 2: page 1 already proved the shape, and its detail pages
                        # are already paid for. Failing the query here would discard that
                        # work — and since the rows are never inserted they stay unseen, so
                        # the next run pays for them again, forever.
                        print(f"[dice] {name}: page {page} {why} — keeping pages "
                              f"1..{page - 1}", file=sys.stderr)
                        break
                    if not page_jobs:
                        break
                    returned += len(page_jobs)
                    for r in page_jobs:
                        url = r["url"]
                        if not url:
                            urlless += 1
                            continue
                        if not r["employment"].strip():
                            blank_employment += 1
                        if url in seen_urls:
                            continue  # relevance ranking can repeat a row across pages
                        if any(_pattern_matches(k, r["employment"])
                               for k in employment_exclude):
                            continue
                        eligible += 1
                        seen_urls.add(url)
                        if conn.execute("SELECT 1 FROM jobs WHERE job_url=?",
                                        (url,)).fetchone():
                            continue  # known URL — never spend the detail fetch on it
                        time.sleep(delay)
                        detail_attempts += 1
                        try:
                            desc = _dice_description(_dice_get(r["detail_url"]),
                                                     s["max_description_chars"])
                        except Exception as de:  # noqa: BLE001 — one dead detail page
                            detail_fetch_errors += 1   # must not kill the query's rows
                            print(f"[dice] {name}: detail fetch failed for {url}: {de}",
                                  file=sys.stderr)
                            continue
                        if not desc:
                            # Fetched cleanly, no anchored JD. Don't insert: an empty
                            # description would reach the paid eval as a judgment on
                            # nothing. Still unseen next run, so a transient miss retries.
                            detail_parse_fails += 1
                            continue
                        pending.append((r, desc))
                    time.sleep(delay)
                with_url = returned - urlless
                if returned and not with_url:
                    # Rows parsed but none carried detailsPageUrl or guid. As a success
                    # this is indistinguishable from "the filter excluded everything", and
                    # the source then yields nothing for as long as the rename stands.
                    raise ValueError(f"none of {returned} rows carried a posting URL")
                if employment_exclude and with_url and blank_employment == with_url:
                    # employmentType is this source's flood guard: a rename silently admits
                    # the whole C2C staffing population into the DB and the paid eval. The
                    # config-side version of this already refuses loudly; so does this one.
                    raise ValueError(
                        f"none of {with_url} rows carried an employment type — "
                        f"employment_exclude cannot apply")
                if (detail_parse_fails >= _DICE_MIN_DETAIL_SAMPLE
                        and detail_parse_fails == detail_attempts):
                    # Every new URL's page was FETCHED cleanly and yielded no description:
                    # the detail-page shape changed. As a success that is byte-identical to
                    # a healthy "everything was already known" sweep. Network errors are
                    # deliberately excluded — a delisted posting stays in Dice's window and
                    # is never inserted, so counting timeouts here would re-fire every run
                    # for a week and blame the parser for a connectivity event.
                    raise ValueError(
                        f"all {detail_attempts} detail pages fetched cleanly but yielded "
                        f"no job description")
                # PHASE 2 — writes only, no network, so the job rows and their success fact
                # still commit as one transaction (the per-target health invariant).
                for r, desc in pending:
                    n, repost_of = _insert_posting(
                        conn, url=r["url"], title=r["title"], company=r["company"],
                        location=r["location"], search_name=name,
                        tier=search.get("tier", "primary"),
                        date_posted=_ats_date(r["date_posted"]),
                        first_seen=today_iso, salary_min=None, salary_max=None,
                        description=desc, source="dice",
                    )
                    query_inserted += n
                    if n and repost_of:
                        query_reposts += 1
                record_active_fetch_attempt(
                    conn, source_family="dice", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="success",
                    returned_count=returned, eligible_count=eligible,
                    inserted_count=query_inserted, repost_count=query_reposts,
                    started_at=attempt_started, commit=False,
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                failures += 1
                record_active_fetch_attempt(
                    conn, source_family="dice", target_kind="query", target_label=name,
                    definition_hash=definition_hash, status="failed",
                    error_kind=fetch_error_kind(e), started_at=attempt_started,
                )
                print(f"[dice] {name} ({phrase}) FAILED: {e}", file=sys.stderr)
                time.sleep(delay)
                continue
            successes += 1
            inserted += query_inserted
            reposts += query_reposts
            truncated = (f" of {total_results} listed" if total_results is not None
                         and total_results > returned else "")
            skipped = detail_parse_fails + detail_fetch_errors
            detail_note = (f", {skipped} skipped on missing JD "
                           f"({detail_fetch_errors} fetch error(s))" if skipped else "")
            print(f"[dice] {name} ({phrase}): {returned} returned{truncated}, "
                  f"{query_inserted} new{detail_note}")

    print(f"[dice] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    if not units:
        record_active_fetch_attempt(
            conn, source_family="dice", target_kind="family", target_label="dice",
            definition_hash=None, status="skipped", skip_reason="no configured queries",
        )
        return FetchSummary.skipped("no configured queries")
    return FetchSummary(
        inserted, units=units, successes=successes, failures=failures
    )
