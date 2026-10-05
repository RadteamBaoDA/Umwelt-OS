"""Declare news module descriptor for core registration and capability discovery."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsDescriptor:
    """Declare news intelligence module metadata, routes, events, and tools."""

    id: str = "news"
    name: str = "News"
    version: str = "1.0.0"
    description: str = "Owner-managed topic interests with story clustering and explainable relevance scoring."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
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


descriptor = NewsDescriptor()
