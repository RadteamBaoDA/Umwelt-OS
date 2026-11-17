"""Structural guards for two B1c fixes whose code paths need a full collector stack / graph driver to execute.

the native provider fetch dispatcher (GitHub path) and _build_replacement_node (reconcile_canonical_nodes replace_from_current_support) are
hundreds of lines deep. Their unit-testable invariant is where the None-narrowing assert sits: an assert
placed before the branch fires on the legitimate path where the other operand is the live one.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _asserts_under(
    path: str, func: str, narrowed: str, test_name: str
) -> list[tuple[ast.Assert | ast.If, bool]]:
    """Return each `assert <narrowed> is not None` in `func` and whether it sits in the else arm of
    `if <test_name> is not None`."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == func
    )
    found: list[tuple[ast.Assert | ast.If, bool]] = []
    in_else: set[int] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == test_name
            and isinstance(node.test.ops[0], ast.IsNot)
        ):
            in_else.update(id(n) for stmt in node.orelse for n in ast.walk(stmt))
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assert)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == narrowed
        ):
            found.append((node, id(node) in in_else))
        # Production code now narrows with ``if x is None: raise`` instead of ``assert`` (ruff S101).
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == narrowed
            and isinstance(node.test.ops[0], ast.Is)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Raise)
        ):
            found.append((node, id(node) in in_else))
    return found


def test_github_path_never_asserts_page_present() -> None:
    # GitHub collections carry no provider page (the proof is validated into a CollectedPage); the
    # pre-fix `assert page is not None` fired there (500 on every GitHub fetch). After the dispatcher
    # moved to native_fetch/native_collectors, no GitHub-path function may narrow a `page`/`validated`.
    # Every ``collect`` method in native_collectors (FeedCollector first, GithubCollector among them) is checked.
    tree_c = ast.parse(
        (ROOT / "modules/connectors/native_collectors.py").read_text(encoding="utf-8")
    )
    collects = [
        n for n in ast.walk(tree_c) if isinstance(n, ast.AsyncFunctionDef) and n.name == "collect"
    ]
    github = next(
        n for n in ast.walk(tree_c) if isinstance(n, ast.ClassDef) and n.name == "GithubCollector"
    )
    assert any(m in collects for m in github.body), (
        "GithubCollector.collect must be among the inspected methods"
    )
    for fn in collects:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assert)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id in {"page", "validated"}
            ):
                raise AssertionError(f"collect() at line {fn.lineno} narrows {node.test.left.id}")
    for path, func in (
        ("modules/connectors/native_fetch.py", "_accept_page"),
        ("modules/connectors/native_fetch.py", "run_provider_fetch"),
    ):
        assert not _asserts_under(path, func, "page", "github_validated"), (path, func)
        assert not _asserts_under(path, func, "validated", "github_validated"), (path, func)
    # The dispatcher must take a non-optional CollectedPage: that is what makes the assert unnecessary.
    tree = ast.parse((ROOT / "modules/connectors/native_fetch.py").read_text(encoding="utf-8"))
    accept = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_accept_page"
    )
    page_arg = next(a for a in accept.args.args if a.arg == "page")
    assert isinstance(page_arg.annotation, ast.Name) and page_arg.annotation.id == "CollectedPage"


def test_replace_recovery_asserts_candidate_only_without_binding() -> None:
    # With a replacement binding and no candidate the pre-fix assert fired, failing a valid recovery.
    found = _asserts_under(
        "modules/knowledge/temporal/_reconcile.py",
        "_build_replacement_node",
        "candidate",
        "binding",
    )
    assert found and all(in_else for _, in_else in found)
