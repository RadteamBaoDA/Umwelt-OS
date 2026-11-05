"""Descriptor ``dependencies`` must reflect real cross-module imports (ratcheting allow-list).

The AST scan maps every ``modules.<pkg>`` import to the lifecycle module that owns it, then
compares with each descriptor's declared ``dependencies``:

* ``undeclared``: imported but not declared (the lifecycle gate would not disable the module
  when that dependency is switched off).
* ``unused``: declared but never imported (metadata drift or runtime-only coupling).

Existing mismatches are allow-listed below with a count so the debt can only shrink. Fix a
mismatch by editing the descriptor (or removing the import), then delete its allow-list entry
and lower ``MAX_*``; adding a new mismatch fails the test.
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.modules import register_modules

ROOT = Path(__file__).resolve().parents[3]

# TODO(core-contracts ratchet): shrink these; they record mismatches present when the test was added.
UNDECLARED_ALLOWED: dict[str, set[str]] = {
    "agents": {"automations", "goals", "knowledge.documents", "memory", "sources", "tasks"},
    "automations": {"chat", "ingestion"},
    "chat": {
        "agents", "knowledge.observations", "knowledge.relationships", "knowledge.timeline", "memory", "search",
        "sources",
    },
    "connectors": {
        "knowledge.documents", "knowledge.entities", "knowledge.relationships", "knowledge.timeline", "tools",
    },
    "dashboard": {"connectors"},
    "export": {
        "chat", "dashboard", "goals", "knowledge.documents", "knowledge.entities", "knowledge.observations",
        "knowledge.relationships", "knowledge.timeline", "memory", "news", "sources", "tasks",
    },
    "goals": {"agents", "knowledge.entities"},
    "ingestion": {"connectors", "knowledge.observations"},
    "knowledge.documents": {
        "agents", "automations", "chat", "connectors", "dashboard", "goals", "ingestion", "knowledge.entities",
        "knowledge.observations", "knowledge.relationships", "knowledge.temporal", "knowledge.timeline", "memory",
        "news", "notifications", "tasks",
    },
    "knowledge.entities": {
        "connectors", "ingestion", "knowledge.relationships", "knowledge.temporal", "knowledge.timeline", "sources",
    },
    "knowledge.observations": {"connectors"},
    "knowledge.relationships": {"knowledge.temporal", "sources"},
    "knowledge.temporal": {"sources"},
    "knowledge.timeline": {"knowledge.temporal", "sources"},
    "memory": {"chat", "knowledge.documents", "sources"},
    "news": {
        "connectors", "goals", "ingestion", "knowledge.documents", "knowledge.entities", "knowledge.observations",
        "knowledge.timeline", "search", "sources",
    },
    "notifications": {"knowledge.documents", "sources"},
    "observability": {
        "agents", "automations", "chat", "connectors", "ingestion", "knowledge.documents", "knowledge.entities",
        "sources", "tools",
    },
    "search": {"goals", "sources", "tasks"},
    "sources": {"chat", "connectors", "dashboard", "ingestion", "knowledge.documents", "memory", "tools"},
    "tasks": {"agents", "goals", "knowledge.entities"},
    "tools": {"agents", "connectors", "knowledge.timeline"},
}
UNUSED_ALLOWED: dict[str, set[str]] = {
    "dashboard": {"knowledge.observations"},
    "knowledge.observations": {"ingestion"},
}
MAX_UNDECLARED = 104
MAX_UNUSED = 2


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
            for node in ast.walk(ast.parse(source.read_text())):
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
    undeclared = {mid: imports[mid] - set(item.dependencies) for mid, item in registry.items()}
    unused = {mid: set(item.dependencies) - imports[mid] for mid, item in registry.items()}
    undeclared = {mid: deps - UNDECLARED_ALLOWED.get(mid, set()) for mid, deps in undeclared.items()}
    unused = {mid: deps - UNUSED_ALLOWED.get(mid, set()) for mid, deps in unused.items()}
    assert not {mid: deps for mid, deps in undeclared.items() if deps}, "new undeclared module imports"
    assert not {mid: deps for mid, deps in unused.items() if deps}, "new unused declared dependencies"
    assert sum(len(v) for v in UNDECLARED_ALLOWED.values()) <= MAX_UNDECLARED
    assert sum(len(v) for v in UNUSED_ALLOWED.values()) <= MAX_UNUSED
    # Allow-list entries that no longer mismatch must be deleted so the ratchet tightens.
    for mid, allowed in UNDECLARED_ALLOWED.items():
        assert allowed <= imports[mid] - set(registry[mid].dependencies), f"stale allow-list entry for {mid}"
    for mid, allowed in UNUSED_ALLOWED.items():
        assert allowed <= set(registry[mid].dependencies) - imports[mid], f"stale allow-list entry for {mid}"
