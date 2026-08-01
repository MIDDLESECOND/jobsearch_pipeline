#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false
"""One-off arbiter for the 2026-07-31 model comparison: re-run every round-2
disagreement posting through kimi-k3 (Moonshot's flagship) as an INDEPENDENT
evaluator — k3 sees only the standard system prompt + posting, never the other
models' verdicts — then line its verdict up against each contender's.

Reads results/compare_results.json (round 2, required) and
results/compare_results_luna_round1.json (round 1, optional — supplies the
pre-0731-drift ds-flash verdict for the flip cases). Writes
results/arbitration_k3.json.

Needs KIMI_API_JOBPIPELINE_KEY. k3 is priced $3/$15 per M — this touches only the
disagreement rows, not the full sample.

PROVENANCE: results files written before the 2026-08-01 prompt unification carry
verdicts from the old hyphen "POSTED SALARY: min-max" prompt; this script now sends
the production en-dash prompt (evaluation.build_user_msg). On the truncated-snippet
boundary a one-character delta can flip temp-0 verdicts, so before RE-running the
arbitration, regenerate compare_results.json with compare_models.py — otherwise a
prompt-byte flip gets attributed to model disagreement.
"""
import json
import sys
import time
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Lives in tests/validation/ but imports the pipeline modules at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import evaluation
from _common import RESULTS_DIR, DB_PATH  # noqa: E402

K3_MODEL = "kimi-k3"
MS_KEY = core._ensure_api_key("KIMI_API_JOBPIPELINE_KEY")
if not MS_KEY:
    sys.exit("KIMI_API_JOBPIPELINE_KEY not set")
SYSTEM = evaluation.build_system_prompt()


def call_k3(user_msg):
    # Same constraints as k2.6: thinking model, default temperature only,
    # max_tokens >= 16k so reasoning + answer never truncate. k3 thinks long —
    # the 180s timeout that killed half the k2.6 column is quadrupled here.
    r = httpx.post(
        "https://api.moonshot.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {MS_KEY}"},
        json={
            "model": K3_MODEL, "max_tokens": 16000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=720,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (d["choices"][0]["message"]["content"],
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def main():
    import sqlite3
    results = json.load(open(RESULTS_DIR / "compare_results.json", encoding="utf-8"))
    try:
        round1 = json.load(open(RESULTS_DIR / "compare_results_luna_round1.json",
                                encoding="utf-8"))
    except FileNotFoundError:
        round1 = None

    # Re-materialize the exact sample the comparison ran on: same query, same
    # ordering, so rows[i] is results[i]'s posting (titles are asserted below —
    # a drifted jobs.db would silently misalign the join otherwise).
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM jobs WHERE length(trim(description))>0 "
        "ORDER BY search_name, job_url LIMIT ?", (len(results),)
    ).fetchall()
    if len(rows) != len(results):
        sys.exit(f"sample size drifted: db gave {len(rows)}, results has {len(results)}")

    total_in = total_out = 0
    arb = []
    for i, (row, rec) in enumerate(zip(rows, results), 1):
        if row["title"] != rec["title"]:
            sys.exit(f"row {i} misaligned: db '{row['title']}' vs results '{rec['title']}'")
        verds = {lab: m.get("verdict") for lab, m in rec["models"].items() if m["ok"]}
        if len(set(verds.values())) <= 1:
            continue  # unanimous (among parsed) — nothing to arbitrate

        r1_flash = None
        if round1 and i <= len(round1) and round1[i - 1]["title"] == rec["title"]:
            m = round1[i - 1]["models"].get("ds-flash", {})
            r1_flash = m.get("verdict") if m.get("ok") else None

        user_msg = evaluation.build_user_msg(row)
        t0 = time.monotonic()
        try:
            text, tin, tout = call_k3(user_msg)
            parsed = evaluation.normalize_result(evaluation.parse_eval_json(text))
        except Exception as e:
            print(f"[{i:>2}] {row['title'][:44]:<44} k3=ERR {type(e).__name__}: {e}"[:150],
                  flush=True)
            arb.append({"title": row["title"], "company": row["company"],
                        "models": verds, "flash_round1": r1_flash,
                        "k3": {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}})
            continue
        total_in += tin
        total_out += tout
        k3v = parsed.get("verdict")
        sided = sorted(lab for lab, v in verds.items() if v == k3v)
        drift = f"  [flash r1={r1_flash} -> r2={verds.get('ds-flash')}]" \
            if r1_flash and r1_flash != verds.get("ds-flash") else ""
        print(f"[{i:>2}] {row['title'][:44]:<44} k3={str(k3v):<14} "
              f"sides with: {', '.join(sided) or 'NOBODY'}"
              f"{drift}  ({round(time.monotonic() - t0)}s)", flush=True)
        arb.append({
            "title": row["title"], "company": row["company"], "search": row["search_name"],
            "models": verds, "flash_round1": r1_flash,
            "k3": {"ok": True, "verdict": k3v, "failed_gate": parsed.get("failed_gate"),
                   "fit_score": parsed.get("fit_score"), "bucket": parsed.get("bucket"),
                   "gate_notes": (parsed.get("gate_notes") or "")[:200],
                   "one_line": (parsed.get("one_line") or "")[:200],
                   "in_tok": tin, "out_tok": tout},
            "k3_sides_with": sided,
        })

    with open(RESULTS_DIR / "arbitration_k3.json", "w", encoding="utf-8") as f:
        json.dump(arb, f, indent=2, ensure_ascii=False)

    done = [a for a in arb if a["k3"].get("ok")]
    cost = total_in * 3.0 / 1e6 + total_out * 15.0 / 1e6
    print(f"\narbitrated {len(done)}/{len(arb)} cases, "
          f"{total_in} in / {total_out} out tokens, ~${cost:.2f}")
    # Scoreboard: how often k3 sided with each model across the arbitrated cases.
    labs = sorted({lab for a in done for lab in a["models"]})
    for lab in labs:
        with_lab = sum(1 for a in done if lab in a["k3_sides_with"])
        of = sum(1 for a in done if lab in a["models"])
        print(f"  k3 agrees with {lab:<9} {with_lab}/{of}")


if __name__ == "__main__":
    main()
