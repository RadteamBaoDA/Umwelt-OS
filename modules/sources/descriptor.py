from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDescriptor:
    """Declare the source lifecycle module's public routes and capabilities."""

    id: str = "sources"
    name: str = "Sources"
    version: str = "1.0.0"
    description: str = "Manage collection identities and source lifecycle."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("sources",)
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/sources",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("sources.list_sources", "sources.get_source")
    navigation: tuple[dict[str, str], ...] = ({"label": "Sources", "href": "/sources"},)
    settings_schema: dict[str, object] | None = None


descriptor = SourceDescriptor()
