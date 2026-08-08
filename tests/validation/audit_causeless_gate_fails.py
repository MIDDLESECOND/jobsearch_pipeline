#!/usr/bin/env python3
"""Triage GATE_FAIL rows that named no gate, and separate the real misroutes.

Why this exists: `evaluation.normalize_result`'s contract diagnostics compare the model's
own `gate_results` table against its verdict, but that field only exists from 2026-08-07.
Rows evaluated before it look unknowable — yet most are not, because the evaluation guide
says fit is scored ONLY when every gate passes ("if ANY gate fails, stop — do not score
fit"). So a rejection that still carries a complete `score_breakdown` contradicts itself
just as loudly as a gate table reading six PASSes, and that signature is testable on the
whole history.

The script classifies, it does not decide: nothing is rewritten, no verdict changes, and
the output is a list for a human to read. Read-only; writes one markdown file to
tests/validation/results/.
"""
import json
import sqlite3
import sys
from collections import Counter

from _common import DB_PATH, RESULTS_DIR

# The model's own words for "I could not evaluate this", as opposed to "this fails a gate".
_UNEVALUABLE = ("truncat", "incomplete", "insufficient", "cannot evaluate",
                "unable to evaluate", "cannot assess", "no details", "lacks specific")
# Channel language: the verdict the guide asks for when gates pass but the seat cannot be
# won by a cold application -- i.e. RECRUITER_ONLY, not a gate failure.
_CHANNEL = ("recruiter", "referral", "cold appl", "cold-appl", "not cold", "unwinnable")


def _classify(ev):
    text = " ".join(str(ev.get(k) or "") for k in ("one_line", "gate_notes")).lower()
    bd = ev.get("score_breakdown")
    scored = isinstance(bd, dict) and len([v for v in bd.values()
                                           if isinstance(v, (int, float))]) >= 6
    if scored:
        # Scored at all => the model believed every gate passed. The verdict disagrees.
        return "scored-yet-rejected"
    if any(w in text for w in _UNEVALUABLE):
        # Not a judgment call: the posting text never arrived. These belong in the
        # needs_manual bucket, not in the rejected pile.
        return "unevaluable-input"
    return "unlabelled-rejection"


def main():
    if not DB_PATH.exists():
        sys.exit(f"no database at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT job_url,company,title,source,first_seen,eval_json FROM jobs "
        "WHERE status='evaluated' AND verdict='GATE_FAIL' "
        "AND (failed_gate IS NULL OR failed_gate='') ORDER BY first_seen"
    ).fetchall()

    buckets, kinds = {}, Counter()
    for r in rows:
        try:
            ev = json.loads(r["eval_json"] or "{}")
        except ValueError:
            continue
        gr = ev.get("gate_results")
        # Rows carrying a gate table are already covered by the live contract check.
        pre_field = not (isinstance(gr, dict) and gr)
        kind = _classify(ev)
        kinds[(kind, pre_field)] += 1
        buckets.setdefault(kind, []).append((r, ev))

    out = RESULTS_DIR / "causeless_gate_fails.md"
    with out.open("w", encoding="utf-8") as fh:
        fh.write("# GATE_FAIL rows that named no gate\n\n")
        fh.write(f"{len(rows)} rows. Classified by the model's own stored output; "
                 "no verdict was changed.\n\n")
        for kind, label in (
            ("scored-yet-rejected",
             "Scored yet rejected — the model scored fit, which the guide permits only "
             "when every gate passes, then returned GATE_FAIL. Read these: the reasoning "
             "is usually a RECRUITER_ONLY channel call."),
            ("unevaluable-input",
             "Unevaluable input — the posting text was truncated or missing, so no gate "
             "was ever tested. A data problem, not a judgment; these are silently in the "
             "rejected pile rather than needs_manual."),
            ("unlabelled-rejection",
             "Unlabelled rejection — a real reason is stated but no gate was named."),
        ):
            items = buckets.get(kind, [])
            fh.write(f"\n## {label}\n\n**{len(items)} rows**\n\n")
            for r, ev in items:
                bd = ev.get("score_breakdown") or {}
                depth = bd.get("ai_artifact_depth")
                total = sum(v for v in bd.values() if isinstance(v, (int, float))) or ""
                fh.write(f"- **{r['first_seen'][:10]}** · {r['company']} — {r['title']}\n")
                if depth is not None:
                    fh.write(f"  - depth `{depth}` · breakdown sums to `{total}`"
                             f"{' — the guide caps depth 0 to RECRUITER_ONLY' if depth == 0 else ''}\n")
                fh.write(f"  - one_line: {ev.get('one_line') or '(none)'}\n")
                fh.write(f"  - gate_notes: {ev.get('gate_notes') or '(none)'}\n")
                fh.write(f"  - {r['job_url']}\n")

    print(f"{len(rows)} GATE_FAIL rows named no gate")
    for kind in ("scored-yet-rejected", "unevaluable-input", "unlabelled-rejection"):
        pre = kinds[(kind, True)]
        live = kinds[(kind, False)]
        print(f"  {kind:22} {pre + live:4}  ({pre} predate gate_results, {live} carry it)")
    scored = buckets.get("scored-yet-rejected", [])
    depth0 = sum(1 for _, ev in scored
                 if (ev.get("score_breakdown") or {}).get("ai_artifact_depth") == 0)
    print(f"  of the scored-yet-rejected rows, {depth0} scored ai_artifact_depth 0 "
          "(the guide's hard cap to RECRUITER_ONLY)")
    by_day = Counter(r["first_seen"][:10] for r, _ in scored)
    print("  their dates:", dict(sorted(by_day.items())))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
