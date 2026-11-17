from dataclasses import dataclass

from core.module_descriptor import EventConsumerSpec, ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class SourceDescriptor(ModuleDescriptor):
    """Declare the source lifecycle module's public routes and capabilities."""

    id: str = "sources"
    name: str = "Sources"
    version: str = "1.0.0"
    description: str = "Manage collection identities and source lifecycle."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = (
        "chat",
        "connectors",
        "dashboard",
        "ingestion",
        "knowledge.documents",
        "memory",
        "tools",
    )
    scheduled_jobs: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("sources",)
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/sources",)
    emitted_events: tuple[str, ...] = ("source.purge.requested", "source.purge.coverage")
    consumed_events: tuple[str, ...] = (
        "source.purge.requested",
        "source.purge.progressed",
        "source.purge.coverage",
    )
    tools: tuple[str, ...] = ("sources.list_sources", "sources.get_source")
    navigation: tuple[dict[str, str], ...] = ({"label": "Sources", "href": "/sources"},)
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.sources.system_routes", 55),
        RouterSpec("modules.sources.routes", 60),
    )
    erasure_module: str | None = "modules.sources.erasure"
    erasure_order: int = 190
    event_consumers: tuple[EventConsumerSpec, ...] = (
        EventConsumerSpec(
            "source.purge.requested",
            "process_source_purge",
            ("modules.sources",),
            "modules.ingestion.public:SourcePurgeEventHandler",
        ),
        EventConsumerSpec(
            "source.purge.progressed",
            "process_source_purge",
            ("modules.knowledge.documents",),
            "modules.ingestion.public:SourcePurgeEventHandler",
        ),
        EventConsumerSpec(
            "source.purge.coverage",
            "process_source_memory_coverage",
            ("modules.sources",),
            "modules.ingestion.public:SourcePurgeEventHandler",
        ),
    )


descriptor = SourceDescriptor()
