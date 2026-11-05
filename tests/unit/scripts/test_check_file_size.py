"""Unit tests for the file-size ratchet's pure comparison logic."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_file_size", Path(__file__).parents[3] / "scripts" / "check_file_size.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate


def test_update_does_not_regrow_an_empty_baseline_but_creates_a_missing_one(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``--update`` with ``{}`` stays empty; with no baseline file it is created from oversize files."""
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(_mod, "BASELINE", baseline)
    monkeypatch.setattr(_mod, "tracked_files", lambda: ["big.py"])
    monkeypatch.setattr(_mod, "count_lines", lambda _name: 1600)
    baseline.write_text("{}", encoding="utf-8")
    assert _mod.main(["--update"]) == 0
    assert baseline.read_text(encoding="utf-8").strip() == "{}"
    baseline.unlink()
    assert _mod.main(["--update"]) == 0
    assert '"big.py": 1600' in baseline.read_text(encoding="utf-8")


def test_new_file_over_limit_fails() -> None:
    violations, _ = evaluate({"a.py": 1501}, {}, limit=1500)
    assert len(violations) == 1


def test_baselined_file_growth_fails_but_equal_passes() -> None:
    assert evaluate({"a.py": 1700}, {"a.py": 1700})[0] == []
    assert len(evaluate({"a.py": 1701}, {"a.py": 1700})[0]) == 1


def test_shrunk_or_deleted_baseline_entries_are_stale() -> None:
    violations, stale = evaluate({"a.py": 1600, "b.py": 10}, {"a.py": 1700, "b.py": 1800, "c.py": 1600})
    assert violations == [] and len(stale) == 3
