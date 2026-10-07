"""Declare connector configuration and reconciliation ownership."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorsDescriptor:
    """Describe source connector routes and the bounded reconciliation worker."""

    id: str = "connectors"
    name: str = "Connectors"
    version: str = "1.0.0"
    description: str = "Manage configured connector credentials, collection, and provider synchronization."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "ingestion")
    scheduled_jobs: tuple[str, ...] = ("dispatch_due_collections", "process_collection_request")
    provides: tuple[str, ...] = ("source_connectors",)
    requires: tuple[str, ...] = ("sources", "ingestion_runs")
    routes: tuple[str, ...] = ("/api/v1/connectors", "/api/v1/connectors/sources")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = ConnectorsDescriptor()
