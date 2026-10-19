"""Validate docs/GENUI_PARITY.md: complete rows, allowed statuses, real evidence paths/lines/symbols, no gaps.

Usage: python scripts/check_genui_parity.py [--doc PATH] [--root DIR] [--allow-not-ported]
Exit 0 when the matrix is valid, 1 with one readable line per problem otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STATUSES = {"ported", "equivalent", "excluded-by-owner", "not-ported"}
_ROW_ID = re.compile(r"^[A-Z]\d+$")
_TICKS = re.compile(r"`([^`]+)`")
_PATHLIKE = re.compile(r"^[\w.\-/]+(?::(\d+)(?:#([^\s`|]+))?)?$")
_SYMBOL_WINDOW = 6  # a `#Symbol` must occur within this many lines of the cited line
# Column layouts by cell count: the main table has an upstream-path column, the Extras table does not.
_LAYOUTS = {
    6: ("id", "feature", "upstream", "status", "evidence", "proof"),
    5: ("id", "feature", "status", "evidence", "proof"),
}


def _cells(line: str) -> list[str]:
    """Split a markdown table row into stripped cells (outer pipes removed)."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _path_problems(text: str, root: Path) -> list[str]:
    """Return problems for backticked `path`, `path:line` or `path:line#Symbol` tokens in `text`.

    A path must exist, a line must not exceed the file length, and a `#Symbol` must occur within
    +-_SYMBOL_WINDOW lines of the cited line, so drifted line numbers fail instead of passing silently.
    """
    problems = []
    for token in _TICKS.findall(text):
        match = _PATHLIKE.match(token)
        if not match or not ("/" in token or "." in token or match.group(1)):
            continue  # identifiers and commands, not paths
        rel = token.split(":")[0]
        target = root / rel
        if not target.is_file():
            problems.append(f"path not found: {rel}")
        elif match.group(1):
            lines = target.read_text("utf-8", "replace").splitlines()
            line, symbol = int(match.group(1)), match.group(2)
            if not 1 <= line <= len(lines):
                problems.append(f"line out of range: {token} (file has {len(lines)} lines)")
            elif symbol and not any(symbol in ln for ln in lines[max(0, line - 1 - _SYMBOL_WINDOW):line + _SYMBOL_WINDOW]):
                problems.append(f"symbol not near line: {token}")
    return problems


def check(doc: Path, root: Path, allow_not_ported: bool) -> tuple[list[str], dict[str, int]]:
    """Check every table row of `doc`; return (problems, status counts) with paths resolved under `root`."""
    problems: list[str] = []
    counts: dict[str, int] = {}
    for line in doc.read_text("utf-8").splitlines():
        cells = _cells(line) if line.startswith("|") else []
        if not cells or not _ROW_ID.match(cells[0]):
            continue
        layout = _LAYOUTS.get(len(cells))
        if layout is None:
            problems.append(f"{cells[0]}: expected 5 or 6 columns, found {len(cells)}")
            continue
        row = dict(zip(layout, cells))
        rid, status = row["id"], row["status"]
        for col in ("feature", "status", "evidence", "proof"):
            if not row[col]:
                problems.append(f"{rid}: empty {col}")
        if status not in STATUSES:
            problems.append(f"{rid}: invalid status '{status}' (allowed: {', '.join(sorted(STATUSES))})")
            continue
        counts[status] = counts.get(status, 0) + 1
        if status == "not-ported" and not allow_not_ported:
            problems.append(f"{rid}: not-ported ({row['feature']})")
        for col in ("evidence", "proof"):
            problems += [f"{rid}: {p}" for p in _path_problems(row[col], root)]
    return problems, counts


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", type=Path, default=root / "docs" / "GENUI_PARITY.md")
    ap.add_argument("--root", type=Path, default=root)
    ap.add_argument("--allow-not-ported", action="store_true")
    args = ap.parse_args(argv)
    problems, counts = check(args.doc, args.root, args.allow_not_ported)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    if problems:
        print("GenUI parity check FAILED:\n  " + "\n  ".join(problems) + f"\n({summary})")
        return 1
    print(f"GenUI parity check ok ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
