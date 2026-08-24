"""Static type gate: pyright reports zero ERRORS across the project, from inside pytest.

Why this exists: six consecutive red CI runs (2026-08-06 .. 2026-08-24) were the typecheck
job and nothing else. The type gate lived only in CI, so its first run was always after the
push — every one of those reds was discovered by the next session, not the one that shipped
it. Same argument as test_no_undefined_names.py one file over, with a wider net: the 08-24
batch shipped a call missing a required argument in a validation script pytest never imports
(a TypeError on that script's first live run). pyflakes' message classes can't see arity,
Optional flow, or attribute existence; pyright can, and `python -m pytest` is the one gate
AGENTS.md documents and every session actually runs before committing.

The version is PINNED in requirements.txt: an unpinned checker means this gate (and CI)
can go red with zero code change when a new pyright release tightens a rule — the gate may
only move when the code moves.

Fail-loud rule: if pyright cannot run at all (no node, download blocked), this test FAILS
rather than skips. A gate that skips is a silent gate — the exact shape the pyflakes gate's
auto-discovery note warns about, and the reason the CI-only arrangement lasted six reds.

ERRORS only, on purpose: warnings include environment facts (msal has no stubs off Windows),
and gating on them would make the suite's verdict depend on which OS runs it.
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def test_pyright_reports_zero_errors():
    proc = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson",
         "--project", str(PROJECT),
         # Resolve imports against THIS interpreter's environment (the venv locally, the
         # job's python on CI) — without it pyright guesses, and every venv-only package
         # shows up as a missing import.
         "--pythonpath", sys.executable],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT, timeout=600,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"pyright produced no JSON (exit {proc.returncode}) — it did not run at all, "
            f"which FAILS rather than skips:\n{proc.stdout[:1500]}\n{proc.stderr[:1500]}")

    # Coverage sanity: pyrightconfig.json's own comment records that an explicit `exclude`
    # REPLACES pyright's defaults — a bad edit there silently shrinks this gate toward
    # zero files. 106 analyzed as of 2026-08-24.
    analyzed = out["summary"]["filesAnalyzed"]
    assert analyzed > 50, f"pyright analyzed only {analyzed} files — exclude-list rot?"

    errors = [d for d in out.get("generalDiagnostics", []) if d["severity"] == "error"]
    lines = [
        "%s:%d: %s" % (
            Path(d["file"]).name,
            d["range"]["start"]["line"] + 1,
            d["message"].splitlines()[0],
        )
        for d in errors
    ]
    assert not errors, "pyright errors (run `python -m pyright` for detail):\n" + "\n".join(lines)
