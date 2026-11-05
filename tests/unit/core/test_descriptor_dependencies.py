"""Descriptor dependencies must reflect real cross-module imports (zero allow-list).

The AST scan maps every ``modules.<pkg>`` import to the lifecycle module that owns it, then
compares with each descriptor's declared ``dependencies`` plus ``optional_dependencies``:

* ``undeclared``: imported but not declared (the lifecycle gate would not disable the module
  when that dependency is switched off).
* ``unused``: declared but never imported (metadata drift or runtime-only coupling).

The allow-lists are empty and ``MAX_*`` is zero: fix a mismatch by editing the descriptor (or removing the import).
Only ``dependencies`` cascade in ``core.modules.effective_modules``; ``optional_dependencies`` are truthful
coupling metadata that never disable a module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.modules import register_modules

ROOT = Path(__file__).resolve().parents[3]

# The allow-lists are empty: every import coupling is declared (``dependencies`` gate the lifecycle,
# ``optional_dependencies`` record coupling that degrades instead of disabling) and nothing declared is unused.
UNDECLARED_ALLOWED: dict[str, set[str]] = {}
UNUSED_ALLOWED: dict[str, set[str]] = {}
MAX_UNDECLARED = 0
MAX_UNUSED = 0


def _package_dirs() -> dict[str, Path]:
    """Map each lifecycle module id to its package directory (``knowledge.timeline`` lives in ``modules/timeline``)."""
    mapping: dict[str, Path] = {}
    for module_id in register_modules():
        parts = module_id.split(".")
        directory = ROOT / "modules" / "/".join(parts)
        if module_id == "knowledge.timeline":
            directory = ROOT / "modules" / "timeline"
        mapping[module_id] = directory
    return mapping


def _owner_of(dotted: str, prefixes: dict[str, str]) -> str | None:
    """Return the module id owning ``dotted`` by longest package prefix, or ``None``."""
    best = None
    for prefix, module_id in prefixes.items():
        if (dotted == prefix or dotted.startswith(prefix + ".")) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, module_id)
    return best[1] if best else None


def scan_imports() -> dict[str, set[str]]:
    """Return, per module id, the other lifecycle modules its non-test sources import."""
    dirs = _package_dirs()
    prefixes = {"modules." + ".".join(path.relative_to(ROOT / "modules").parts): mid for mid, path in dirs.items()}
    found: dict[str, set[str]] = {mid: set() for mid in dirs}
    for module_id, directory in dirs.items():
        for source in directory.rglob("*.py"):
            if source.name == "descriptor.py":
                continue
            # Nested lifecycle packages (modules/knowledge/documents) belong to their own id.
            if _owner_of("modules." + ".".join(source.relative_to(ROOT / "modules").parts[:-1]), prefixes) != module_id:
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    owner = _owner_of(name, prefixes)
                    if owner is not None and owner != module_id:
                        found[module_id].add(owner)
    return found


def test_descriptor_dependencies_match_imports_with_ratchet() -> None:
    """Undeclared/unused dependency mismatches may not grow beyond the recorded allow-list."""
    registry = register_modules()
    imports = scan_imports()
    declared = {mid: set(item.dependencies) | set(item.optional_dependencies) for mid, item in registry.items()}
    undeclared = {mid: imports[mid] - declared[mid] for mid, item in registry.items()}
    unused = {mid: declared[mid] - imports[mid] for mid, item in registry.items()}
    undeclared = {mid: deps - UNDECLARED_ALLOWED.get(mid, set()) for mid, deps in undeclared.items()}
    unused = {mid: deps - UNUSED_ALLOWED.get(mid, set()) for mid, deps in unused.items()}
    assert not {mid: deps for mid, deps in undeclared.items() if deps}, "new undeclared module imports"
    assert not {mid: deps for mid, deps in unused.items() if deps}, "new unused declared dependencies"
    assert sum(len(v) for v in UNDECLARED_ALLOWED.values()) <= MAX_UNDECLARED
    assert sum(len(v) for v in UNUSED_ALLOWED.values()) <= MAX_UNUSED
    # Allow-list entries that no longer mismatch must be deleted so the ratchet tightens.
    for mid, allowed in UNDECLARED_ALLOWED.items():
        assert allowed <= imports[mid] - declared[mid], f"stale allow-list entry for {mid}"
    for mid, allowed in UNUSED_ALLOWED.items():
        assert allowed <= declared[mid] - imports[mid], f"stale allow-list entry for {mid}"


def test_hard_and_optional_dependencies_are_disjoint_and_exclude_self() -> None:
    """A peer is either a lifecycle gate or optional coupling, never both, and never the module itself."""
    for mid, item in register_modules().items():
        assert not set(item.dependencies) & set(item.optional_dependencies), mid
        assert mid not in item.dependencies and mid not in item.optional_dependencies, mid
