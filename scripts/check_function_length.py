"""Fail when a production Python function exceeds the maximum allowed length.

Usage: ``python scripts/check_function_length.py [--max-lines 100] [paths...]``.

The check walks ``core/``, ``modules/`` and ``apps/`` by default (excluding Alembic migrations and
tests), measures every ``def``/``async def`` from its first decorator-free line to its last line,
and prints each violation as ``path:line name lines``. Nested functions are measured on their own
and also count toward their parent's span, which matches how a reader experiences the code.
Exit status is 1 when any violation exists, 0 otherwise; it never modifies files.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEFAULT_ROOTS = ("core", "modules", "apps")
EXCLUDED_PARTS = frozenset({"migrations", "tests", "__pycache__", "node_modules", ".venv"})


def iter_python_files(roots: list[Path]) -> list[Path]:
    """Return production ``.py`` files under ``roots`` in a stable order.

    Files inside migration, test, cache or virtualenv directories are skipped so the gate only
    covers code the owner maintains by hand.
    """
    files: list[Path] = []
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
        files.extend(path for path in candidates if not EXCLUDED_PARTS.intersection(path.parts))
    return files


def long_functions(path: Path, max_lines: int) -> list[tuple[int, str, int]]:
    """Return ``(line, name, length)`` for every function in ``path`` longer than ``max_lines``.

    Raises ``SyntaxError`` for unparsable files so a broken file never passes the gate silently.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > max_lines:
                found.append((node.lineno, node.name, length))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Run the gate and return the process exit code (1 when violations exist)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_ROOTS))
    parser.add_argument("--max-lines", type=int, default=100)
    options = parser.parse_args(argv)
    violations = 0
    for path in iter_python_files([Path(item) for item in options.paths]):
        for line, name, length in long_functions(path, options.max_lines):
            print(f"{path.as_posix()}:{line} {name} {length} lines (max {options.max_lines})")
            violations += 1
    print(f"{violations} function(s) longer than {options.max_lines} lines", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
