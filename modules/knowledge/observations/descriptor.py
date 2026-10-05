"""Declare observation persistence, public reads and evidence dependencies."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ObservationDescriptor:
    """Expose typed derived observations produced only from normalized ingestion evidence."""
    id: str = "knowledge.observations"
    name: str = "Observations"
    version: str = "1.0.0"
    description: str = "Store provider-declared structured measurements with immutable evidence links."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "ingestion", "knowledge.documents")
    provides: tuple[str, ...] = ("world_observations",)
    requires: tuple[str, ...] = ("sources", "ingestion_receipts", "document_versions")
    routes: tuple[str, ...] = ("/api/v1/observations",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = ObservationDescriptor()
