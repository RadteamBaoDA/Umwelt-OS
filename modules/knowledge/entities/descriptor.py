from dataclasses import dataclass


@dataclass(frozen=True)
class EntityDescriptor:
    """Declare entity storage's document dependency and public capabilities."""

    id: str = "knowledge.entities"
    name: str = "Entities"
    version: str = "1.0.0"
    description: str = "Store owner-managed entities and exact evidence memberships."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents",)
    scheduled_jobs: tuple[str, ...] = ("process_entity_extraction_work",)
    provides: tuple[str, ...] = ("entities", "entity_evidence")
    requires: tuple[str, ...] = ("document_versions", "document_chunks")
    routes: tuple[str, ...] = ("/api/v1/entities",)
    emitted_events: tuple[str, ...] = ("knowledge.changed",)
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = EntityDescriptor()
