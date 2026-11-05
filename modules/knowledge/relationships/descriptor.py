from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class RelationshipDescriptor(ModuleDescriptor):
    """Declare relationship storage's dependencies, capabilities, and event."""

    id: str = "knowledge.relationships"
    name: str = "Relationships"
    version: str = "1.0.0"
    description: str = "Store owner-authored and evidence-backed relationships."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.entities", "knowledge.documents")
    provides: tuple[str, ...] = ("relationships", "relationship_evidence")
    requires: tuple[str, ...] = ("entities", "document_versions", "document_chunks")
    routes: tuple[str, ...] = ("/api/v1/relationships", "/api/v1/entities/{id}/neighbors")
    emitted_events: tuple[str, ...] = ("knowledge.changed",)
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.knowledge.relationships.routes", 100),
    )
    erasure_module: str | None = "modules.knowledge.relationships.erasure"
    erasure_order: int = 110


descriptor = RelationshipDescriptor()
