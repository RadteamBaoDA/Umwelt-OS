from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class SearchDescriptor(ModuleDescriptor):
    """Declare search's public capabilities and its document-chunk dependency."""

    id: str = "search"
    name: str = "Search"
    version: str = "1.0.0"
    description: str = "Search current document chunks with lexical and permitted vector retrieval."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents",)
    optional_dependencies: tuple[str, ...] = ("goals", "sources", "tasks")
    scheduled_jobs: tuple[str, ...] = ("index_pending_chunks",)
    provides: tuple[str, ...] = ("search",)
    requires: tuple[str, ...] = ("document_chunks",)
    routes: tuple[str, ...] = ("/api/v1/search",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("search.query",)
    navigation: tuple[dict[str, str], ...] = ({"label": "Search", "href": "/search"},)
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.search.routes", 270),
    )
    erasure_module: str | None = "modules.search.erasure"
    erasure_order: int = 170


descriptor = SearchDescriptor()
