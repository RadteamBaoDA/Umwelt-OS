"""Central module registration and capability dependency validation."""

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from modules.agents.descriptor import descriptor as agents
from modules.automations.descriptor import descriptor as automations
from modules.backup.descriptor import descriptor as backup
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
from modules.memory.descriptor import descriptor as memory
from modules.news.descriptor import descriptor as news
from modules.notifications.descriptor import descriptor as notifications
from modules.observability.descriptor import descriptor as observability
from modules.search.descriptor import descriptor as search
from modules.sources.descriptor import descriptor as sources
from modules.tasks.descriptor import descriptor as tasks
from modules.timeline.descriptor import descriptor as timeline
from modules.tools.descriptor import descriptor as tools


def register_modules(descriptors: Iterable[Any] = (
        sources, ingestion, documents, observations, entities, relationships, timeline, search, temporal,
        dashboard, tasks, goals, news, notifications, chat, memory, tools, agents,
        automations, observability, connectors, backup, export,
)) -> dict[str, Any]:
    """Build the descriptor registry and reject duplicate IDs or missing dependencies.

    Args:
        descriptors: Module descriptors; the default includes the native tools composition owner.
    Returns:
        A module-ID keyed map used for lifecycle and dependency checks.
    Raises:
        ValueError: If IDs repeat or a declared dependency has no registered descriptor.
    """
    registry: dict[str, Any] = {}
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


def effective_modules(disabled: Iterable[str], descriptors: dict[str, Any] | None = None) -> dict[str, Any]:
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


def scheduled_job_owners(descriptors: dict[str, Any] | None = None) -> dict[str, str]:
    """Map declared worker entry points to their owning module for persisted dispatch gates."""
    registry = descriptors or register_modules()
    return {job_name: module_id for module_id, descriptor in registry.items()
            for job_name in getattr(descriptor, "scheduled_jobs", ())}
