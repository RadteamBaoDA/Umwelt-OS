"""Declare durable source ingestion routes and event-dispatch ownership."""

from dataclasses import dataclass


@dataclass(frozen=True)
class IngestionDescriptor:
    """Describe ingestion capabilities and the bounded worker entry points owned by this module."""

    id: str = "ingestion"
    name: str = "Ingestion"
    version: str = "1.0.0"
    description: str = "Receive, normalize, and process source-backed content."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "knowledge.documents")
    scheduled_jobs: tuple[str, ...] = (
        "dispatch_pending_work", "process_ingestion_event", "process_normalize_event", "process_uploaded_file",
    )
    provides: tuple[str, ...] = ("ingestion_runs", "source_observations")
    requires: tuple[str, ...] = ("sources", "documents")
    routes: tuple[str, ...] = ("/api/v1/ingestion", "/api/v1/documents/upload")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = IngestionDescriptor()
