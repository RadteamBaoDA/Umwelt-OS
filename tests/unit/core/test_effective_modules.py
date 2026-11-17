"""``effective_modules`` over the real descriptor graph: defaults unchanged, hard deps cascade, optional deps do not."""

from __future__ import annotations

from dataclasses import replace

from core.modules import effective_modules, register_modules


def _closure(disabled: set[str]) -> set[str]:
    """Independent reference: every module reachable from ``disabled`` through reverse hard dependencies."""
    registry = register_modules()
    unavailable = set(disabled)
    grew = True
    while grew:
        grew = False
        for mid, item in registry.items():
            if mid not in unavailable and unavailable & set(item.dependencies):
                unavailable.add(mid)
                grew = True
    return unavailable


def test_default_configuration_keeps_every_module_enabled() -> None:
    """With nothing disabled the effective registry equals the static one (all descriptors enabled)."""
    registry = register_modules()
    effective = effective_modules(set(), registry)
    assert effective == registry
    assert all(item.enabled for item in effective.values())


def test_disabling_a_module_cascades_only_through_hard_dependencies() -> None:
    """Each module's disable set equals the reverse hard-dependency closure, for every single module."""
    registry = register_modules()
    for module_id in registry:
        effective = effective_modules({module_id}, registry)
        expected = _closure({module_id})
        assert {mid for mid, item in effective.items() if not item.enabled} == expected, module_id


def test_optional_dependencies_never_disable_a_module() -> None:
    """A module that only imports a peer (optional) stays enabled when that peer is disabled."""
    registry = register_modules()
    pairs = [(mid, peer) for mid, item in registry.items() for peer in item.optional_dependencies]
    assert pairs, "the real graph declares optional coupling"
    for mid, peer in pairs:
        if mid in _closure({peer}):
            continue  # reachable through some other hard path
        assert effective_modules({peer}, registry)[mid].enabled, (mid, peer)


def test_descriptor_enabled_flag_and_transitive_hard_dependency() -> None:
    """A descriptor that ships disabled is unavailable, and so are modules that hard-depend on it transitively."""
    registry = dict(register_modules())
    registry["sources"] = replace(registry["sources"], enabled=False)
    effective = effective_modules(set(), registry)
    assert not effective["sources"].enabled
    assert not effective["ingestion"].enabled  # hard: ingestion -> sources
    assert not effective["connectors"].enabled  # hard: connectors -> ingestion/sources
