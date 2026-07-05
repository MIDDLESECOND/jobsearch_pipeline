"""Static lint gate: no UNDEFINED NAMES (and no syntax errors) in any project module.

Why this exists: the module split (core / fetch / filters / evaluation / report) moves functions
between files. A moved function that references a name its new module forgot to import is a runtime
NameError — but only on the code path that hits it. The unit tests can't catch that on the paths
they don't exercise (fetch's network calls, the eval providers' error/retry branches), which is
exactly how an `import sys` got dropped from evaluation.py and shipped. pyflakes flags that class
statically, across every module, in milliseconds.

We assert on real-defect message classes (undefined name, etc.), syntax errors, AND unused
imports: since the pipeline.py re-export layer was removed, every module imports only what it
uses, so an unused import is always dead code (usually a leftover from moving a function).

Residual limitation (inherent to pyflakes, not fixable by FATAL): an undefined name used inside a
`try` whose handler catches `NameError` (or a bare `except`) is suppressed and never reported. We
don't use that pattern; if you ever add an `except NameError`, this gate stops covering its body.
"""

import io
from pathlib import Path

import pytest
from pyflakes import api, messages
from pyflakes.reporter import Reporter

PROJECT = Path(__file__).resolve().parent.parent
# Auto-discover every .py we maintain (root app modules + the validation scripts + the tests
# themselves) instead of a hardcoded list — so a NEW module is covered automatically. The gate
# must not itself have the "forgot to add it to a list" footgun it exists to prevent.
MODULES = sorted(
    set(PROJECT.glob("*.py"))
    | set((PROJECT / "tests").glob("*.py"))
    | set((PROJECT / "tests" / "validation").glob("*.py"))
)

# Message classes that mean a real defect (a crash waiting to happen) or dead code.
FATAL = (
    messages.UnusedImport,
    messages.UndefinedName,
    messages.UndefinedLocal,
    messages.UndefinedExport,
    messages.ReturnOutsideFunction,
    messages.YieldOutsideFunction,
    messages.DuplicateArgument,
    # A star import blinds pyflakes' undefined-name detection for the whole module — an undefined
    # name becomes ImportStarUsage, not UndefinedName — so a future `from x import *` would
    # silently neuter this gate. Treat star imports as fatal (the project forbids them anyway), and
    # catch a loop var that shadows an import (rebinds it -> AttributeError on later use).
    messages.ImportStarUsed,
    messages.ImportStarUsage,
    messages.ImportStarNotPermitted,
    messages.ImportShadowedByLoopVar,
)


class _Collector(Reporter):
    """A pyflakes reporter that keeps only the fatal flakes and any syntax/parse error."""

    def __init__(self):
        super().__init__(io.StringIO(), io.StringIO())
        self.problems = []

    def flake(self, message):
        if isinstance(message, FATAL):
            self.problems.append(str(message))

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.problems.append(f"{filename}:{lineno}: syntax error: {msg}")

    def unexpectedError(self, filename, msg):
        self.problems.append(f"{filename}: {msg}")


@pytest.mark.parametrize("module", MODULES, ids=[p.name for p in MODULES])
def test_no_undefined_names(module):
    rep = _Collector()
    api.checkPath(str(module), rep)
    assert not rep.problems, "pyflakes found real defects:\n  " + "\n  ".join(rep.problems)
