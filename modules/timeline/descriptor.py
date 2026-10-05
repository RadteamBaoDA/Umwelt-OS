"""Declare timeline ownership, public APIs, dependencies, and durable event consumption."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TimelineDescriptor:
    """Declare canonical events and their exact document/entity dependencies."""
    id: str = "knowledge.timeline"
    name: str = "Timeline"
    version: str = "1.0.0"
    description: str = "Store owner-correctable events with explicit occurrence and evidence provenance."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents", "knowledge.entities")
    scheduled_jobs: tuple[str, ...] = ("process_timeline_extraction_work",)
    provides: tuple[str, ...] = ("events", "timeline")
    requires: tuple[str, ...] = ("document_versions", "document_chunks", "entities")
    routes: tuple[str, ...] = ("/api/v1/events", "/api/v1/timeline")
    emitted_events: tuple[str, ...] = ("knowledge.changed",)
    consumed_events: tuple[str, ...] = ("document.version.ready",)
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = TimelineDescriptor()
