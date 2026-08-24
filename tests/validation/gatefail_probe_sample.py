"""gatefail_probe_sample: draw the stratified GATE_FAIL sample that bounds the judge's
false-negative rate — the one recall layer with no usable instrument (2026-08-22).

What this answers, and only this: of the ~336 full-text rows/day the primary judge
GATE_FAILs, how many are (a) judge errors — the gate was misapplied on the full JD — and
(b) policy cost — the gate was applied correctly and the role is still one the user would
apply to. The noise probe of 2026-08-07 (35 GATE_FAIL rows x 3 draws, 0 flipped to a cold
apply under majority vote) can only bound (a) below ~8% — a rule-of-three ceiling that
would still allow ~200 misses/week, which is why this sample exists.

This script only DRAWS the sample (read-only DB, deterministic seed) and writes two files
under results/: the JSON the reader works from and a markdown worksheet with the rubric.
The reading itself is one attended deepdive session (~60 rows x 1.3 min), recorded in
reports/deepdive/ like any special batch; the sample rows are NOT zone rows and must not
enter `.deepdive_state.json`'s processed_urls.

Design:
- population: evaluated, unfiltered, undecided, verdict='GATE_FAIL', full text (not an
  Adzuna snippet), first_seen inside the window, title inside the user's target families
  (regex below — the point is the population the allocation cares about, not all of
  GATE_FAIL);
- strata by `failed_gate` in proportion to the 14-day mix, years_floor oversampled because
  it is 60% of the volume and the one whose "5+ years" wording is most often a judgment
  call; shortfalls in any stratum are refilled from years_floor;
- seed fixed by argument so the draw is reproducible and the worksheet can be re-derived.

Usage:
    python tests/validation/gatefail_probe_sample.py                 # 60 rows, 14 days
    python tests/validation/gatefail_probe_sample.py --n 80 --days 21 --seed 7
"""
import argparse
import datetime as dt
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = HERE / "results"
sys.path.insert(0, str(ROOT))
from core import ADZUNA_SNIPPET_MAX_CHARS  # noqa: E402

TITLE_FAMILY = re.compile(
    r"\b(ai|artificial intelligence|machine learning|ml|genai|llm|automation|copilot|"
    r"power platform|powerplatform|rpa|analyst|analytics|business intelligence|bi|"
    r"solution|solutions|consultant|implementation|innovation|transformation|"
    r"forward deployed|enablement|knowledge management|product manager)\b", re.I)

# Stratum shares of the sample. Measured 2026-08-22 over 14 days of full-text GATE_FAIL:
# years_floor 2979, domain 535, work_auth 452, tool 250, employment_type 232, other 148,
# role_substance 110. years_floor is oversampled relative to nothing — it IS the mix.
STRATA = {
    "years_floor": 0.40,
    "domain_requirement": 0.20,
    "work_auth": 0.10,
    "tool_requirement": 0.10,
    "employment_type": 0.10,
    "_rest": 0.10,          # other + role_substance + anything new
}
REASON_KEYS = ("gate_reason", "failed_gate_reason", "gate_notes", "gates", "reason", "notes")


def population(conn, days):
    floor = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT job_url, company, title, location, source, first_seen, eval_json,
                  length(COALESCE(description,'')) AS dlen
             FROM jobs
            WHERE status='evaluated' AND filter_source IS NULL AND app_status IS NULL
              AND verdict='GATE_FAIL'
              AND NOT (COALESCE(source,'')='adzuna'
                       AND length(COALESCE(description,'')) <= ?)
              AND first_seen >= ?""",
        (ADZUNA_SNIPPET_MAX_CHARS, floor)).fetchall()
    out = []
    for url, company, title, loc, src, fs, ej, dlen in rows:
        if not TITLE_FAMILY.search(title or ""):
            continue
        try:
            ev = json.loads(ej) if ej else {}
        except ValueError:
            ev = {}
        gate = ev.get("failed_gate") or "_unknown"
        reason = ""
        for k in REASON_KEYS:
            v = ev.get(k)
            if v:
                reason = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                break
        out.append({
            "job_url": url, "company": company, "title": title, "location": loc,
            "source": src, "first_seen": fs, "failed_gate": gate,
            "gate_reason": reason[:400], "description_chars": dlen,
        })
    return out


def draw(pop, n, seed):
    rng = random.Random(seed)
    by = {}
    for r in pop:
        key = r["failed_gate"] if r["failed_gate"] in STRATA else "_rest"
        by.setdefault(key, []).append(r)
    for v in by.values():
        rng.shuffle(v)
    want = {k: round(n * share) for k, share in STRATA.items()}
    picked, short = [], 0
    for k, cnt in want.items():
        got = by.get(k, [])[:cnt]
        short += cnt - len(got)
        picked.extend(got)
    # refill shortfalls from years_floor (the deep stratum), past what it already gave
    if short:
        used = {r["job_url"] for r in picked}
        extra = [r for r in by.get("years_floor", []) if r["job_url"] not in used][:short]
        picked.extend(extra)
    rng.shuffle(picked)
    return picked[:n]


def job_id(url):
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.split("?")[0][:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=20260822)
    a = ap.parse_args()

    conn = sqlite3.connect(f"file:{ROOT / 'jobs.db'}?mode=ro", uri=True)
    try:
        pop = population(conn, a.days)
    finally:
        conn.close()
    if not pop:
        sys.exit("empty population — no observation recorded")
    sample = draw(pop, a.n, a.seed)

    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    mix = {}
    for r in pop:
        mix[r["failed_gate"]] = mix.get(r["failed_gate"], 0) + 1
    smix = {}
    for r in sample:
        smix[r["failed_gate"]] = smix.get(r["failed_gate"], 0) + 1

    jpath = RESULTS / f"gatefail_probe_sample_{stamp}.json"
    jpath.write_text(json.dumps({
        "drawn_at": stamp, "days": a.days, "seed": a.seed, "n": len(sample),
        "population": len(pop), "population_by_gate": mix, "sample_by_gate": smix,
        "title_family": TITLE_FAMILY.pattern, "sample": sample,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [
        f"# GATE_FAIL probe sample — {stamp}",
        "",
        f"Population: {len(pop)} full-text GATE_FAIL rows in the target title family, last "
        f"{a.days} days (by gate: {json.dumps(mix)}). Sample: {len(sample)} rows, seed "
        f"{a.seed} (by gate: {json.dumps(smix)}). Rows are NOT zone rows — do not write them "
        f"to `.deepdive_state.json`.",
        "",
        "## Rubric — answer BOTH questions per row, from the FULL JD",
        "",
        "- **A. Gate correctly applied?** Read the posting's required column against the "
        "guide's gate rules (qualifications-column reading, years_floor, domain, work_auth, "
        "tool, employment-type/CTH). `yes` = the primary read the JD right; `no` = judge "
        "error (misread years, hallucinated requirement, preferred treated as required, "
        "missed a fully-remote contract exception, …). Quote the line that decides it.",
        "- **B. Would you apply under the standing allocation?** `yes` only if, gate aside, "
        "the role clears the cold-apply bar on the full read (fit ≥ 15 on the guide's scale, "
        "Bucket 3 or enablement-overlay, posted ≤ 14 days, variant can prove the required "
        "column). This is the user's call after the read; record the recommendation.",
        "",
        "Outputs, with Wilson intervals at n=" + str(len(sample)) + ":",
        "",
        "- **judge false-negative rate** = rows with A=no AND B=yes / n — the recall leak a "
        "better reader could recover;",
        "- **policy cost** = rows with A=yes AND B=yes / n — roles the guide's gate rules "
        "themselves turn away (a guide question, not a recall one);",
        "- A=no AND B=no is a harmless error; count it but it changes nothing.",
        "",
        "Stop rule: if the judge false-negative rate is < 1% (0 or 1 of 60), close the question "
        "for this judge build and record the date. If it is ≥ 3%, that is ≥ 10 rows/day and the "
        "fix is at the judge/guide, not at the batch.",
        "",
        "| # | gate | company | title | id | source | first_seen | JD chars | A | B | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(sample, 1):
        lines.append(
            f"| {i} | {r['failed_gate']} | {(r['company'] or '?')[:28]} | "
            f"{(r['title'] or '')[:48]} | `{job_id(r['job_url'])}` | {r['source'] or '?'} | "
            f"{(r['first_seen'] or '')[:10]} | {r['description_chars']} |  |  |  |")
    mpath = RESULTS / f"gatefail_probe_sample_{stamp}.md"
    mpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"population {len(pop)} -> sample {len(sample)}  (by gate: {smix})")
    print(f"wrote {jpath.name} and {mpath.name} under {RESULTS}")


if __name__ == "__main__":
    main()
