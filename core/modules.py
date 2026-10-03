from collections.abc import Iterable
from typing import Any

from modules.knowledge.documents.descriptor import descriptor as documents
from modules.sources.descriptor import descriptor as sources
from modules.search.descriptor import descriptor as search
from modules.knowledge.entities.descriptor import descriptor as entities
from modules.knowledge.relationships.descriptor import descriptor as relationships
from modules.timeline.descriptor import descriptor as timeline
from modules.knowledge.temporal.descriptor import descriptor as temporal


def register_modules(descriptors: Iterable[Any] = (sources, documents, entities, relationships, timeline, search, temporal)) -> dict[str, Any]:
    """Build a module registry and reject duplicate IDs or dependencies that are not registered."""
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
