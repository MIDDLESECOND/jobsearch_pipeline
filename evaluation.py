#!/usr/bin/env python3
"""LLM gate-check evaluation: builds the system prompt from profile.md + evaluation_guide.md,
calls the configured provider (Anthropic or DeepSeek), and applies the guide's hard routing rules
deterministically in code (the 50/0 cap in normalize_result) so they can't depend on the model
complying. The 'brain' is the external markdown read at runtime — to change how postings are
judged, edit profile.md / evaluation_guide.md, not this file.

Imports only core (paths, constants, the API-key resolver).
"""

import json
import math
import re
import sys
import time

from core import PROFILE_PATH, GUIDE_PATH, GATE_NAMES, VERDICTS, _ensure_api_key


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
