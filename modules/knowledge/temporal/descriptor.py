"""Declare derived temporal graph ownership and canonical dependency contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemporalDescriptor:
    """Expose derived graph status and scoped reconciliation without owning canonical records."""

    id: str = "knowledge.temporal"
    name: str = "Temporal Knowledge"
    version: str = "1.0.0"
    description: str = "Synchronize evidence-backed temporal graph projections with durable recovery."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents", "knowledge.entities", "knowledge.relationships", "knowledge.timeline")
    scheduled_jobs: tuple[str, ...] = ("process_graph_operation",)
    provides: tuple[str, ...] = ("graph_status", "graph_reconciliation", "knowledge_changes")
    requires: tuple[str, ...] = ("document_versions", "entities", "relationships", "events")
    routes: tuple[str, ...] = ("/api/v1/system/graph", "/api/v1/knowledge/changes")
    emitted_events: tuple[str, ...] = ("knowledge.changed",)
    consumed_events: tuple[str, ...] = ("document.version.ready",)
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = TemporalDescriptor()
