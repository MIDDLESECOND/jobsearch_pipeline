#!/usr/bin/env python3
"""Alarm (never auto-fix) when the evaluation guide's sync surfaces drift apart.

The guide lives on three surfaces: the repo's `evaluation_guide.md` (the ONLY copy
the pipeline executes), a staging copy in Downloads that gets uploaded to the
claude.ai Project, and the Project's uploaded file itself (updatable only by a
manual re-upload). They have drifted repeatedly — including one BIDIRECTIONAL
episode where two 07-30 gate rules lived only on the Project side and were never
enforced by the pipeline, while the Project side lacked the 08-03 realignment
(CHANGELOG 2026-08-07 "Guide surfaces reconverged").

This script compares the two locally readable surfaces and, on mismatch, names the
sections that differ so the human can see WHICH rules are out of sync, not just
that bytes differ. It deliberately does not copy anything: the correct sync
direction depends on which side has the newer decision, and that is a judgment
call. (Detection heuristic when unsure: a rule with no CHANGELOG entry was decided
on the Project side and has never reached the pipeline.)

Exit codes: 0 = in sync, 1 = drift detected, 2 = a surface is missing.
Run:  python tests/validation/guide_sync_check.py [--mirror PATH]
      (mirror defaults to ~/Downloads/evaluation_guide.md)
"""
import argparse
import difflib
import hashlib
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core

SECTION_RE = re.compile(r"^## ", re.MULTILINE)


def sections(text):
    """{section_title: body} split on '## ' headings; the preamble keys as HEADER."""
    parts = SECTION_RE.split(text)
    out = {"HEADER": parts[0]}
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        out[title.strip()] = body
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", type=Path,
                    default=Path.home() / "Downloads" / "evaluation_guide.md")
    args = ap.parse_args()

    repo = core.BASE_DIR / "evaluation_guide.md"
    missing = [p for p in (repo, args.mirror) if not p.exists()]
    if missing:
        for p in missing:
            print(f"MISSING surface: {p}")
        sys.exit(2)

    a = repo.read_text(encoding="utf-8")
    b = args.mirror.read_text(encoding="utf-8")
    ha = hashlib.sha256(a.encode()).hexdigest()
    hb = hashlib.sha256(b.encode()).hexdigest()

    if ha == hb:
        print(f"in sync (sha256 {ha[:16]}…)")
        print("note: the claude.ai Project's UPLOADED file is a third surface this "
              "script cannot see — it matches only if it was re-uploaded after the "
              "mirror last changed.")
        sys.exit(0)

    print("DRIFT DETECTED between:")
    print(f"  repo   {repo}  ({len(a.splitlines())} lines, {ha[:16]}…)")
    print(f"  mirror {args.mirror}  ({len(b.splitlines())} lines, {hb[:16]}…)\n")

    sa, sb = sections(a), sections(b)
    only_a = [t for t in sa if t not in sb]
    only_b = [t for t in sb if t not in sa]
    for t in only_a:
        print(f"  section only in repo   : {t}")
    for t in only_b:
        print(f"  section only in mirror : {t}")
    for t in sa:
        if t in sb and sa[t] != sb[t]:
            delta = sum(1 for line in difflib.unified_diff(
                sa[t].splitlines(), sb[t].splitlines(), lineterm="", n=0)
                if line[:1] in "+-" and line[:3] not in ("+++", "---"))
            print(f"  section differs        : {t}  ({delta} changed lines)")

    print("\nNo file was modified. Reconverge by hand (rules from both sides may be "
        "newer — see the CHANGELOG heuristic in the docstring), then re-upload the "
        "merged file to the claude.ai Project.")
    sys.exit(1)


if __name__ == "__main__":
    main()
