"""Ratchet on source file size: no new file above the limit and no baselined file may grow.

Usage: ``python scripts/check_file_size.py [--update]``.

Scans git-tracked ``*.py`` under ``core``, ``apps``, ``modules`` and ``scripts`` plus ``*.ts``/``*.tsx``
under ``apps/web/src`` (generated ``*.gen.ts``, migrations and tests excluded). A file above
``LIMIT`` lines must appear in ``scripts/file_size_baseline.json`` and may not exceed its recorded
count. Stale entries (file deleted or shrunk) are reported as a notice so the baseline is lowered.
``--update`` only lowers or removes baseline entries; raising one is a deliberate manual edit.
Exit status is 1 on any violation; the notice alone never fails the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

LIMIT = 1500
BASELINE = Path(__file__).with_name("file_size_baseline.json")
ROOTS = (
    ("core", ".py"),
    ("apps", ".py"),
    ("modules", ".py"),
    ("scripts", ".py"),
    ("apps/web/src", ".ts"),
    ("apps/web/src", ".tsx"),
)
EXCLUDED_PARTS = frozenset({"migrations", "tests", "__pycache__", "node_modules"})


def tracked_files() -> list[str]:
    """Return git-tracked, non-generated, non-test, non-migration files covered by the ratchet."""
    out = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True).stdout  # noqa: S607
    selected = []
    for name in out.splitlines():
        parts = name.split("/")
        if EXCLUDED_PARTS.intersection(parts) or name.endswith(".gen.ts"):
            continue
        if any(name.startswith(root + "/") and name.endswith(ext) for root, ext in ROOTS):
            selected.append(name)
    return selected


def count_lines(name: str) -> int | None:
    """Return the line count of ``name`` or ``None`` when the file no longer exists."""
    path = Path(name)
    return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else None


def evaluate(
    counts: dict[str, int], baseline: dict[str, int], limit: int = LIMIT
) -> tuple[list[str], list[str]]:
    """Compare current ``counts`` with ``baseline``; return ``(violations, stale_notices)``."""
    violations: list[str] = []
    for name, lines in sorted(counts.items()):
        allowed = baseline.get(name, limit)
        if lines > allowed:
            violations.append(
                f"{name}: {lines} lines exceeds {'baseline' if name in baseline else 'limit'} {allowed}"
            )
    stale = []
    for name, recorded in sorted(baseline.items()):
        lines = counts.get(name)
        if lines is None or lines <= limit:
            stale.append(f"{name}: no longer oversized or deleted; remove it from the baseline")
        elif lines < recorded:
            stale.append(
                f"{name}: shrank to {lines} (baseline {recorded}); lower it (run --update)"
            )
    return violations, stale


def main(argv: list[str] | None = None) -> int:
    """Run the ratchet (or rewrite the baseline with ``--update``) and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower or drop baseline entries")
    options = parser.parse_args(argv)
    has_baseline = BASELINE.exists()
    baseline: dict[str, int] = (
        json.loads(BASELINE.read_text(encoding="utf-8")) if has_baseline else {}
    )
    counts = {name: n for name in tracked_files() if (n := count_lines(name)) is not None}
    if options.update:
        # Only lower or drop: only a missing baseline *file* means "create it" from current oversize files;
        # an existing (even empty) baseline must never be re-grown from today's oversize files.
        base = baseline if has_baseline else {n: c for n, c in counts.items() if c > LIMIT}
        new = {n: min(c, base[n]) for n, c in counts.items() if n in base and c > LIMIT}
        BASELINE.write_text(
            json.dumps(dict(sorted(new.items())), indent=2) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {len(new)} entries")
        return 0
    violations, stale = evaluate(counts, baseline)
    for line in violations:
        print(line)
    for line in stale:
        print(f"notice: {line}", file=sys.stderr)
    print(f"{len(violations)} file(s) over the size ratchet (limit {LIMIT})", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
