from collections.abc import Iterable
from typing import Any

from modules.knowledge.documents.descriptor import descriptor as documents
from modules.sources.descriptor import descriptor as sources
from modules.search.descriptor import descriptor as search
from modules.knowledge.entities.descriptor import descriptor as entities
from modules.knowledge.relationships.descriptor import descriptor as relationships
from modules.timeline.descriptor import descriptor as timeline
from modules.knowledge.temporal.descriptor import descriptor as temporal
from modules.chat.descriptor import descriptor as chat
from modules.memory.descriptor import descriptor as memory
from modules.tools.descriptor import descriptor as tools
from modules.agents.descriptor import descriptor as agents


def register_modules(descriptors: Iterable[Any] = (sources, documents, entities, relationships, timeline, search, temporal, chat, memory, tools, agents)) -> dict[str, Any]:
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
