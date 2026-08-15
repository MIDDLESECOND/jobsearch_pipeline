#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""Head-to-head: run the SAME gate-eval prompt through Claude + DeepSeek V4 on a
sample of real postings, then diff the verdicts. Quality test, not production.

Reads DEEPSEEK_API_KEY + ANTHROPIC_API_KEY from env. Writes
tests/validation/results/compare_results.json.
"""
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk
except Exception:
    pass

# Lives in tests/validation/ but imports the pipeline modules at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import evaluation  # build_system_prompt / build_user_msg / parse_eval_json / normalize_result
from _common import RESULTS_DIR, DB_PATH  # noqa: E402

SAMPLE_N = 25
# (label, provider, model, extra request params). luna vs luna-high is a
# controlled pair: same model, only the reasoning budget differs. The
# openrouter columns auto-drop when OPENROUTER_API_KEY is absent (same
# pattern as openai/kimi). NOTE a bare full-roster run is no longer cheap
# (~$15 printed across all columns) — sweep scripts usually trim MODELS to
# the columns under study.
MODELS = [
    ("ds-flash",  "deepseek", "deepseek-v4-flash", {}),
    ("ds-pro",    "deepseek", "deepseek-v4-pro", {}),
    ("luna",      "openai",   "gpt-5.6-luna", {}),
    ("luna-high", "openai",   "gpt-5.6-luna", {"reasoning_effort": "high"}),
    ("kimi",      "kimi",     "kimi-k2.6", {}),
    ("opus",      "anthropic", "claude-opus-5", {}),
    ("fable",     "anthropic", "claude-fable-5", {}),
    ("sonnet",    "anthropic", "claude-sonnet-5", {}),
    ("haiku",     "anthropic", "claude-haiku-4-5", {}),
    ("grok",      "openrouter", "x-ai/grok-4.20", {}),
    ("gem-fl",    "openrouter", "google/gemini-3.5-flash", {}),
    ("gem-lite",  "openrouter", "google/gemini-3.5-flash-lite", {}),
    ("glm",       "openrouter", "z-ai/glm-5.2", {}),
]
REF = "ds-flash"  # agreement baseline: the incumbent (was "sonnet" pre-401)
# $ per token (input, output). Rates verified 2026-07-31 (Luna post the 07-30
# 80% cut); tokens are measured exactly so you can recompute if cards change.
# Anthropic/DeepSeek rates come from the one production table (evaluation.MODEL_PRICES —
# claude-sonnet-5 is steady-state list there; intro $2/$10 applies through 2026-08-31);
# only models the pipeline can't run through evaluation.py are priced here.
PRICES = {
    **evaluation.MODEL_PRICES,
    "gpt-5.6-luna":      (0.20 / 1e6, 1.20 / 1e6),
    "kimi-k2.6":         (0.95 / 1e6, 4.00 / 1e6),
    # openrouter columns: OpenRouter's live per-model rates, 2026-08-12
    "x-ai/grok-4.20":               (1.25 / 1e6, 2.50 / 1e6),
    "google/gemini-3.5-flash":      (1.50 / 1e6, 9.00 / 1e6),
    "google/gemini-3.5-flash-lite": (0.30 / 1e6, 2.50 / 1e6),
    "z-ai/glm-5.2":                 (0.50 / 1e6, 3.15 / 1e6),
}

core._ensure_api_key()
import anthropic
from anthropic.types import TextBlockParam
aclient = anthropic.Anthropic()
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OAI_KEY = core._ensure_api_key("OPENAI_API_KEY") or ""
MS_KEY = core._ensure_api_key("KIMI_API_JOBPIPELINE_KEY") or ""
OR_KEY = core._ensure_api_key("OPENROUTER_API_KEY") or ""
SYSTEM = evaluation.build_system_prompt()


def call_anthropic(model, user_msg, extra=None):
    # Claude 5 family: `temperature` is rejected there but restored for older
    # models (evaluation.anthropic_extras — pre-Claude-5 baselines were measured
    # at temperature 0 and must stay comparable), thinking is on by default and
    # counts against max_tokens (the old 1200 cap would truncate mid-JSON), and
    # the content list may lead with thinking blocks (evaluation.first_text). The
    # cache_control block mirrors production (_call_anthropic) so 25 sequential
    # calls don't each pay the full ~15k-token system prompt.
    # Annotated: anthropic_extras returns a per-generation kwarg bag, and without a
    # widened value type pyright resolves `**kwargs` against every keyword of create().
    kwargs: dict[str, Any] = dict(evaluation.anthropic_extras(model))
    kwargs.update(extra or {})
    system: list[TextBlockParam] = [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    r = aclient.messages.create(
        model=model, max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        **kwargs,
    )
    text = evaluation.first_text(r.content)
    if not text:
        raise RuntimeError(f"{model}: no text in response (stop_reason={r.stop_reason})")
    u = r.usage
    # Bill cache reads/writes at the full input price — the same deliberate
    # over-estimate convention as the deepseek arm.
    tin = (u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0)
           + (getattr(u, "cache_creation_input_tokens", 0) or 0))
    return text, tin, u.output_tokens


def call_deepseek(model, user_msg, extra=None):
    r = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DS_KEY}"},
        # Production's exact request shape by reference (max_tokens, temperature,
        # reasoning depth, JSON mode) — a comparison that benchmarks the incumbent at
        # settings it does not actually run answers the wrong question. `extra` from a
        # MODELS column overrides, which is what makes a controlled same-model pair
        # (e.g. flash low vs high) expressible; this arm used to drop `extra` silently
        # while call_openai splatted it.
        json=evaluation.deepseek_request_body(model, SYSTEM, user_msg, **(extra or {})),
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def call_openai(model, user_msg, extra=None):
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OAI_KEY}"},
        json={
            # GPT-5.x rejects max_tokens (wants max_completion_tokens) and any
            # temperature other than the default — omit temperature entirely.
            # The cap includes reasoning tokens, so give the high-effort column
            # the same 16k headroom as the other thinking models.
            "model": model, "max_completion_tokens": 16000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            **(extra or {}),
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def call_kimi(model, user_msg, extra=None):
    # Moonshot's international endpoint is OpenAI-compatible. K2.6 is a thinking
    # model: it only accepts temperature=1 (so we omit it), and the answer lands
    # in message.content with reasoning kept separate. Docs say max_tokens >=
    # 16000 so reasoning_content + content never truncate.
    r = httpx.post(
        "https://api.moonshot.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {MS_KEY}"},
        json={
            "model": model, "max_tokens": 16000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            **(extra or {}),
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def call_openrouter(model, user_msg, extra=None):
    # OpenRouter fronts many providers behind one OpenAI-compatible key; the
    # slug picks the provider (x-ai/..., google/..., z-ai/...). Same request
    # shape as the other thinking-model arms: 16k cap + JSON mode. If a route
    # rejects response_format the column just records ERR — drop the param
    # for that column via `extra` rather than loosening every arm.
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}"},
        json={
            "model": model, "max_tokens": 16000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            **(extra or {}),
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


CALLERS = {"anthropic": call_anthropic, "deepseek": call_deepseek,
           "openai": call_openai, "kimi": call_kimi,
           "openrouter": call_openrouter}


def evaluate(provider, model, user_msg, extra=None):
    t0 = time.monotonic()
    fn = CALLERS[provider]
    try:
        text, tin, tout = fn(model, user_msg, extra)
        # normalize_result applies the same hard routing the pipeline enforces
        # (the depth-0 -> RECRUITER_ONLY cap), so verdicts compared here match prod.
        parsed = evaluation.normalize_result(evaluation.parse_eval_json(text))
        return {
            "ok": True, "verdict": parsed.get("verdict"),
            "failed_gate": parsed.get("failed_gate"),
            "fit_score": parsed.get("fit_score"),
            "bucket": parsed.get("bucket"),
            "gate_notes": (parsed.get("gate_notes") or "")[:140],
            "in_tok": tin, "out_tok": tout, "latency": round(time.monotonic() - t0, 1),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160],
                "latency": round(time.monotonic() - t0, 1)}


def main():
    import sqlite3
    if not OAI_KEY:
        print("OPENAI_API_KEY not set — skipping the luna columns\n")
        MODELS[:] = [m for m in MODELS if m[1] != "openai"]
    if not MS_KEY:
        print("KIMI_API_JOBPIPELINE_KEY not set — skipping the kimi column\n")
        MODELS[:] = [m for m in MODELS if m[1] != "kimi"]
    if not OR_KEY:
        print("OPENROUTER_API_KEY not set — skipping the openrouter columns\n")
        MODELS[:] = [m for m in MODELS if m[1] != "openrouter"]
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM jobs WHERE length(trim(description))>0 "
        "ORDER BY search_name, job_url LIMIT ?", (SAMPLE_N,)
    ).fetchall()
    print(f"sampling {len(rows)} postings\n")

    results = []
    for i, r in enumerate(rows, 1):
        user_msg = evaluation.build_user_msg(r)
        rec = {"title": r["title"], "company": r["company"], "search": r["search_name"], "models": {}}
        line = f"[{i:>2}/{len(rows)}] {(r['title'] or '')[:38]:<38}"
        for label, provider, model, extra in MODELS:
            res = evaluate(provider, model, user_msg, extra)
            rec["models"][label] = res
            tag = res.get("verdict") if res["ok"] else "ERR"
            line += f" {label}={str(tag):<14}"
        results.append(rec)
        print(line, flush=True)

    with open(RESULTS_DIR / "compare_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    summarize(results)


def summarize(results):
    labels = [m[0] for m in MODELS]
    model_by_label = {m[0]: m[2] for m in MODELS}
    print("\n" + "=" * 64)
    print("PER-MODEL TOTALS")
    for lab in labels:
        recs = [r["models"][lab] for r in results]
        ok = [x for x in recs if x["ok"]]
        npass = sum(1 for x in ok if x["verdict"] == "PASS")
        nrec = sum(1 for x in ok if x["verdict"] == "RECRUITER_ONLY")
        nfail = sum(1 for x in ok if x["verdict"] == "GATE_FAIL")
        nerr = len(recs) - len(ok)
        pin, pout = PRICES[model_by_label[lab]]
        cost = sum(x.get("in_tok", 0) * pin + x.get("out_tok", 0) * pout for x in ok)
        avg_lat = sum(x["latency"] for x in recs) / len(recs)
        per1k = (cost / len(ok) * 1000) if ok else 0
        print(f"  {lab:<9} parsed {len(ok):>2}/{len(recs)}  "
              f"PASS {npass:>2}  RECRUITER {nrec:>2}  GATE_FAIL {nfail:>2}  ERR {nerr}  "
              f"| ${per1k:>6.2f}/1k jobs  {avg_lat:>4.1f}s avg")

    # Agreement vs the incumbent (reference)
    ref = REF if REF in labels else labels[0]
    print("\n" + "=" * 64)
    print(f"VERDICT AGREEMENT vs {ref} (only jobs both parsed)")
    for lab in labels:
        if lab == ref:
            continue
        both = [r for r in results
                if r["models"][ref]["ok"] and r["models"][lab]["ok"]]
        agree = sum(1 for r in both
                    if r["models"][ref]["verdict"] == r["models"][lab]["verdict"])
        gate_both = [r for r in both
                     if r["models"][ref]["verdict"] == "GATE_FAIL"
                     and r["models"][lab]["verdict"] == "GATE_FAIL"]
        gate_agree = sum(1 for r in gate_both
                         if r["models"][ref]["failed_gate"] == r["models"][lab]["failed_gate"])
        pct = 100 * agree / len(both) if both else 0
        gpct = 100 * gate_agree / len(gate_both) if gate_both else 0
        print(f"  {lab:<9} verdict {agree}/{len(both)} ({pct:>3.0f}%)  "
              f"| same failed_gate {gate_agree}/{len(gate_both)} ({gpct:>3.0f}%)")

    # Disagreements
    print("\n" + "=" * 64)
    print("VERDICT DISAGREEMENTS (where models split)")
    any_dis = False
    for r in results:
        verds = {lab: r["models"][lab].get("verdict") for lab in labels
                 if r["models"][lab]["ok"]}
        if len(set(verds.values())) > 1:
            any_dis = True
            print(f"\n  • {(r['title'] or '')[:50]} — {r['company']}  [{r['search']}]")
            for lab in labels:
                m = r["models"][lab]
                if not m["ok"]:
                    print(f"      {lab:<9} ERR: {m['error']}")
                else:
                    g = (f" gate={m['failed_gate']}" if m["verdict"] == "GATE_FAIL"
                         else f" score={m['fit_score']} bucket={m.get('bucket')}")
                    print(f"      {lab:<9} {str(m['verdict']):<14}{g}  — {m['gate_notes']}")
    if not any_dis:
        print("  none — all models agreed on every PASS/GATE_FAIL verdict")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "resummarize":
        summarize(json.load(open(RESULTS_DIR / "compare_results.json", encoding="utf-8")))
    else:
        main()
