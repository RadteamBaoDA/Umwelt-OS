"""Ratchet on production function length: no new long function and no baselined one may grow.

Usage: ``python scripts/check_function_length.py [--max-lines 100] [--update] [paths...]``.

The check walks ``core/``, ``modules/`` and ``apps/`` by default (excluding Alembic migrations and
tests) and measures every ``def``/``async def`` from its ``def`` line to its last line. Nested
functions are measured on their own and also count toward their parent's span, which matches how a
reader experiences the code. A function above ``--max-lines`` must appear in
``scripts/function_length_baseline.json`` (keyed ``path::qualname``) and may not exceed its recorded
length; same-named functions in one scope are tracked by their longest. Stale entries (function
deleted, shrunk or no longer over the limit) are printed as notices so the baseline is lowered.
``--update`` only lowers or removes baseline entries (a missing baseline is created from the current
offenders); raising one is a deliberate manual edit. Exit status is 1 on any violation; notices
alone never fail the run.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

DEFAULT_ROOTS = ("core", "modules", "apps")
BASELINE = Path(__file__).with_name("function_length_baseline.json")
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


def function_lengths(path: Path) -> dict[str, tuple[int, int]]:
    """Return ``{qualname: (first_line, length)}`` for every function in ``path``.

    Qualified names join enclosing classes and functions with dots. When one qualname repeats
    (overloads, conditional redefinitions) the longest definition is kept. Raises ``SyntaxError``
    for unparsable files so a broken file never passes the gate silently.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, tuple[int, int]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        """Record every function below ``node`` under its dotted ``prefix``."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                qualname = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    length = (child.end_lineno or child.lineno) - child.lineno + 1
                    if length > found.get(qualname, (0, 0))[1]:
                        found[qualname] = (child.lineno, length)
                visit(child, qualname + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def evaluate(
    counts: dict[str, int], baseline: dict[str, int], limit: int = 100
) -> tuple[list[str], list[str]]:
    """Compare current ``counts`` with ``baseline``; return ``(violations, stale_notices)``."""
    violations: list[str] = []
    for key, length in sorted(counts.items()):
        allowed = baseline.get(key, limit)
        if length > allowed:
            kind = "baseline" if key in baseline else "limit"
            violations.append(f"{key}: {length} lines exceeds {kind} {allowed}")
    stale = []
    for key, recorded in sorted(baseline.items()):
        length = counts.get(key)
        if length is None or length <= limit:
            stale.append(f"{key}: no longer oversized or deleted; remove it from the baseline")
        elif length < recorded:
            stale.append(
                f"{key}: shrank to {length} (baseline {recorded}); lower it (run --update)"
            )
    return violations, stale


def main(argv: list[str] | None = None) -> int:
    """Run the ratchet (or rewrite the baseline with ``--update``) and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_ROOTS))
    parser.add_argument("--max-lines", type=int, default=100)
    parser.add_argument("--update", action="store_true", help="lower or drop baseline entries")
    options = parser.parse_args(argv)
    baseline: dict[str, int] = (
        json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    )
    counts: dict[str, int] = {}
    for path in iter_python_files([Path(item) for item in options.paths]):
        for name, (_, length) in function_lengths(path).items():
            counts[f"{path.as_posix()}::{name}"] = length
    if options.update:
        # Only lower or drop: a missing baseline file means "create it" from current offenders.
        base = baseline or {k: v for k, v in counts.items() if v > options.max_lines}
        new = {k: min(v, base[k]) for k, v in counts.items() if k in base and v > options.max_lines}
        BASELINE.write_text(
            json.dumps(dict(sorted(new.items())), indent=2) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {len(new)} entries")
        return 0
    violations, stale = evaluate(counts, baseline, options.max_lines)
    for line in violations:
        print(line)
    for line in stale:
        print(f"notice: {line}", file=sys.stderr)
    print(
        f"{len(violations)} function(s) over the length ratchet (limit {options.max_lines})",
        file=sys.stderr,
    )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
