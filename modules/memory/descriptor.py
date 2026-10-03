"""Declare memory module ownership, public capabilities, dependencies, and routes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryDescriptor:
    """Declare selective memory lifecycle, candidates evaluation, and privacy management capabilities."""

    id: str = "memory"
    name: str = "Memory"
    version: str = "1.0.0"
    description: str = "Selective memory lifecycle, candidate evaluation, and privacy management."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("memories", "memory_candidates", "memory_privacy")
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/memories", "/api/v1/settings/memory-privacy")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ({"label": "Memory", "href": "/knowledge/memory"},)
    settings_schema: dict[str, object] | None = None


descriptor = MemoryDescriptor()
