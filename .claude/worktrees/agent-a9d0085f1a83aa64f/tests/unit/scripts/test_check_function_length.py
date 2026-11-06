"""Unit tests for the function-length ratchet's comparison and measuring logic."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_function_length", Path(__file__).parents[3] / "scripts" / "check_function_length.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
function_lengths = _mod.function_lengths


def test_new_long_function_fails() -> None:
    violations, _ = evaluate({"a.py::f": 101}, {}, limit=100)
    assert len(violations) == 1


def test_baselined_growth_fails_but_equal_passes() -> None:
    assert evaluate({"a.py::f": 150}, {"a.py::f": 150})[0] == []
    assert len(evaluate({"a.py::f": 151}, {"a.py::f": 150})[0]) == 1


def test_shrunk_or_deleted_baseline_entries_are_stale() -> None:
    violations, stale = evaluate(
        {"a.py::f": 120, "a.py::g": 10}, {"a.py::f": 150, "a.py::g": 200, "a.py::h": 130}
    )
    assert violations == [] and len(stale) == 3


def test_function_lengths_uses_qualnames(tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    source.write_text("class C:\n    def f(self):\n        def g():\n            pass\n")
    found = function_lengths(source)
    assert found["C.f"] == (2, 3) and found["C.f.g"] == (3, 2)
