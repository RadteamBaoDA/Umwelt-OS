"""Structural guards for two B1c fixes whose code paths need a full collector stack / graph driver to execute.

fetch_native_provider (GitHub path) and reconcile_canonical_nodes (replace_from_current_support) are
hundreds of lines deep. Their unit-testable invariant is where the None-narrowing assert sits: an assert
placed before the branch fires on the legitimate path where the other operand is the live one.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _asserts_under(path: str, func: str, narrowed: str, test_name: str) -> list[tuple[ast.Assert, bool]]:
    """Return each `assert <narrowed> is not None` in `func` and whether it sits in the else arm of
    `if <test_name> is not None`."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == func)
    found: list[tuple[ast.Assert, bool]] = []
    in_else: set[int] = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == test_name
                and isinstance(node.test.ops[0], ast.IsNot)):
            in_else.update(id(n) for stmt in node.orelse for n in ast.walk(stmt))
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == narrowed):
            found.append((node, id(node) in in_else))
    return found


def test_github_path_never_asserts_page_present() -> None:
    # GitHub collections have page=None; the pre-fix assert fired there (500 on every GitHub fetch).
    found = _asserts_under("modules/connectors/routes.py", "fetch_native_provider", "page",
                           "github_validated")
    assert found and all(in_else for _, in_else in found)


def test_replace_recovery_asserts_candidate_only_without_binding() -> None:
    # With a replacement binding and no candidate the pre-fix assert fired, failing a valid recovery.
    found = _asserts_under("modules/knowledge/temporal/adapter.py", "reconcile_canonical_nodes", "candidate",
                           "binding")
    assert found and all(in_else for _, in_else in found)
