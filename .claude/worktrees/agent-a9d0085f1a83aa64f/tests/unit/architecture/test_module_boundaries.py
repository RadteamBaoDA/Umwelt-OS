"""Module-boundary gates: import-linter contracts, the custom contract itself, and read-projection write guards."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRITE_CALLS = {"insert", "update", "delete", "add", "add_all", "merge", "refresh", "expunge"}


def test_import_linter_contracts_pass() -> None:
    """``lint-imports`` (see ``.importlinter``) keeps every contract: public-API-only, core-free-of-modules, layers."""
    pytest.importorskip("importlinter")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def _contract_check(
    imports: list[tuple[str, str]], modules: list[str], ignore: list[str] | None = None
) -> object:
    """Run ``ModulePublicApiContract`` over a synthetic import graph and return its ``ContractCheck``."""
    grimp = pytest.importorskip("grimp")
    sys.path.insert(0, str(ROOT))  # ``scripts`` is a plain directory, not an installed package
    from scripts.importlinter_contracts import ModulePublicApiContract

    graph = grimp.ImportGraph()
    for name in modules:
        graph.add_module(name)
    for importer, imported in imports:
        graph.add_import(importer=importer, imported=imported, line_number=7, line_contents="x")
    options: dict[str, object] = {
        "package": "modules",
        "allowed_submodules": ["public", "descriptor"],
        "extra_packages": ["modules.shared"],
    }
    if ignore:
        options["ignore_imports"] = ignore
    contract = ModulePublicApiContract("t", {"root_packages": ["modules"]}, options)
    return contract.check(graph, verbose=False)


MODULES = [
    "modules",
    "modules.a",
    "modules.a.descriptor",
    "modules.a.public",
    "modules.a.models",
    "modules.a.sub",
    "modules.a.sub.descriptor",
    "modules.a.sub.x",
    "modules.b",
    "modules.b.descriptor",
    "modules.b.public",
    "modules.b.service",
    "modules.shared",
    "modules.shared.public",
    "modules.shared.secret",
]


def test_contract_flags_private_cross_module_imports_only() -> None:
    """Private foreign submodules (also nested packages and extra packages) are violations; public/own are not."""
    check = _contract_check(
        [
            ("modules.a.public", "modules.b.public"),  # allowed: public contract
            ("modules.a.models", "modules.b.descriptor"),  # allowed: descriptor
            ("modules.b.public", "modules.b.service"),  # allowed: own internals
            (
                "modules.a.models",
                "modules.a.sub.x",
            ),  # nested package of another descriptor owner is foreign: flagged
            ("modules.b.service", "modules.a.models"),  # flagged: foreign models
            (
                "modules.b.service",
                "modules.shared.secret",
            ),  # flagged: extra package private submodule
            ("modules.b.service", "modules.shared.public"),  # allowed
        ],
        MODULES,
    )
    found = {(v["importer"], v["imported"]) for v in check.metadata["violations"]}  # type: ignore[attr-defined]
    assert found == {
        ("modules.a.models", "modules.a.sub.x"),
        ("modules.b.service", "modules.a.models"),
        ("modules.b.service", "modules.shared.secret"),
    }
    assert check.kept is False  # type: ignore[attr-defined]


def test_contract_honours_documented_exceptions() -> None:
    """An ignored import is skipped; with it removed the contract is kept."""
    check = _contract_check(
        [("modules.b.service", "modules.a.models")],
        MODULES,
        ignore=["modules.b.service -> modules.a.models"],
    )
    assert check.kept is True  # type: ignore[attr-defined]
    assert check.ignored_import_count == 1  # type: ignore[attr-defined]


def _projection_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``*ReadProjection`` classes imported from an owner's public module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(".public"):
            names |= {a.asname or a.name for a in node.names if a.name.endswith("ReadProjection")}
    return names


def _write_uses(tree: ast.AST, names: set[str]) -> list[int]:
    """Line numbers where one of ``names`` is a write target: instantiated, or passed to insert/update/delete/add."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if isinstance(func, ast.Name) and callee in names:
            lines.append(node.lineno)  # instantiation
        if callee in WRITE_CALLS and any(
            isinstance(a, ast.Name) and a.id in names for a in node.args
        ):
            lines.append(node.lineno)
    return lines


def test_read_projections_are_never_written_through() -> None:
    """Callers of an owner's ``*ReadProjection`` (approved read projection) only SELECT/JOIN; they never write."""
    offenders: list[str] = []
    checked = 0
    for path in (ROOT / "modules").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _projection_aliases(tree)
        if not aliases:
            continue
        checked += 1
        offenders += [f"{path.relative_to(ROOT)}:{n}" for n in _write_uses(tree, aliases)]
    assert checked >= 15, (
        "expected the cross-module readers of the sources/documents/observations projections"
    )
    assert not offenders, offenders


def test_write_guard_detects_a_write() -> None:
    """The guard fires on instantiation and on insert()/session.add() of a projection alias."""
    tree = ast.parse(
        "from modules.sources.public import SourceReadProjection as Source\n"
        "s = Source(id=1)\nsession.add(s)\nstmt = insert(Source)\nok = select(Source.id)\n",
    )
    assert _write_uses(tree, _projection_aliases(tree)) == [2, 4]
