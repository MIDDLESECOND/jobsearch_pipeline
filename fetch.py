#!/usr/bin/env python3
"""The two posting sources: the LinkedIn scrape (python-jobspy guest endpoints) and the Adzuna
REST API. Both insert unseen postings as status='new' and are otherwise source-agnostic from then
on — the `source` column is provenance only. Imports core (the API-key resolver) and chain (the
fingerprint/repost helpers); nothing depends back on this module except pipeline's `run`.
"""

import json
import sys
import time
from datetime import datetime

from core import _ensure_api_key
from chain import _norm_company, _norm_title, _fingerprint, _find_repost


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
            norm_company = _norm_company(company)
            norm_title = _norm_title(title)
            fingerprint = _fingerprint(company, location)
            repost_of = _find_repost(conn, fingerprint, norm_title, exclude_url=url)
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_url, title, company, location, search_name, tier, date_posted,
                    first_seen, salary_min, salary_max, description, status,
                    norm_company, norm_title, fingerprint, repost_of, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,'linkedin')""",
                (
                    url,
                    title,
                    company,
                    location,
                    name,
                    search.get("tier", "primary"),
                    str(row.get("date_posted") or ""),
                    today_iso,
                    _num(row.get("min_amount")),
                    _num(row.get("max_amount")),
                    desc[: s["max_description_chars"]],
                    norm_company,
                    norm_title,
                    fingerprint,
                    repost_of,
                ),
            )
            inserted += cur.rowcount
            if cur.rowcount and repost_of:
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
    ADZUNA_APP_ID / ADZUNA_APP_KEY credentials are absent, so `run` still works LinkedIn-only."""
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
        queries = block if isinstance(block, list) else [block]
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
                salary_min = None if predicted else _num(r.get("salary_min"))
                salary_max = None if predicted else _num(r.get("salary_max"))
                norm_company = _norm_company(company)
                norm_title = _norm_title(title)
                fingerprint = _fingerprint(company, location)
                repost_of = _find_repost(conn, fingerprint, norm_title, exclude_url=url)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO jobs
                       (job_url, title, company, location, search_name, tier, date_posted,
                        first_seen, salary_min, salary_max, description, status,
                        norm_company, norm_title, fingerprint, repost_of, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,'adzuna')""",
                    (
                        url,
                        title,
                        company,
                        location,
                        name,
                        search.get("tier", "primary"),
                        str(r.get("created") or ""),
                        today_iso,
                        salary_min,
                        salary_max,
                        desc[: s["max_description_chars"]],
                        norm_company,
                        norm_title,
                        fingerprint,
                        repost_of,
                    ),
                )
                inserted += cur.rowcount
                if cur.rowcount and repost_of:
                    reposts += 1
            conn.commit()
            print(f"[adzuna] {name} ({label}): {len(results)} returned")
            time.sleep(delay)

    print(f"[adzuna] {inserted} new postings inserted ({reposts} reposts of seen roles)")
    return inserted
