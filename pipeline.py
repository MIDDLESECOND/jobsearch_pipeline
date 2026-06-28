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
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
PROFILE_PATH = BASE_DIR / "profile.md"
GUIDE_PATH = BASE_DIR / "evaluation_guide.md"

GATE_NAMES = ["years_floor", "domain_requirement", "role_substance", "tool_requirement", "work_auth", "employment_type"]
SCORE_DIMS = ["ai_applied_vs_research", "ai_artifact_depth", "learning_value",
              "technical_skill_match", "title_trajectory", "years_vs_stated"]
VERDICTS = ["PASS", "GATE_FAIL", "RECRUITER_ONLY"]


# ---------------------------------------------------------------- config / db

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db(cfg):
    conn = sqlite3.connect(BASE_DIR / cfg["settings"]["db_path"])
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_url      TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            location     TEXT,
            search_name  TEXT,
            tier         TEXT,
            date_posted  TEXT,
            first_seen   TEXT,
            salary_min   REAL,
            salary_max   REAL,
            description  TEXT,
            status       TEXT,   -- new | evaluated | needs_manual | salary_filtered | error
            verdict      TEXT,   -- PASS | GATE_FAIL | RECRUITER_ONLY
            failed_gate  TEXT,
            fit_score    INTEGER,
            bucket       INTEGER, -- 1 | 2 | 3 (channel routing; null for gate fails)
            eval_json    TEXT,
            norm_company TEXT,    -- normalized company (suffix-stripped) for repost matching
            norm_title   TEXT,    -- normalized title (abbrevs expanded) for fuzzy matching
            fingerprint  TEXT,    -- blocking key: norm_company|norm_location
            repost_of    TEXT,    -- job_url of the canonical original if this is a repost
            app_status   TEXT,    -- NULL (backlog) | applied | passed  (user's decision)
            status_date  TEXT,    -- date app_status was set
            filter_source TEXT,   -- NULL | manual | rule:<name>  (hard-fail override)
            filter_gate  TEXT,    -- which gate the override represents
            filter_date  TEXT     -- date the override was set
        )
    """)
    _migrate(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)")
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an existing DB up to the current schema. Idempotent — safe to run
    every startup. Added for the v2 guide: the `bucket` column (channel routing).
    Repost dedup (v3): content fingerprint + application-status tracking columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "bucket" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN bucket INTEGER")
        print("[migrate] added column jobs.bucket")
    new_cols = [
        ("norm_company", "TEXT"),
        ("norm_title", "TEXT"),
        ("fingerprint", "TEXT"),
        ("repost_of", "TEXT"),
        ("app_status", "TEXT"),   # NULL | applied | passed
        ("status_date", "TEXT"),
        ("filter_source", "TEXT"),  # NULL | manual | rule:<name>
        ("filter_gate", "TEXT"),
        ("filter_date", "TEXT"),
    ]
    added = False
    for col, decl in new_cols:
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            print(f"[migrate] added column jobs.{col}")
            added = True
    if added:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)")
    conn.commit()
    _migrate_applied_to_status(conn, cols)
    _backfill_fingerprints(conn)


def _migrate_applied_to_status(conn, cols):
    """v3.1: the binary `applied`/`applied_date` columns became the `app_status`
    lifecycle (NULL | applied | passed). Fold the old flag into the new column, then
    drop the dead columns. `DROP COLUMN` needs SQLite >= 3.35; if older, the columns
    are left in place (harmless — nothing reads them)."""
    if "applied" not in cols:
        return
    conn.execute(
        "UPDATE jobs SET app_status='applied', status_date=applied_date "
        "WHERE applied=1 AND app_status IS NULL"
    )
    conn.commit()
    print("[migrate] folded jobs.applied into jobs.app_status")
    for dead in ("applied", "applied_date"):
        try:
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {dead}")
            print(f"[migrate] dropped column jobs.{dead}")
        except sqlite3.OperationalError:
            pass  # SQLite < 3.35: leave it, it's unused
    conn.commit()


def _backfill_fingerprints(conn):
    """Populate norm_company / norm_title / fingerprint for rows that predate the
    repost-dedup columns, so historical postings participate in repost detection.
    One-time: only touches rows where fingerprint is still NULL."""
    rows = conn.execute(
        "SELECT job_url, company, title, location FROM jobs WHERE fingerprint IS NULL"
    ).fetchall()
    if not rows:
        return
    for r in rows:
        conn.execute(
            "UPDATE jobs SET norm_company=?, norm_title=?, fingerprint=? WHERE job_url=?",
            (
                _norm_company(r["company"]),
                _norm_title(r["title"]),
                _fingerprint(r["company"], r["location"]),
                r["job_url"],
            ),
        )
    conn.commit()
    print(f"[migrate] backfilled fingerprints for {len(rows)} existing rows")


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
                    norm_company, norm_title, fingerprint, repost_of)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?)""",
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


# ------------------------------------------------------- repost / content dedup
#
# LinkedIn mints a fresh job_url every time a role is reposted, so URL-level
# dedup (the INSERT OR IGNORE on the PRIMARY KEY) misses relistings. These
# helpers add a content fingerprint: postings with the same normalized
# company+location AND the same normalized title are treated as the same role
# across URL churn — guarding against a double-apply.
#
# Matching is EXACT on the normalized title, not fuzzy. A backtest over the real
# DB (2,677 rows) showed fuzzy title matching collapsing distinct roles that share
# a generic core — 'Workday Business Analyst' vs 'SalesForce Business Analyst',
# 'Legal Engineer (Corporate)' vs '(In-House)' — into false reposts. The cost is
# asymmetric the wrong way: a false "ALREADY APPLIED" banner on a genuinely new
# role makes you SKIP a job you should apply to. Real reposts keep the title
# verbatim; a different qualifier means a different role. Normalization (case,
# punctuation, company suffixes, Sr/Jr→Senior/Junior) absorbs the noise that
# isn't role-distinguishing; exact match on the result is both safe and accurate.

_COMPANY_SUFFIXES = re.compile(
    r"\b(?:llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|limited|co|company|"
    r"plc|gmbh|llp|lp|holdings|group)\b\.?",
    re.IGNORECASE,
)
_TITLE_ABBREVS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mgr": "manager",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "ml": "machine learning",
    "ai": "ai",  # kept as-is, listed for clarity
}


def _clean(s):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    if not isinstance(s, str):
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_company(s):
    s = _clean(_COMPANY_SUFFIXES.sub(" ", s or ""))
    return s


def _norm_title(s):
    toks = _clean(s).split()
    expanded = []
    for t in toks:
        expanded.append(_TITLE_ABBREVS.get(t, t))
    return " ".join(expanded).strip()


def _norm_location(s):
    s = _clean(s)
    # Drop a trailing country qualifier so "Austin, TX, United States" and
    # "Austin, TX" share a fingerprint.
    s = re.sub(r"\b(?:united states|usa|us)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _fingerprint(company, location):
    return f"{_norm_company(company)}|{_norm_location(location)}"


def _find_repost(conn, fingerprint, norm_title, exclude_url=None):
    """Return the canonical job_url of an earlier posting for the same role, or None.
    A match requires the same fingerprint (normalized company+location) AND an exact
    normalized-title match. The canonical url is the earliest match's own repost_of
    when set, so every relisting in a chain points at the single first posting."""
    if not fingerprint or not norm_title:
        return None
    rows = conn.execute(
        "SELECT job_url, repost_of FROM jobs "
        "WHERE fingerprint=? AND norm_title=? ORDER BY first_seen ASC",
        (fingerprint, norm_title),
    ).fetchall()
    for r in rows:
        if exclude_url and r["job_url"] == exclude_url:
            continue
        return r["repost_of"] or r["job_url"]
    return None


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
        if bd.get("ai_artifact_depth") == 0:
            verdict = "RECRUITER_ONLY"
            result["bucket"] = 1
        if not result.get("bucket"):
            # depth 3 -> clean low-code delivery (3); otherwise acceptable-tier (2)
            result["bucket"] = 3 if bd.get("ai_artifact_depth") == 3 else 2
    else:  # GATE_FAIL
        result["bucket"] = None
        result["fit_score"] = None

    bucket = result.get("bucket")
    if bucket not in (1, 2, 3, None):
        result["bucket"] = None

    result["verdict"] = verdict
    return result


def _ensure_api_key(var="ANTHROPIC_API_KEY"):
    """Return the named API key, self-healing the common Windows case where the
    key was set with `setx` but the current shell was opened before that and so
    never inherited it. Falls back to the persistent HKCU user environment."""
    key = os.environ.get(var)
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val, _ = winreg.QueryValueEx(k, var)
            if val:
                os.environ[var] = val
                print(f"[eval] loaded {var} from persistent user environment")
                return val
        except (OSError, FileNotFoundError):
            pass
    return None


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

    reposts = [r for r in rows if r["repost_of"]]
    repost_status = [_repost_info(conn, r)[1] for r in reposts]
    applied_reposts = sum(s == "applied" for s in repost_status)
    passed_reposts = sum(s == "passed" for s in repost_status)

    lines = [f"# Job Pipeline Report — {d}", ""]
    lines.append(
        f"**{len(rows)} new postings** | {len(passes)} cold-apply (PASS) | "
        f"{len(recruiter)} recruiter-only | {len(fails)} gate fails | "
        f"{len(manual)} need manual review | {len(salary_filtered)} salary-filtered | "
        f"{len(hard_filtered)} hard-filtered | {len(errors)} errors"
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
                f"- {r['title']} — {r['company']} ({r['location']}){_repost_tag(conn, r)} · [link]({r['job_url']})"
            )
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)}: `{r['failed_gate']}` — "
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
                f"- **{r['title']} — {r['company']}**{_repost_tag(conn, r)} · {tag} · "
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
    """For a posting, return (banner_lines, effective_status). `effective_status` is the
    user's decision across the whole repost chain — 'applied', 'passed', or None — with
    `applied` outranking `passed`. The row's own status counts too, so re-running `report`
    after marking a same-day posting reflects it immediately. `banner_lines` are the
    matching markdown lines (loud for applied, quiet for passed) plus the repost note."""
    canonical = r["repost_of"] or r["job_url"]
    # Every row in the chain: the canonical original plus anything pointing at it
    # (includes r itself, whether r is the original or a relisting).
    group = conn.execute(
        "SELECT job_url, first_seen, verdict, app_status, status_date FROM jobs "
        "WHERE job_url=? OR repost_of=? ORDER BY first_seen ASC",
        (canonical, canonical),
    ).fetchall()
    applied_row = next((g for g in group if g["app_status"] == "applied"), None)
    passed_row = next((g for g in group if g["app_status"] == "passed"), None)

    lines = []
    if applied_row:
        status = "applied"
        lines.append(f"- 🚫 **ALREADY APPLIED** ({applied_row['status_date']}) — do not re-apply")
    elif passed_row:
        status = "passed"
        lines.append(f"- ↩ You reviewed & passed on {passed_row['status_date']} — skip unless reconsidering")
    else:
        status = None

    if r["repost_of"]:
        orig = next((g for g in group if g["job_url"] == canonical), None)
        if orig:
            seen = (orig["first_seen"] or "")[:10]
            lines.append(f"- ↻ Repost — original first seen {seen}, prior verdict {orig['verdict']}")
        else:
            lines.append("- ↻ Repost of a previously seen posting")
    return lines, status


def _repost_tag(conn, r):
    """Compact inline marker for one-liner sections (gate fails, manual review)."""
    lines, status = _repost_info(conn, r)
    if status == "applied":
        return " · 🚫 **ALREADY APPLIED**"
    if status == "passed":
        return " · ↩ passed"
    return " · ↻ repost" if r["repost_of"] else ""


BUCKET_LABELS = {
    1: "Bucket 1 — required AI depth a generation ahead (recruiter/referral)",
    2: "Bucket 2 — acceptable-tier BI/BA (cold-apply where title gap is small)",
    3: "Bucket 3 — clean low-code / Power Platform AI delivery (cold-apply)",
}


def _render_scored_job(r, conn):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = "strong" if (score or 0) >= 14 else ("acceptable" if (score or 0) >= 10 else "likely pass")
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band})"]
    out.extend(_repost_info(conn, r)[0])
    out.append(f"- {r['location']}  ·  tier: {r['tier']}  ·  search: `{r['search_name']}`")
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


def _resolve_posting(conn, url, label):
    """Resolve a --url (full or unique substring) to a single jobs row, or None. Prints a
    helpful message on no-match / ambiguity. Shared by the `applied`/`passed`/`reject`
    commands so they behave identically."""
    if not url:
        print(f"[{label}] provide --url (full or unique substring of the job_url)", file=sys.stderr)
        return None
    # Escape LIKE metacharacters so a substring containing % or _ matches literally
    # (the resolved row drives a destructive UPDATE, so a mis-match must not happen).
    safe = url.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    matches = conn.execute(
        "SELECT * FROM jobs WHERE job_url LIKE ? ESCAPE '\\'", (f"%{safe}%",)
    ).fetchall()
    if not matches:
        print(f"[{label}] no posting matches '{url}'", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"[{label}] '{url}' is ambiguous ({len(matches)} matches):", file=sys.stderr)
        for m in matches:
            print(f"    {m['title']} — {m['company']}  {m['job_url']}", file=sys.stderr)
        return None
    return matches[0]


def _chain_targets(m):
    """The set of job_urls a per-posting decision should apply to: this posting plus the
    canonical original of its repost chain, so a decision follows the role across relistings."""
    return {m["job_url"], m["repost_of"]} - {None}


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
    for t in _chain_targets(m):
        conn.execute(
            "UPDATE jobs SET app_status=?, status_date=? WHERE job_url=?", (status, stamp, t)
        )
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
    if undo:
        for t in _chain_targets(m):
            conn.execute(
                "UPDATE jobs SET filter_source=NULL, filter_gate=NULL, filter_date=NULL WHERE job_url=?",
                (t,),
            )
        conn.commit()
        print(f"[{label}] cleared override: {m['title']} — {m['company']}")
        return True

    for t in _chain_targets(m):
        # Also lift a still-'new' row out of status='new' so it isn't sent to the paid
        # evaluator on the next run — you've already overruled it. Already-evaluated rows
        # keep their status (the report groups them by filter_source either way).
        conn.execute(
            "UPDATE jobs SET filter_source='manual', filter_gate=?, filter_date=?, "
            "status=CASE WHEN status='new' THEN 'rule_filtered' ELSE status END WHERE job_url=?",
            (gate, today, t),
        )
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


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed", "reject", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / `reject`")
    ap.add_argument("--undo", action="store_true", help="clear the status/override instead of setting it")
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
        fetch_new_jobs(cfg, conn)
        apply_salary_filter(cfg, conn)
        apply_hard_filters(cfg, conn)
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


if __name__ == "__main__":
    main()
