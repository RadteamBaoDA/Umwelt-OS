from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class EntityDescriptor(ModuleDescriptor):
    """Declare entity storage's document dependency and public capabilities."""

    id: str = "knowledge.entities"
    name: str = "Entities"
    version: str = "1.0.0"
    description: str = "Store owner-managed entities and exact evidence memberships."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents",)
    optional_dependencies: tuple[str, ...] = (
        "connectors",
        "ingestion",
        "knowledge.relationships",
        "knowledge.temporal",
        "knowledge.timeline",
        "sources",
    )
    scheduled_jobs: tuple[str, ...] = ("process_entity_extraction_work",)
    provides: tuple[str, ...] = ("entities", "entity_evidence")
    requires: tuple[str, ...] = ("document_versions", "document_chunks")
    routes: tuple[str, ...] = ("/api/v1/entities",)
    emitted_events: tuple[str, ...] = ("knowledge.changed",)
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.knowledge.entities.routes", 90),
    )
    erasure_module: str | None = "modules.knowledge.entities.erasure"
    erasure_order: int = 90


descriptor = EntityDescriptor()
