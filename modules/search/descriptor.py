from dataclasses import dataclass


@dataclass(frozen=True)
class SearchDescriptor:
    """Declare search's public capabilities and its document-chunk dependency."""

    id: str = "search"
    name: str = "Search"
    version: str = "1.0.0"
    description: str = "Search current document chunks with lexical and permitted vector retrieval."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents",)
    scheduled_jobs: tuple[str, ...] = ("index_pending_chunks",)
    provides: tuple[str, ...] = ("search",)
    requires: tuple[str, ...] = ("document_chunks",)
    routes: tuple[str, ...] = ("/api/v1/search",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("search.query",)
    navigation: tuple[dict[str, str], ...] = ({"label": "Search", "href": "/search"},)
    settings_schema: dict[str, object] | None = None


descriptor = SearchDescriptor()
