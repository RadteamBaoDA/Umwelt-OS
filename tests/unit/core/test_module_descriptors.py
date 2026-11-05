"""Descriptor-driven erasure ordering and registry typing."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.module_descriptor import ModuleDescriptor, RouterSpec
from core.modules import (
    LIFECYCLE_DESCRIPTORS,
    PLATFORM_DESCRIPTORS,
    composition_descriptors,
    erasure_descriptors,
    ordered_router_specs,
    register_modules,
)

ROOT = Path(__file__).resolve().parents[3]

# The hard-coded ``_OWNERS`` order of core/auth/deletion_cleanup.py before the registry existed.
LEGACY_ERASURE_ORDER = (
    "modules.agents.erasure", "modules.automations.erasure", "modules.chat.erasure",
    "modules.connectors.erasure", "modules.dashboard.erasure", "modules.goals.erasure",
    "modules.ingestion.erasure", "modules.knowledge.documents.erasure",
    "modules.knowledge.entities.erasure", "modules.knowledge.observations.erasure",
    "modules.knowledge.relationships.erasure", "modules.knowledge.temporal.erasure",
    "modules.memory.erasure", "modules.news.erasure", "modules.notifications.erasure",
    "modules.observability.erasure", "modules.search.erasure", "modules.settings.erasure",
    "modules.sources.erasure", "modules.tasks.erasure", "modules.timeline.erasure",
    "modules.tools.erasure", "modules.translations.erasure",
)


def test_erasure_registry_order_equals_legacy_hard_coded_order() -> None:
    """Resolved descriptor order, and the coordinator's owners, equal the old explicit tuple."""
    assert tuple(item.erasure_module for item in erasure_descriptors()) == LEGACY_ERASURE_ORDER
    from core.auth import deletion_cleanup

    assert tuple(module.__name__ for module in deletion_cleanup._OWNERS) == LEGACY_ERASURE_ORDER


def test_deletion_cleanup_no_longer_imports_modules() -> None:
    """core/auth/deletion_cleanup.py names no ``modules.*`` package statically."""
    tree = ast.parse((ROOT / "core" / "auth" / "deletion_cleanup.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("modules"), node.module
        elif isinstance(node, ast.Import):
            assert not any(alias.name.startswith("modules") for alias in node.names)


def test_registry_is_typed_and_platform_descriptors_stay_out_of_lifecycle() -> None:
    """Every descriptor derives from ModuleDescriptor; platform ones are composition-only."""
    assert all(isinstance(item, ModuleDescriptor) for item in composition_descriptors())
    lifecycle = register_modules()
    assert len(lifecycle) == len(LIFECYCLE_DESCRIPTORS) == 22
    assert {item.id for item in PLATFORM_DESCRIPTORS} == {
        "settings", "translations", "pick", "model_gateway", "mcp_widgets",
    }
    assert not {item.id for item in PLATFORM_DESCRIPTORS} & lifecycle.keys()


def test_router_orders_are_unique_and_specs_load() -> None:
    """Every spec resolves to a router object and mount orders are strictly ascending."""
    entries = ordered_router_specs(register_modules())
    orders = [spec.order for spec, _ in entries]
    assert orders == sorted(set(orders))
    for spec, _owner in entries:
        assert hasattr(spec.load(), "routes"), spec.target


def test_duplicate_router_order_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A colliding order is an error, never silently tie-broken."""
    clash = ModuleDescriptor(id="clash", routers=(RouterSpec("core.auth.routes", 10),))
    monkeypatch.setattr("core.modules.PLATFORM_DESCRIPTORS", (clash,))
    with pytest.raises(ValueError, match="unique"):
        ordered_router_specs()


def test_load_erasure_requires_declaration() -> None:
    """Descriptors without an erasure package refuse to load one."""
    with pytest.raises(LookupError):
        ModuleDescriptor(id="x").load_erasure()
