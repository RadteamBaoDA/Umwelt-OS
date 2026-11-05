"""Declare news module descriptor for core registration and capability discovery."""

from dataclasses import dataclass

from core.module_descriptor import EventConsumerSpec, ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class NewsDescriptor(ModuleDescriptor):
    """Declare news intelligence module metadata, routes, events, and tools."""

    id: str = "news"
    name: str = "News"
    version: str = "1.0.0"
    description: str = "Owner-managed topic interests with story clustering and explainable relevance scoring."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = (
        "connectors",
        "goals",
        "ingestion",
        "knowledge.documents",
        "knowledge.entities",
        "knowledge.observations",
        "knowledge.timeline",
        "search",
        "sources",
    )
    scheduled_jobs: tuple[str, ...] = ("process_news_document_ready",)
    provides: tuple[str, ...] = ("news", "topics")
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = (
        "/api/v1/topics",
        "/api/v1/topics/{id}",
        "/api/v1/stories",
        "/api/v1/stories/{id}",
        "/api/v1/trends",
    )
    emitted_events: tuple[str, ...] = ("topic.created", "topic.updated", "topic.deleted")
    consumed_events: tuple[str, ...] = ("news.document.ready",)
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.news.topics", 350),
        RouterSpec("modules.news.routes", 360),
    )
    erasure_module: str | None = "modules.news.erasure"
    erasure_order: int = 140
    event_consumers: tuple[EventConsumerSpec, ...] = (
        EventConsumerSpec("news.document.ready", "process_news_document_ready",
                          ("modules.ingestion", "modules.knowledge.documents"), "modules.ingestion.public:DocumentReadyEventHandler"),
    )


descriptor = NewsDescriptor()
