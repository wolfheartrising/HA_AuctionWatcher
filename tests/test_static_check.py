"""Catch undefined names (like a missing import) in the Home Assistant parts too.

Most of the integration can't be imported without Home Assistant, so the other
tests never run coordinator.py, config_flow.py etc. pyflakes reads them without
running them and fails on any name that's used but never defined or imported.
"""

from pathlib import Path

import pytest

pyflakes_api = pytest.importorskip("pyflakes.api")
pyflakes_reporter = pytest.importorskip("pyflakes.reporter")

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "auction_watcher"


class _Collect:
    def __init__(self):
        self.problems = []

    def flake(self, message):
        text = str(message)
        if "imported but unused" not in text and "redefinition of unused" not in text:
            self.problems.append(text)

    def unexpectedError(self, filename, msg):
        self.problems.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.problems.append(f"{filename}:{lineno}: {msg}")


@pytest.mark.parametrize("path", sorted(PKG.rglob("*.py")), ids=lambda p: p.name)
def test_no_undefined_names(path):
    collect = _Collect()
    reporter = pyflakes_reporter.Reporter(None, None)
    reporter.flake = collect.flake
    reporter.unexpectedError = collect.unexpectedError
    reporter.syntaxError = collect.syntaxError
    pyflakes_api.checkPath(str(path), reporter)
    assert collect.problems == []
