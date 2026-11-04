"""Central module registration, capability dependency validation and descriptor-driven composition.

Descriptors are the single declaration point for routers, worker jobs and erasure packages;
``apps/api/main.py`` and ``core/auth/deletion_cleanup.py`` iterate the resolved lists below
instead of importing each module. Two sets exist on purpose:

* ``register_modules`` returns the lifecycle registry (the toggleable capability modules the
  settings UI, worker gates and tool registry reason about).
* ``composition_descriptors`` additionally includes platform descriptors (settings,
  translations, pick, model_gateway, mcp_widgets) that contribute routers or erasure
  packages but are not independently disable-able.
"""

from collections.abc import Iterable
from dataclasses import replace
from typing import TypeAlias

from core.module_descriptor import ModuleDescriptor, RouterSpec
from modules.agents.descriptor import descriptor as agents
from modules.automations.descriptor import descriptor as automations
from modules.chat.descriptor import descriptor as chat
from modules.connectors.descriptor import descriptor as connectors
from modules.dashboard.descriptor import descriptor as dashboard
from modules.export.descriptor import descriptor as export
from modules.goals.descriptor import descriptor as goals
from modules.ingestion.descriptor import descriptor as ingestion
from modules.knowledge.documents.descriptor import descriptor as documents
from modules.knowledge.entities.descriptor import descriptor as entities
from modules.knowledge.observations.descriptor import descriptor as observations
from modules.knowledge.relationships.descriptor import descriptor as relationships
from modules.knowledge.temporal.descriptor import descriptor as temporal
from modules.mcp_widgets.descriptor import descriptor as mcp_widgets
from modules.memory.descriptor import descriptor as memory
from modules.model_gateway.descriptor import descriptor as model_gateway
from modules.news.descriptor import descriptor as news
from modules.notifications.descriptor import descriptor as notifications
from modules.observability.descriptor import descriptor as observability
from modules.pick.descriptor import descriptor as pick
from modules.search.descriptor import descriptor as search
from modules.settings.descriptor import descriptor as settings
from modules.sources.descriptor import descriptor as sources
from modules.tasks.descriptor import descriptor as tasks
from modules.timeline.descriptor import descriptor as timeline
from modules.tools.descriptor import descriptor as tools
from modules.translations.descriptor import descriptor as translations

ModuleRegistry: TypeAlias = dict[str, ModuleDescriptor]  # noqa: UP040 - mypy-friendly alias

LIFECYCLE_DESCRIPTORS: tuple[ModuleDescriptor, ...] = (
    sources, ingestion, documents, observations, entities, relationships, timeline, search, temporal,
    dashboard, tasks, goals, news, notifications, chat, memory, tools, agents,
    automations, observability, connectors, export,
)
PLATFORM_DESCRIPTORS: tuple[ModuleDescriptor, ...] = (settings, translations, pick, model_gateway, mcp_widgets)

# Routers owned by core packages (not modules); positions share the global RouterSpec order space.
CORE_ROUTERS: tuple[RouterSpec, ...] = (
    RouterSpec("core.auth.routes", 10),
    RouterSpec("core.auth.deletion_routes", 20),
    RouterSpec("core.workspaces.routes", 30),
    RouterSpec("core.system.routes", 50),
    RouterSpec("core.realtime.routes", 320),
)


def register_modules(descriptors: Iterable[ModuleDescriptor] = LIFECYCLE_DESCRIPTORS) -> ModuleRegistry:
    """Build the descriptor registry and reject duplicate IDs or missing dependencies.

    Args:
        descriptors: Module descriptors; the default includes the native tools composition owner.
    Returns:
        A module-ID keyed map used for lifecycle and dependency checks.
    Raises:
        ValueError: If IDs repeat or a declared dependency has no registered descriptor.
    """
    registry: ModuleRegistry = {}
    for descriptor in descriptors:
        if descriptor.id in registry:
            raise ValueError(f"Duplicate module id: {descriptor.id}")
        registry[descriptor.id] = descriptor
    for descriptor in registry.values():
        missing = set(descriptor.dependencies) - registry.keys()
        if missing:
            raise ValueError(
                f"Module {descriptor.id} has missing dependencies: {', '.join(sorted(missing))}"
            )
    return registry


def effective_modules(disabled: Iterable[str], descriptors: ModuleRegistry | None = None) -> ModuleRegistry:
    """Return descriptor copies with explicit disables and unavailable dependencies applied transitively."""
    registry = descriptors or register_modules()
    unavailable = set(disabled)
    changed = True
    while changed:
        changed = False
        for module_id, descriptor in registry.items():
            if module_id not in unavailable and (
                not descriptor.enabled or any(dependency in unavailable for dependency in descriptor.dependencies)
            ):
                unavailable.add(module_id)
                changed = True
    return {module_id: replace(descriptor, enabled=module_id not in unavailable)
            for module_id, descriptor in registry.items()}


def scheduled_job_owners(descriptors: ModuleRegistry | None = None) -> dict[str, str]:
    """Map declared worker entry points to their owning module for persisted dispatch gates."""
    registry = descriptors or register_modules()
    return {job_name: module_id for module_id, descriptor in registry.items()
            for job_name in getattr(descriptor, "scheduled_jobs", ())}


def composition_descriptors() -> tuple[ModuleDescriptor, ...]:
    """Return lifecycle plus platform descriptors in their declaration order for composition."""
    return LIFECYCLE_DESCRIPTORS + PLATFORM_DESCRIPTORS


def ordered_router_specs(
    registry: ModuleRegistry | None = None,
) -> list[tuple[RouterSpec, ModuleDescriptor | None]]:
    """Return every core and module router spec sorted by its global mount ``order``.

    Each entry pairs the spec with its owning descriptor (``None`` for core routers) so callers
    can honour ``when_enabled``. Orders must be unique; a duplicate raises ``ValueError`` because
    tie-breaking would silently change first-match route resolution.

    Args:
        registry: Effective lifecycle registry used for ``when_enabled`` decisions; defaults to
            the static registry. Platform descriptors are always taken from the static set.
    """
    owners = {item.id: item for item in composition_descriptors()}
    if registry:
        owners.update(registry)
    entries: list[tuple[RouterSpec, ModuleDescriptor | None]] = [(spec, None) for spec in CORE_ROUTERS]
    entries.extend((spec, item) for item in owners.values() for spec in item.routers)
    entries.sort(key=lambda entry: entry[0].order)
    orders = [spec.order for spec, _ in entries]
    if len(orders) != len(set(orders)):
        raise ValueError("Router mount orders must be unique")
    return entries


def erasure_descriptors() -> tuple[ModuleDescriptor, ...]:
    """Return descriptors that declare an account-erasure package, in ascending ``erasure_order``.

    The order encodes foreign-key/cleanup dependencies for ordinary table erasure and must stay
    stable; duplicates raise ``ValueError`` rather than relying on sort stability.
    """
    items = sorted((item for item in composition_descriptors() if item.erasure_module), key=lambda i: i.erasure_order)
    if len({item.erasure_order for item in items}) != len(items):
        raise ValueError("Erasure orders must be unique")
    return tuple(items)
