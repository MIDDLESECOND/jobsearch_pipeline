#!/usr/bin/env python3
"""Head-to-head: run the SAME gate-eval prompt through Claude + DeepSeek V4 on a
sample of real postings, then diff the verdicts. Quality test, not production.

Reads DEEPSEEK_API_KEY + ANTHROPIC_API_KEY from env. Writes compare_results.json.
"""
import json
import os
import sys
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to gbk
except Exception:
    pass

import pipeline  # reuse build_system_prompt / parse_eval_json / _ensure_api_key

SAMPLE_N = 25
MODELS = [
    ("sonnet",   "anthropic", "claude-sonnet-4-6"),
    ("haiku",    "anthropic", "claude-haiku-4-5"),
    ("ds-flash", "deepseek",  "deepseek-v4-flash"),
    ("ds-pro",   "deepseek",  "deepseek-v4-pro"),
]
# $ per token (input, output). DeepSeek rates are placeholders — adjust to the
# current rate card; tokens are measured exactly so you can recompute.
PRICES = {
    "claude-sonnet-4-6": (3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5":  (1.0 / 1e6, 5.0 / 1e6),
    "deepseek-v4-flash": (0.10 / 1e6, 0.30 / 1e6),
    "deepseek-v4-pro":   (0.28 / 1e6, 1.10 / 1e6),
}

pipeline._ensure_api_key()
import anthropic
aclient = anthropic.Anthropic()
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
SYSTEM = pipeline.build_system_prompt()


def call_anthropic(model, user_msg):
    r = aclient.messages.create(
        model=model, max_tokens=1200, temperature=0,
        system=SYSTEM, messages=[{"role": "user", "content": user_msg}],
    )
    return r.content[0].text, r.usage.input_tokens, r.usage.output_tokens


def call_deepseek(model, user_msg):
    r = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DS_KEY}"},
        json={
            # V4 is a reasoning model — it spends 2-4k tokens thinking before the
            # JSON answer. 1200 truncates mid-reasoning -> empty answer. Give headroom.
            "model": model, "max_tokens": 8000, "temperature": 0,
            # Force valid JSON — DeepSeek (esp. the reasoning-style pro) otherwise
            # wraps the answer in prose. This is how you'd call it in production.
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def evaluate(provider, model, user_msg):
    t0 = time.monotonic()
    fn = call_anthropic if provider == "anthropic" else call_deepseek
    try:
        text, tin, tout = fn(model, user_msg)
        # normalize_result applies the same hard routing the pipeline enforces
        # (the depth-0 -> RECRUITER_ONLY cap), so verdicts compared here match prod.
        parsed = pipeline.normalize_result(pipeline.parse_eval_json(text))
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
    c = sqlite3.connect("jobs.db"); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM jobs WHERE length(trim(description))>0 "
        "ORDER BY search_name, job_url LIMIT ?", (SAMPLE_N,)
    ).fetchall()
    print(f"sampling {len(rows)} postings\n")

    results = []
    for i, r in enumerate(rows, 1):
        user_msg = (
            f"TITLE: {r['title']}\nCOMPANY: {r['company']}\nLOCATION: {r['location']}\n"
            f"SOURCE SEARCH: {r['search_name']} (tier: {r['tier']})\n"
            f"POSTED SALARY: {r['salary_min']}-{r['salary_max']}\n\n"
            f"JOB DESCRIPTION:\n{r['description']}"
        )
        rec = {"title": r["title"], "company": r["company"], "search": r["search_name"], "models": {}}
        line = f"[{i:>2}/{len(rows)}] {(r['title'] or '')[:38]:<38}"
        for label, provider, model in MODELS:
            res = evaluate(provider, model, user_msg)
            rec["models"][label] = res
            tag = res.get("verdict") if res["ok"] else "ERR"
            line += f" {label}={str(tag):<14}"
        results.append(rec)
        print(line, flush=True)

    with open("compare_results.json", "w", encoding="utf-8") as f:
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

    # Agreement vs sonnet (reference)
    ref = "sonnet"
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
        summarize(json.load(open("compare_results.json", encoding="utf-8")))
    else:
        main()
