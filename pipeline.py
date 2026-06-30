#!/usr/bin/env python3
"""
LinkedIn job search pipeline.

  fetch -> dedupe (SQLite) -> salary filter -> hard-requirement filters -> LLM gate
  evaluation (Claude or DeepSeek) -> daily markdown report

Usage:
  python pipeline.py run                       # full cycle: fetch + filter + evaluate + report
  python pipeline.py report                     # regenerate today's report only (no fetch, no API calls)
  python pipeline.py stats                      # quick database stats
  python pipeline.py ui                         # local web UI to triage postings (applied/passed/reject)
  python pipeline.py applied --url X            # mark a posting (full URL or unique substring) as applied-to
  python pipeline.py passed  --url X            # mark a posting as reviewed-and-passed
  python pipeline.py reject  --url X --gate G   # override the model: mark a hard-fail it missed
                                                #   (--pattern P also writes a reusable rule to filters.yaml)
  # add --undo to applied / passed / reject to clear what you set

Requires the API key for the configured provider (config.yaml): DEEPSEEK_API_KEY by default,
or ANTHROPIC_API_KEY when provider is "anthropic".
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml

# The foundation (paths, cross-cutting constants, config, the DB open/schema/migration path, the
# API-key resolver) lives in core.py; the repost/content-dedup + decision-chain core in chain.py.
# Both are re-imported into this module's namespace so the pipeline-stage code here — and every
# `pipeline.X` reference from app.py / the tests / the validation scripts — keeps working unchanged.
import chain  # noqa: E402
import core   # noqa: E402,F401
from core import (  # noqa: E402,F401
    BASE_DIR, CONFIG_PATH, PROFILE_PATH, GUIDE_PATH,
    GATE_NAMES, SCORE_DIMS, VERDICTS,
    load_config, get_db, _ensure_api_key,
)
from chain import (  # noqa: E402,F401
    _clean, _norm_company, _norm_title, _norm_location, _fingerprint, _NORM_VERSION,
    _find_repost, skip_decided_reposts, _resolve_posting, _chain_targets, _chain_members,
    _chain_decision, _decision_sig, _fmt_decision, effective_decision,
    propagate_app_status, propagate_reject, clear_reject,
    _dupe_resolve, _dupe_commit, _dupe_unlink,
)


# load_config / get_db + the schema and all migrations moved to core.py (re-imported above).


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
                print(f"[adzuna] {name} ({label}) FAILED: {e}", file=sys.stderr)
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


# Repost / content dedup (normalization, fingerprint, _find_repost) moved to chain.py;
# imported at the top of this module so call sites here read unchanged.


# -------------------------------------------------------------- salary filter

def apply_salary_filter(cfg, conn):
    """Analyst-tier rule: drop only when annual salary is KNOWN and below the floor.
    Unstated salary is kept — this is the '>80k or not mentioned' rule."""
    filtered = 0
    for search in cfg["searches"]:
        floor = search.get("min_salary")
        if not floor:
            continue
        rows = conn.execute(
            "SELECT job_url, salary_min, salary_max FROM jobs WHERE search_name=? AND status='new'",
            (search["name"],),
        ).fetchall()
        for r in rows:
            known = r["salary_max"] or r["salary_min"]
            if known is not None and known < floor:
                conn.execute(
                    "UPDATE jobs SET status='salary_filtered' WHERE job_url=?", (r["job_url"],)
                )
                filtered += 1
    conn.commit()
    if filtered:
        print(f"[salary] {filtered} postings below floor, filtered")


# ----------------------------------------------------- hard-requirement filters
#
# DeepSeek Flash (the cheap default evaluator) under-filters by design — some postings
# that miss a hard requirement (clearance, citizenship, 10+ years) slip through as PASS.
# These deterministic, user-maintained rules catch them BEFORE the paid eval: zero cost,
# instant, fully predictable. The companion `reject` command writes rules here as you
# spot misses. Mirrors apply_salary_filter — a deterministic pre-eval hard rule.

FILTERS_PATH = BASE_DIR / "filters.yaml"


def load_filters():
    """Read filters.yaml → list of rule dicts. Returns [] if the file is absent or empty."""
    if not FILTERS_PATH.exists():
        return []
    with open(FILTERS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("hard_filters") or []


def save_filters(rules):
    """Write the ruleset back. filters.yaml is tool-owned, so normalizing on write is fine."""
    with open(FILTERS_PATH, "w", encoding="utf-8") as f:
        f.write("# Hard-requirement filters — postings matching a pattern are auto-failed\n")
        f.write("# before evaluation. A pattern is a case-insensitive substring unless it is\n")
        f.write("# prefixed `re:`, which makes it a regex. Managed by `pipeline.py reject`;\n")
        f.write("# safe to hand-edit. See README.\n")
        yaml.safe_dump({"hard_filters": rules}, f, sort_keys=False, allow_unicode=True)


def _pattern_matches(pattern, text):
    """True if `pattern` matches `text`. `re:`-prefixed patterns are case-insensitive
    regexes; everything else is a case-insensitive substring."""
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def _rule_hit(rule, text):
    """Return the first pattern in `rule` that matches `text`, or None."""
    for pat in rule.get("any") or []:
        if _pattern_matches(pat, text):
            return pat
    return None


def apply_hard_filters(cfg, conn):
    """Auto-fail new postings that match a user-maintained hard-requirement rule, before
    the paid eval. Matched rows get status='rule_filtered' (skipped by evaluate_new_jobs)."""
    rules = load_filters()
    if not rules:
        return
    rows = conn.execute(
        "SELECT job_url, title, description FROM jobs WHERE status='new'"
    ).fetchall()
    today = date.today().isoformat()
    filtered = 0
    for r in rows:
        text = f"{r['title'] or ''}\n{r['description'] or ''}"
        # Rules are tried in file order; the FIRST match wins and records its gate. If a
        # posting could match several rules, reorder filters.yaml to control attribution.
        for rule in rules:
            if _rule_hit(rule, text):
                conn.execute(
                    "UPDATE jobs SET status='rule_filtered', verdict='GATE_FAIL', "
                    "failed_gate=?, filter_source=?, filter_gate=?, filter_date=? WHERE job_url=?",
                    (rule.get("gate", "other"), "rule:" + rule.get("name", "?"),
                     rule.get("gate", "other"), today, r["job_url"]),
                )
                filtered += 1
                break
    conn.commit()
    if filtered:
        print(f"[filter] {filtered} postings auto-failed by hard rules (eval skipped, cost saved)")


# skip_decided_reposts (the deterministic pre-eval repost-skip pass) moved to chain.py.


# ----------------------------------------------------------------- evaluation

SYSTEM_TEMPLATE = """You are a strict job-posting evaluator for one specific candidate. \
Apply the evaluation guide below EXACTLY: run the six hard gates first; if ANY gate fails, \
stop — do not score fit. Only score fit (0-18) if all gates pass. Be conservative: \
a title can say "Solutions Architect" and still fail role substance if the work is \
research-coded (model training/tuning, evals/benchmarks, published work). Willingness to \
learn never converts a stated core tool requirement with years attached into a pass.

CRITICAL — the two AI lines are SEPARATE and must not be merged (this is the 50/0 fix):
- ai_applied_vs_research = is the ROLE applied/delivery AI, not research? The candidate's \
artifact passes this cleanly, so this is usually 2-3 unless the work is genuinely research-coded.
- ai_artifact_depth = does the candidate's CURRENT shipped artifact (low-code AI Builder + \
Power Automate classification/extraction) evidence the AI depth this role lists as REQUIRED? \
3 = exactly that low-code/prompt/classification shape. 1-2 = a step beyond (light agent/orchestration). \
0 = a generation ahead (production agentic systems, multi-agent orchestration, LangChain/CrewAI/ \
LangGraph/MCP as a *built* requirement, SDK/connector/middleware engineering).

DISAMBIGUATION — agentic depth gap is NOT a tool_requirement gate fail:
Do NOT fail the tool_requirement gate merely because the role requires production agentic / \
multi-agent / orchestration depth beyond the candidate's low-code artifact. That depth is \
BUILDABLE — it clears the gate, then ai_artifact_depth scores 0 and the verdict caps to \
RECRUITER_ONLY (bucket 1). Reserve a tool_requirement FAIL for a *specific named tool or \
platform with years attached* that is genuinely non-rampable and disqualifying (e.g. "6+ yrs \
Salesforce Apex"), NOT for "the required AI depth is ahead of what I've shipped." A role that \
is built ON agentic systems is the canonical Bucket-1 / RECRUITER_ONLY case, not a gate fail.

VERDICT + BUCKET ROUTING (after all gates pass):
- ai_artifact_depth == 0  -> verdict "RECRUITER_ONLY", bucket 1. This is a HARD CAP: it holds \
even if the total is 16-18 and every other line is strong. Never "PASS" a depth-0 role.
- Acceptable-tier BI/BA with a small title gap -> verdict "PASS", bucket 2.
- Clean low-code / Power Platform AI delivery (ai_artifact_depth == 3) -> verdict "PASS", bucket 3.
- A gate failed -> verdict "GATE_FAIL", bucket null, fit_score null.

=== CANDIDATE PROFILE ===
{profile}

=== EVALUATION GUIDE ===
{guide}

=== OUTPUT FORMAT ===
Respond with ONLY a JSON object, no markdown fences, no preamble:
{{
  "verdict": "PASS" or "GATE_FAIL" or "RECRUITER_ONLY",
  "failed_gate": null or one of ["years_floor","domain_requirement","role_substance","tool_requirement","work_auth","employment_type"],
  "gate_notes": "one short sentence on the decisive gate finding",
  "fit_score": null or integer 0-18 (set whenever gates pass — i.e. for PASS and RECRUITER_ONLY),
  "score_breakdown": null or {{"ai_applied_vs_research": 0-3, "ai_artifact_depth": 0-3, "learning_value": 0-3, "technical_skill_match": 0-3, "title_trajectory": 0-3, "years_vs_stated": 0-3}},
  "bucket": null or 1 or 2 or 3,
  "one_line": "one-line summary a human reads in the report",
  "flags": ["anything needing human judgment, e.g. ambiguous seniority, possible research-coding, recruiter posting with unnamed client"]
}}"""


def build_system_prompt():
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    return SYSTEM_TEMPLATE.format(profile=profile, guide=guide)


def parse_eval_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start : end + 1])


def normalize_result(result):
    """Apply the guide's hard routing rules deterministically, regardless of what
    the model returned. The artifact-depth cap is the load-bearing 50/0 fix, so it
    is enforced in code, not left to the model: any role that passes the gates but
    scores ai_artifact_depth == 0 is RECRUITER_ONLY / bucket 1, even at 18/18.
    Mutates and returns `result`."""
    verdict = result.get("verdict", "GATE_FAIL")
    if verdict not in VERDICTS:
        verdict = "GATE_FAIL"

    if verdict in ("PASS", "RECRUITER_ONLY"):
        bd = result.get("score_breakdown") or {}
        depth = bd.get("ai_artifact_depth")
        # The 50/0 cap is load-bearing and must not depend on the model emitting a
        # literal 0: the output spec allows a null/partial score_breakdown, so any depth
        # that isn't a finite number (None, missing, string, NaN/Infinity — json.loads
        # parses bare NaN/Infinity tokens) must fail closed, not slip through to bucket 2.
        valid = (isinstance(depth, (int, float)) and not isinstance(depth, bool)
                 and math.isfinite(depth))
        if not valid or depth == 0:
            verdict = "RECRUITER_ONLY"
            result["bucket"] = 1
        if not result.get("bucket"):
            # depth 3 -> clean low-code delivery (3); otherwise acceptable-tier (2)
            result["bucket"] = 3 if (valid and depth == 3) else 2
    else:  # GATE_FAIL
        result["bucket"] = None
        result["fit_score"] = None

    bucket = result.get("bucket")
    if bucket not in (1, 2, 3, None):
        result["bucket"] = None

    result["verdict"] = verdict
    return result


# _ensure_api_key (used by both the Adzuna fetch and the eval) moved to core.py (re-imported above).


# (input cache-miss, output) USD per token. DeepSeek V4 rates per the official
# card (api-docs.deepseek.com/quick_start/pricing); cache-hit input is ~$0.0028/1M
# for flash (auto-cached prefix), far below the 0.1x the tally assumes — so the
# DeepSeek cost line is a slight over-estimate, which is the safe direction.
MODEL_PRICES = {
    "claude-sonnet-4-6":          (3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5":           (1.0 / 1e6, 5.0 / 1e6),
    "claude-haiku-4-5-20251001":  (1.0 / 1e6, 5.0 / 1e6),
    "deepseek-v4-flash":          (0.14 / 1e6, 0.28 / 1e6),
    "deepseek-v4-pro":            (0.435 / 1e6, 0.87 / 1e6),
}


def _call_anthropic(client, model, system_prompt, user_msg):
    """Return (text, fresh_in_tok, out_tok, cache_read_tok, cache_write_tok)."""
    resp = client.messages.create(
        model=model,
        max_tokens=1200,
        temperature=0,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    u = resp.usage
    return (resp.content[0].text, u.input_tokens, u.output_tokens,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0)


def _call_deepseek(api_key, model, system_prompt, user_msg):
    """Return (text, fresh_in_tok, out_tok, cache_read_tok, cache_write_tok).
    V4 is a reasoning model — it spends 2-4k tokens thinking before the JSON
    answer, so max_tokens must be generous or the answer truncates to empty.
    response_format forces valid JSON (DeepSeek otherwise wraps it in prose)."""
    import httpx

    r = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model, "max_tokens": 8000, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_msg}],
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    cache_read = u.get("prompt_cache_hit_tokens", 0)  # DeepSeek auto-caches the prefix
    fresh_in = u.get("prompt_tokens", 0) - cache_read
    return (d["choices"][0]["message"]["content"], fresh_in,
            u.get("completion_tokens", 0), cache_read, 0)


def evaluate_new_jobs(cfg, conn):
    provider = cfg["settings"].get("provider", "anthropic")
    model = cfg["settings"]["model"]

    client = api_key = None
    if provider == "anthropic":
        import anthropic
        if not _ensure_api_key("ANTHROPIC_API_KEY"):
            print("[eval] ANTHROPIC_API_KEY not set — skipping evaluation", file=sys.stderr)
            return
        client = anthropic.Anthropic()
    elif provider == "deepseek":
        api_key = _ensure_api_key("DEEPSEEK_API_KEY")
        if not api_key:
            print("[eval] DEEPSEEK_API_KEY not set — skipping evaluation", file=sys.stderr)
            return
    else:
        print(f"[eval] unknown provider '{provider}' — skipping evaluation", file=sys.stderr)
        return

    # Catch the documented config footgun (provider/model out of sync) BEFORE spending: a
    # deepseek provider with a claude-* model — or vice versa — would otherwise send every
    # posting to the wrong endpoint and fail all N rows through their retries into 'error'.
    expected = {"anthropic": "claude", "deepseek": "deepseek"}.get(provider)
    if expected and not model.startswith(expected):
        print(f"[eval] provider '{provider}' expects a '{expected}-*' model but config.yaml "
              f"has model '{model}' — fix the mismatch; skipping evaluation", file=sys.stderr)
        return

    system_prompt = build_system_prompt()
    price_in, price_out = MODEL_PRICES.get(model, (0.0, 0.0))

    rows = conn.execute("SELECT * FROM jobs WHERE status='new'").fetchall()
    print(f"[eval] {len(rows)} postings to evaluate via {provider}:{model}")

    usage_in = usage_cache_write = usage_cache_read = usage_out = 0

    for r in rows:
        if not (r["description"] or "").strip():
            conn.execute("UPDATE jobs SET status='needs_manual' WHERE job_url=?", (r["job_url"],))
            conn.commit()
            continue

        user_msg = (
            f"TITLE: {r['title']}\nCOMPANY: {r['company']}\nLOCATION: {r['location']}\n"
            f"SOURCE SEARCH: {r['search_name']} (tier: {r['tier']})\n"
            f"POSTED SALARY: {r['salary_min']}–{r['salary_max']}\n\n"
            f"JOB DESCRIPTION:\n{r['description']}"
        )

        result = None
        for attempt in range(3):
            try:
                if provider == "anthropic":
                    text, tin, tout, cr, cw = _call_anthropic(client, model, system_prompt, user_msg)
                else:
                    text, tin, tout, cr, cw = _call_deepseek(api_key, model, system_prompt, user_msg)
                usage_in += tin
                usage_cache_read += cr
                usage_cache_write += cw
                usage_out += tout
                result = parse_eval_json(text)
                break
            except Exception as e:
                wait = 5 * (attempt + 1)
                print(f"[eval] attempt {attempt+1} failed ({e}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)

        if result is None:
            conn.execute("UPDATE jobs SET status='error' WHERE job_url=?", (r["job_url"],))
        else:
            normalize_result(result)
            verdict = result["verdict"]
            failed_gate = result.get("failed_gate")
            if failed_gate and failed_gate not in GATE_NAMES:
                failed_gate = "other"
            conn.execute(
                """UPDATE jobs SET status='evaluated', verdict=?, failed_gate=?,
                   fit_score=?, bucket=?, eval_json=? WHERE job_url=?""",
                (
                    verdict,
                    failed_gate,
                    result.get("fit_score"),
                    result.get("bucket"),
                    json.dumps(result, ensure_ascii=False),
                    r["job_url"],
                ),
            )
        conn.commit()
        time.sleep(1)

    cost = (
        (usage_in + usage_cache_read * 0.1 + usage_cache_write * 1.25) * price_in
        + usage_out * price_out
    )
    print(
        f"[eval] done | tokens: {usage_in} in, {usage_cache_read} cache-read, "
        f"{usage_cache_write} cache-write, {usage_out} out | est. cost ${cost:.2f}"
    )


# --------------------------------------------------------------------- report

def generate_report(cfg, conn, for_date=None):
    d = for_date or date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE substr(first_seen,1,10)=? ORDER BY fit_score DESC", (d,)
    ).fetchall()

    # Hard-fail overrides (your rules + manual rejects) are pulled out first so they don't
    # also appear under their model verdict (a manual reject keeps its original PASS verdict).
    hard_filtered = [r for r in rows if r["filter_source"]]
    passes = [r for r in rows if r["verdict"] == "PASS" and not r["filter_source"]]
    recruiter = [r for r in rows if r["verdict"] == "RECRUITER_ONLY" and not r["filter_source"]]
    fails = [r for r in rows if r["verdict"] == "GATE_FAIL" and not r["filter_source"]]
    manual = [r for r in rows if r["status"] == "needs_manual" and not r["filter_source"]]
    errors = [r for r in rows if r["status"] == "error"]
    salary_filtered = [r for r in rows if r["status"] == "salary_filtered"]
    repost_skipped = [r for r in rows if r["status"] == "repost_decided"]

    reposts = [r for r in rows if r["repost_of"]]
    repost_status = [_repost_info(conn, r)[1] for r in reposts]
    applied_reposts = sum(s == "applied" for s in repost_status)
    passed_reposts = sum(s == "passed" for s in repost_status)

    lines = [f"# Job Pipeline Report — {d}", ""]
    lines.append(
        f"**{len(rows)} new postings** | {len(passes)} cold-apply (PASS) | "
        f"{len(recruiter)} recruiter-only | {len(fails)} gate fails | "
        f"{len(manual)} need manual review | {len(salary_filtered)} salary-filtered | "
        f"{len(hard_filtered)} hard-filtered | {len(repost_skipped)} repost-skipped | {len(errors)} errors"
    )
    if reposts:
        n = len(reposts)
        extra = []
        if applied_reposts:
            extra.append(f"🚫 {applied_reposts} ALREADY APPLIED")
        if passed_reposts:
            extra.append(f"↩ {passed_reposts} previously passed")
        lines.append(
            f"↻ **{n} repost{'s' if n != 1 else ''}** of roles already seen"
            + (" · " + " · ".join(extra) if extra else "")
        )
    lines.append("")

    lines.append("## ✅ Cold-apply (PASS) — worth your read (triage, not verdict)")
    lines.append("")
    if not passes:
        lines.append("*None today.*")
    for r in passes:
        lines.extend(_render_scored_job(r, conn))

    if recruiter:
        lines.append("## 🤝 Recruiter-only — route to a human, do NOT cold-apply")
        lines.append("")
        lines.append(
            "*Passed every gate and scored well, but the artifact is a generation behind the "
            "role's **required** AI depth (artifact-depth 0). An ATS screen filters these out; "
            "a recruiter or referral can carry the ramp narrative. This is the 50/0 fix.*"
        )
        lines.append("")
        for r in recruiter:
            lines.extend(_render_scored_job(r, conn))

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(conn, r)}{_source_tag(r)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)}{_source_tag(r)}: `{r['failed_gate']}` — "
            f"{ev.get('gate_notes', '')} · [link]({r['job_url']})"
        )
    lines.append("")

    if hard_filtered:
        lines.append("## 🚫 Hard-fail filters (your rules + manual rejects)")
        lines.append("")
        lines.append(
            "*Auto-failed by a `filters.yaml` rule or rejected by you — overrides the model. "
            "Skim to catch an over-aggressive rule.*"
        )
        lines.append("")
        for r in hard_filtered:
            src = r["filter_source"] or ""
            tag = f"`rule: {src[5:]}`" if src.startswith("rule:") else "`manual`"
            note = " (model under-filtered)" if (src == "manual" and r["verdict"] in ("PASS", "RECRUITER_ONLY")) else ""
            # _repost_tag keeps the ALREADY APPLIED / passed / repost marker visible here too,
            # so a rule can't silently bury a relisting of a role you already applied to.
            lines.append(
                f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)}{_source_tag(r)} · {tag} · "
                f"gate `{r['filter_gate']}`{note} · [link]({r['job_url']})"
            )
        lines.append("")

    if errors:
        lines.append("## ⚠️ Evaluation errors (re-run `python pipeline.py run` to retry is NOT automatic — check log)")
        for r in errors:
            lines.append(f"- {r['title']} — {r['company']} · [link]({r['job_url']})")
        lines.append("")

    out_dir = BASE_DIR / cfg["settings"]["reports_dir"]
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"report_{d}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] written: {out_path}")


def _repost_info(conn, r):
    """For a posting, return (banner_lines, effective_status) for the report. Both come from
    chain.effective_decision — the single source of truth for a chain's decision, shared with the
    web UI and the dupe guard — so this function only FORMATS them into markdown. `effective_status`
    is 'applied', 'passed', or None (applied outranks passed across the chain); `banner_lines` are
    the matching markdown lines (loud for applied, quiet for passed) plus the repost note."""
    dec = effective_decision(conn, r)
    status = dec["app_status"]
    lines = []
    if status == "applied":
        lines.append(f"- 🚫 **ALREADY APPLIED** ({dec['status_date']}) — do not re-apply")
    elif status == "passed":
        lines.append(f"- ↩ You reviewed & passed on {dec['status_date']} — skip unless reconsidering")
    if dec["is_repost"]:
        seen = (dec["original_first_seen"] or "")[:10]
        lines.append(f"- ↻ Repost — original first seen {seen}, prior verdict {dec['original_verdict']}")
    return lines, status


def _repost_tag(conn, r):
    """Compact inline marker for one-liner sections (gate fails, manual review)."""
    lines, status = _repost_info(conn, r)
    if status == "applied":
        return " · 🚫 **ALREADY APPLIED**"
    if status == "passed":
        return " · ↩ passed"
    return " · ↻ repost" if r["repost_of"] else ""


def _source_tag(r):
    """Flag postings from a thin-data source so the verdict's context is visible. Adzuna only
    gives a 500-char snippet, so its evals are made on far less text than a LinkedIn JD."""
    # Tolerate rows from a SELECT that omits `source` (this file mixes Row and dict rows).
    source = r["source"] if "source" in r.keys() else None
    if source == "adzuna":
        return " · 📋 adzuna (500-char snippet — verdict on thin text)"
    return ""


BUCKET_LABELS = {
    1: "Bucket 1 — required AI depth a generation ahead (recruiter/referral)",
    2: "Bucket 2 — acceptable-tier BI/BA (cold-apply where title gap is small)",
    3: "Bucket 3 — clean low-code / Power Platform AI delivery (cold-apply)",
}


def score_band(score):
    """Fit-score band label (out of 18). The single definition of the thresholds, shared by the
    report (_render_scored_job) and the web UI (app.row_to_dict) so the two can't disagree."""
    s = score or 0
    return "strong" if s >= 14 else ("acceptable" if s >= 10 else "likely pass")


def _render_scored_job(r, conn):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = score_band(score)
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band})"]
    out.extend(_repost_info(conn, r)[0])
    out.append(f"- {r['location']}  ·  tier: {r['tier']}  ·  search: `{r['search_name']}`{_source_tag(r)}")
    if r["bucket"]:
        out.append("- " + BUCKET_LABELS.get(r["bucket"], "Bucket " + str(r["bucket"])))
    if r["salary_min"] or r["salary_max"]:
        out.append(f"- Posted salary: {_fmt_sal(r['salary_min'])}–{_fmt_sal(r['salary_max'])}")
    out.append(f"- {ev.get('one_line', '')}")
    bd = ev.get("score_breakdown") or {}
    if bd:
        out.append("- Scores: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in bd.items()))
    for fl in ev.get("flags") or []:
        out.append(f"- ⚠️ {fl}")
    out.append(f"- [Posting]({r['job_url']})")
    out.append("")
    return out


def _fmt_sal(v):
    return f"${int(v):,}" if v else "?"


# ----------------------------------------------------------------------- main

def cmd_stats(conn):
    for row in conn.execute(
        "SELECT status, verdict, COUNT(*) n FROM jobs GROUP BY status, verdict ORDER BY n DESC"
    ):
        print(f"{row['status']:>16} {str(row['verdict']):>10} {row['n']:>5}")
    print("  -- application status --")
    for row in conn.execute(
        "SELECT COALESCE(app_status,'(backlog)') s, COUNT(*) n FROM jobs GROUP BY app_status ORDER BY n DESC"
    ):
        print(f"{row['s']:>16} {row['n']:>16}")
    fsrc = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL"
    ).fetchone()["n"]
    if fsrc:
        print("  -- hard-fail overrides --")
        for row in conn.execute(
            "SELECT filter_source s, COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL "
            "GROUP BY filter_source ORDER BY n DESC"
        ):
            print(f"{row['s']:>16} {row['n']:>16}")


# _resolve_posting (url → row) and _chain_targets (decision propagation set) moved to chain.py.


def cmd_mark(conn, url, status):
    """Record the user's decision on a posting: `status` is 'applied', 'passed', or None
    (undo). `url` may be a unique substring of the job_url. The decision propagates to the
    canonical original of a repost chain, so the whole group is covered."""
    label = status or "undo"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    today = date.today().isoformat()
    stamp = today if status else None
    propagate_app_status(conn, _chain_targets(conn, m), status, stamp)
    conn.commit()
    verb = f"marked {status}" if status else "cleared status"
    print(f"[{label}] {verb}: {m['title']} — {m['company']}" + (f" ({today})" if status else ""))
    return True


def cmd_reject(conn, url, gate, pattern, note, undo):
    """Manually correct the model: mark a posting as a hard-fail it missed (distinct from the
    softer `passed`). `--pattern` additionally promotes the catch into a deterministic rule in
    filters.yaml so future postings with the same requirement are auto-failed. `--undo` clears
    the override (it does not remove any rule)."""
    label = "reject"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    if gate not in GATE_NAMES + ["other"]:
        print(f"[{label}] --gate must be one of {GATE_NAMES + ['other']}", file=sys.stderr)
        return False

    today = date.today().isoformat()
    targets = _chain_targets(conn, m)
    if undo:
        clear_reject(conn, targets)
        conn.commit()
        print(f"[{label}] cleared override: {m['title']} — {m['company']}")
        return True

    # The explicitly named row is always (re)stamped — you're overruling it, possibly re-attributing
    # a row a filters.yaml rule auto-failed; siblings are stamped too but never clobber a rule:<name>.
    propagate_reject(conn, targets, gate, today, force_url=m["job_url"], overwrite_manual=True)
    conn.commit()
    print(f"[{label}] rejected (gate: {gate}): {m['title']} — {m['company']} ({today})")

    if pattern:
        _add_filter_rule(conn, gate, pattern, note, m)
    return True


def _add_filter_rule(conn, gate, pattern, note, posting):
    """Promote a pattern into filters.yaml under the rule named for `gate`. Shows the matching
    sentence from this posting and how many existing postings the pattern would also catch
    (false-positive preview) before saving. De-dupes identical patterns."""
    # False-positive preview: how many existing postings would this pattern also match?
    rows = conn.execute("SELECT title, description FROM jobs").fetchall()
    hits = sum(1 for r in rows if _pattern_matches(pattern, f"{r['title'] or ''}\n{r['description'] or ''}"))
    # Show the sentence in THIS posting that the pattern matches, to sanity-check the phrase.
    desc = posting["description"] or ""
    snippet = next(
        (s.strip() for s in re.split(r"(?<=[.!?\n])\s+", desc) if _pattern_matches(pattern, s)),
        None,
    )
    print(f"[reject] pattern {pattern!r} → would match {hits} existing posting(s) in the DB")
    if snippet:
        print(f"[reject] matched here: …{snippet[:200]}…")

    rules = load_filters()
    # Match on `gate` (not `name`) so a hand-edited rule whose name differs from its gate
    # is still extended rather than duplicated.
    rule = next((r for r in rules if r.get("gate") == gate), None)
    if rule is None:
        rule = {"name": gate, "gate": gate, "note": note or "", "any": []}
        rules.append(rule)
    elif note and not rule.get("note"):
        rule["note"] = note
    if pattern in (rule.get("any") or []):
        print(f"[reject] pattern already in rule '{gate}' — nothing to add")
        return
    rule.setdefault("any", []).append(pattern)
    save_filters(rules)
    print(f"[reject] added pattern to rule '{gate}' in {FILTERS_PATH.name} "
          f"({len(rule['any'])} pattern(s) now)")


# ------------------------------------------------------- manual repost linking
#
# The dupe cores (_chain_members, _chain_decision, _decision_sig, _fmt_decision,
# _dupe_resolve, _dupe_commit, _dupe_unlink) live in chain.py and are imported above —
# `dupe` is the manual escape hatch for a relisting `_find_repost` missed (drifted
# title/location, or the same role cross-posted to Adzuna vs LinkedIn). The CLI wrapper
# below and the web UI (app.api_dupe) share those cores so the guard logic lives in one place.

def cmd_dupe(conn, url, of_url, undo, assume_yes):
    """CLI wrapper over the shared dupe cores. Manually link two existing postings as the same role
    (a duplicate `_find_repost` missed): earliest-`first_seen` becomes canonical, the other side is
    repointed under it, and any existing decision propagates across the unified chain. `--undo`
    splits a manual link apart. Previews the merge and confirms (skippable with `assume_yes`)."""
    label = "dupe"
    if undo:
        a = _resolve_posting(conn, url, label)
        if a is None:
            return False
        ok, msg, _ = _dupe_unlink(conn, a)
        print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
        return ok

    plan, err = _dupe_resolve(conn, url, of_url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    winner, loser, dec = plan["winner"], plan["loser"], plan["dec"]

    # Preview + confirm: a wrong merge buries a real job under another role's decision.
    print(f"[{label}] link as the SAME role:")
    print(f"    canonical (kept) : {winner['title']} — {winner['company']} ({winner['first_seen']})")
    print(f"    relisting (merge): {loser['title']} — {loser['company']} ({loser['first_seen']})")
    if len(plan["loser_members"]) > 1:
        print(f"    + {len(plan['loser_members']) - 1} relisting(s) already under the merged side")
    if dec:
        print(f"    decision propagated to the whole chain: {_fmt_decision(dec)}")
    if not assume_yes and not _confirm(f"[{label}] proceed?"):
        print(f"[{label}] aborted", file=sys.stderr)
        return False

    _dupe_commit(conn, plan)
    print(f"[{label}] linked: {loser['title']} — {loser['company']} → canonical {winner['job_url']}")
    return True


def _confirm(prompt):
    """Yes/no prompt; treats a closed stdin (non-interactive) or Ctrl-C as 'no' to fail safe."""
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()  # finish the prompt line so the caller's abort message isn't appended to it
        return False


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed", "reject", "dupe", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / `reject` / `dupe`")
    ap.add_argument("--of", help="`dupe`: job_url (or unique substring) of the other posting this duplicates")
    ap.add_argument("--yes", action="store_true", help="`dupe`: skip the confirmation prompt")
    ap.add_argument("--undo", action="store_true", help="clear the status/override/link instead of setting it")
    ap.add_argument("--gate", default="other",
                    help="hard gate a `reject` represents — one of: " + ", ".join(GATE_NAMES + ["other"]))
    ap.add_argument("--pattern", help="`reject`: promote this pattern into filters.yaml (re: prefix = regex)")
    ap.add_argument("--note", help="`reject`: optional note stored with a new filter rule")
    args = ap.parse_args()

    if args.command == "ui":
        # Lazy import so the core pipeline runs without Flask installed.
        try:
            import app
        except ImportError:
            print("[ui] Flask is required — run: pip install -r requirements.txt", file=sys.stderr)
            return
        app.serve()
        return

    cfg = load_config()
    conn = get_db(cfg)

    if args.command == "run":
        # The `status` column is a state machine and THIS ORDER IS LOAD-BEARING: each stage gates
        # on status and only the deterministic, zero-cost filters run before the *paid* eval, so an
        # obvious reject never reaches the LLM. The transitions:
        #   fetch_new_jobs / fetch_adzuna  insert rows as            'new'
        #   apply_salary_filter            'new' below floor      -> 'salary_filtered'
        #   apply_hard_filters             'new' hits a rule      -> 'rule_filtered'
        #   skip_decided_reposts           'new' relisting of a decided role -> 'repost_decided'
        #   evaluate_new_jobs              remaining 'new'        -> 'evaluated' | 'needs_manual' | 'error'
        # A new pre-eval filter must mirror this: set a non-'new' status so evaluate_new_jobs skips it.
        fetch_new_jobs(cfg, conn)
        fetch_adzuna(cfg, conn)
        apply_salary_filter(cfg, conn)
        apply_hard_filters(cfg, conn)
        skip_decided_reposts(conn)
        evaluate_new_jobs(cfg, conn)
        generate_report(cfg, conn, args.date)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)
    elif args.command in ("applied", "passed"):
        cmd_mark(conn, args.url, None if args.undo else args.command)
    elif args.command == "reject":
        cmd_reject(conn, args.url, args.gate, args.pattern, args.note, args.undo)
    elif args.command == "dupe":
        cmd_dupe(conn, args.url, args.of, args.undo, args.yes)


if __name__ == "__main__":
    main()
