"""Declare connector configuration and reconciliation ownership."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ConnectorsDescriptor(ModuleDescriptor):
    """Describe source connector routes and the bounded reconciliation worker."""

    id: str = "connectors"
    name: str = "Connectors"
    version: str = "1.0.0"
    description: str = "Manage configured connector credentials, collection, and provider synchronization."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "ingestion")
    optional_dependencies: tuple[str, ...] = (
        "knowledge.documents",
        "knowledge.entities",
        "knowledge.relationships",
        "knowledge.timeline",
        "tools",
    )
    scheduled_jobs: tuple[str, ...] = ("dispatch_due_collections", "process_collection_request")
    provides: tuple[str, ...] = ("source_connectors",)
    requires: tuple[str, ...] = ("sources", "ingestion_runs")
    routes: tuple[str, ...] = ("/api/v1/connectors", "/api/v1/connectors/sources")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.connectors.routes", 150),
        RouterSpec("modules.connectors.routes:operator_router", 160),
        RouterSpec("modules.connectors.builtins", 180),
        RouterSpec("modules.connectors.provisioning_routes", 190),
        RouterSpec("modules.connectors.github.routes", 200),
        RouterSpec("modules.connectors.oauth_routes", 210),
        RouterSpec("modules.connectors.reddit_oauth_routes", 220),
    )
    erasure_module: str | None = "modules.connectors.erasure"
    erasure_order: int = 40


descriptor = ConnectorsDescriptor()
