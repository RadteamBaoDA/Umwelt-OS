"""Declare durable source ingestion routes and event-dispatch ownership."""

from dataclasses import dataclass

from core.module_descriptor import EventConsumerSpec, ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class IngestionDescriptor(ModuleDescriptor):
    """Describe ingestion contracts and the bounded worker entry points owned by this module."""

    id: str = "ingestion"
    name: str = "Ingestion"
    version: str = "1.0.0"
    description: str = "Receive, normalize, and process source-backed content."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "knowledge.documents")
    optional_dependencies: tuple[str, ...] = ("connectors", "knowledge.observations")
    scheduled_jobs: tuple[str, ...] = (
        "dispatch_pending_work",
        "process_ingestion_event",
        "process_normalize_event",
        "process_uploaded_file",
        "purge_collection_receipts",
    )
    provides: tuple[str, ...] = (
        "ingestion_runs",
        "ingestion_receipts",
        "source_observations",
        "normalized_evidence",
    )
    requires: tuple[str, ...] = ("sources", "document_versions")
    routes: tuple[str, ...] = ("/api/v1/ingestion", "/api/v1/documents/upload")
    emitted_events: tuple[str, ...] = (
        "document.file.uploaded",
        "ingestion.stage.requested",
        "ingestion.normalize.requested",
        "document.version.ready",
        "news.document.ready",
    )
    consumed_events: tuple[str, ...] = (
        "document.file.uploaded",
        "ingestion.stage.requested",
        "connector.crawl.requested",
        "ingestion.normalize.requested",
    )
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.ingestion.routes:documents_router", 130),
        RouterSpec("modules.ingestion.routes", 140),
    )
    erasure_module: str | None = "modules.ingestion.erasure"
    erasure_order: int = 70
    outbox_runtime: str = "modules.ingestion.public:OUTBOX_RUNTIME"
    event_consumers: tuple[EventConsumerSpec, ...] = (
        EventConsumerSpec(
            "document.file.uploaded",
            "process_uploaded_file",
            ("modules.ingestion",),
            "modules.ingestion.public:IngestionStageEventHandler",
        ),
        EventConsumerSpec(
            "ingestion.stage.requested",
            "process_ingestion_event",
            ("modules.ingestion",),
            "modules.ingestion.public:IngestionStageEventHandler",
        ),
        EventConsumerSpec(
            "connector.crawl.requested",
            "process_ingestion_event",
            ("modules.connectors",),
            "modules.ingestion.public:IngestionStageEventHandler",
        ),
        EventConsumerSpec(
            "ingestion.normalize.requested",
            "process_normalize_event",
            ("modules.ingestion",),
            "modules.ingestion.public:IngestionStageEventHandler",
        ),
    )


descriptor = IngestionDescriptor()
