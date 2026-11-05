"""Declare observation persistence, public reads and evidence dependencies."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ObservationDescriptor(ModuleDescriptor):
    """Expose typed derived observations produced only from normalized ingestion evidence."""
    id: str = "knowledge.observations"
    name: str = "Observations"
    version: str = "1.0.0"
    description: str = "Store provider-declared structured measurements with immutable evidence links."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "knowledge.documents")
    optional_dependencies: tuple[str, ...] = ("connectors",)
    provides: tuple[str, ...] = ("world_observations",)
    requires: tuple[str, ...] = ("sources", "ingestion_receipts", "document_versions")
    routes: tuple[str, ...] = ("/api/v1/observations",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.knowledge.observations.routes", 80),
    )
    erasure_module: str | None = "modules.knowledge.observations.erasure"
    erasure_order: int = 100


descriptor = ObservationDescriptor()
