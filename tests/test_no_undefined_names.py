"""Static lint gate: no UNDEFINED NAMES (and no syntax errors) in any project module.

Why this exists: the module split (core / fetch / filters / evaluation / report) moves functions
between files. A moved function that references a name its new module forgot to import is a runtime
NameError — but only on the code path that hits it. The unit tests can't catch that on the paths
they don't exercise (fetch's network calls, the eval providers' error/retry branches), which is
exactly how an `import sys` got dropped from evaluation.py and shipped. pyflakes flags that class
statically, across every module, in milliseconds.

We assert ONLY on real-defect message classes (undefined name, etc.) and syntax errors — NOT on
"imported but unused", which pipeline.py's re-export layer produces ON PURPOSE (it imports names
solely so `pipeline.X` keeps resolving for app.py / the tests / the validation scripts).
"""

import io
from pathlib import Path

import pytest
from pyflakes import api, messages
from pyflakes.reporter import Reporter

PROJECT = Path(__file__).resolve().parent.parent
MODULES = ["core.py", "chain.py", "fetch.py", "filters.py",
           "evaluation.py", "report.py", "pipeline.py", "app.py"]

# Message classes that mean a real defect (a crash waiting to happen), as opposed to style noise
# like UnusedImport — which the re-export layer legitimately and deliberately triggers.
FATAL = (
    messages.UndefinedName,
    messages.UndefinedLocal,
    messages.UndefinedExport,
    messages.ReturnOutsideFunction,
    messages.YieldOutsideFunction,
    messages.DuplicateArgument,
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


@pytest.mark.parametrize("module", MODULES)
def test_no_undefined_names(module):
    rep = _Collector()
    api.checkPath(str(PROJECT / module), rep)
    assert not rep.problems, "pyflakes found real defects:\n  " + "\n  ".join(rep.problems)
