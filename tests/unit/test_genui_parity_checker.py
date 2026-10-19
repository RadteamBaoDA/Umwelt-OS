"""Tests for scripts/check_genui_parity.py using tmp markdown fixtures."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_genui_parity", Path(__file__).resolve().parents[2] / "scripts" / "check_genui_parity.py"
)
checker = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(checker)  # type: ignore[union-attr]

HEAD = "| id | f | up | status | ev | proof |\n|---|---|---|---|---|---|\n"


def _run(tmp_path: Path, rows: str, *flags: str) -> int:
    """Write a fixture doc with one existing file `a.py` (2 lines) and run the checker on it."""
    (tmp_path / "a.py").write_text("x\ny\n")
    doc = tmp_path / "doc.md"
    doc.write_text(HEAD + rows)
    return checker.main(["--doc", str(doc), "--root", str(tmp_path), *flags])


def test_valid_matrix_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, "| T01 | feat | u | ported | `a.py:2` | pytest |\n") == 0


def test_missing_path_fails(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert _run(tmp_path, "| T01 | feat | u | ported | `nope/b.py:1` | pytest |\n") == 1
    assert "path not found: nope/b.py" in capsys.readouterr().out


def test_bad_status_and_empty_proof_fail(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert _run(tmp_path, "| T01 | feat | u | done | `a.py` |  |\n") == 1
    out = capsys.readouterr().out
    assert "invalid status 'done'" in out and "empty proof" in out


def test_not_ported_fails_unless_allowed(tmp_path: Path) -> None:
    row = "| T01 | feat | u | not-ported | none | none |\n"
    assert _run(tmp_path, row) == 1
    assert _run(tmp_path, row, "--allow-not-ported") == 0
