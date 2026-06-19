#!/usr/bin/env python3
"""
LinkedIn job search pipeline.

  fetch -> dedupe (SQLite) -> salary filter -> Claude gate evaluation -> daily markdown report

Usage:
  python pipeline.py run       # full cycle: fetch + evaluate + regenerate today's report
  python pipeline.py report    # regenerate today's report only (no fetch, no API calls)
  python pipeline.py stats     # quick database stats

Requires env var ANTHROPIC_API_KEY.
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
            eval_json    TEXT
        )
    """)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an existing DB up to the current schema. Idempotent — safe to run
    every startup. Added for the v2 guide: the `bucket` column (channel routing)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "bucket" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN bucket INTEGER")
        print("[migrate] added column jobs.bucket")
    conn.commit()


# ---------------------------------------------------------------------- fetch

def fetch_new_jobs(cfg, conn):
    """Run every configured search; insert unseen postings as status='new'."""
    from jobspy import scrape_jobs  # imported here so `report` works even if jobspy breaks

    s = cfg["settings"]
    today_iso = datetime.now().isoformat(timespec="seconds")
    inserted = 0

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
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_url, title, company, location, search_name, tier, date_posted,
                    first_seen, salary_min, salary_max, description, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'new')""",
                (
                    url,
                    row.get("title"),
                    row.get("company"),
                    row.get("location"),
                    name,
                    search.get("tier", "primary"),
                    str(row.get("date_posted") or ""),
                    today_iso,
                    _num(row.get("min_amount")),
                    _num(row.get("max_amount")),
                    desc[: s["max_description_chars"]],
                ),
            )
            inserted += cur.rowcount

        conn.commit()
        print(f"[fetch] {name}: {len(df)} returned")
        time.sleep(s["delay_between_searches"])

    print(f"[fetch] {inserted} new postings inserted")
    return inserted


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
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

    passes = [r for r in rows if r["verdict"] == "PASS"]
    recruiter = [r for r in rows if r["verdict"] == "RECRUITER_ONLY"]
    fails = [r for r in rows if r["verdict"] == "GATE_FAIL"]
    manual = [r for r in rows if r["status"] == "needs_manual"]
    errors = [r for r in rows if r["status"] == "error"]
    salary_filtered = [r for r in rows if r["status"] == "salary_filtered"]

    lines = [f"# Job Pipeline Report — {d}", ""]
    lines.append(
        f"**{len(rows)} new postings** | {len(passes)} cold-apply (PASS) | "
        f"{len(recruiter)} recruiter-only | {len(fails)} gate fails | "
        f"{len(manual)} need manual review | {len(salary_filtered)} salary-filtered | {len(errors)} errors"
    )
    lines.append("")

    lines.append("## ✅ Cold-apply (PASS) — worth your read (triage, not verdict)")
    lines.append("")
    if not passes:
        lines.append("*None today.*")
    for r in passes:
        lines.extend(_render_scored_job(r))

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
            lines.extend(_render_scored_job(r))

    if manual:
        lines.append("## 👀 Needs manual review (no description retrieved)")
        lines.append("")
        for r in manual:
            lines.append(f"- {r['title']} — {r['company']} ({r['location']}) · [link]({r['job_url']})")
        lines.append("")

    lines.append("## ❌ Gate fails")
    lines.append("")
    if not fails:
        lines.append("*None today.*")
    for r in fails:
        ev = json.loads(r["eval_json"] or "{}")
        lines.append(
            f"- **{r['title']} — {r['company']}**: `{r['failed_gate']}` — "
            f"{ev.get('gate_notes', '')} · [link]({r['job_url']})"
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


BUCKET_LABELS = {
    1: "Bucket 1 — required AI depth a generation ahead (recruiter/referral)",
    2: "Bucket 2 — acceptable-tier BI/BA (cold-apply where title gap is small)",
    3: "Bucket 3 — clean low-code / Power Platform AI delivery (cold-apply)",
}


def _render_scored_job(r):
    """Render one gates-passed job (PASS or RECRUITER_ONLY) as report lines."""
    ev = json.loads(r["eval_json"] or "{}")
    score = r["fit_score"]
    band = "strong" if (score or 0) >= 14 else ("acceptable" if (score or 0) >= 10 else "likely pass")
    out = [f"### {r['title']} — {r['company']}  ·  **{score}/18** ({band})"]
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


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    args = ap.parse_args()

    cfg = load_config()
    conn = get_db(cfg)

    if args.command == "run":
        fetch_new_jobs(cfg, conn)
        apply_salary_filter(cfg, conn)
        evaluate_new_jobs(cfg, conn)
        generate_report(cfg, conn, args.date)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)


if __name__ == "__main__":
    main()
