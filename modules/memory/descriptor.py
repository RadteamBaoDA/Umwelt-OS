"""Declare memory module ownership, public capabilities, dependencies, and routes."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class MemoryDescriptor(ModuleDescriptor):
    """Declare selective memory lifecycle, candidates evaluation, and privacy management capabilities."""

    id: str = "memory"
    name: str = "Memory"
    version: str = "1.0.0"
    description: str = "Selective memory lifecycle, candidate evaluation, and privacy management."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ("chat", "knowledge.documents", "sources")
    provides: tuple[str, ...] = ("memories", "memory_candidates", "memory_privacy")
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/memories", "/api/v1/settings/memory-privacy")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ({"label": "Memory", "href": "/knowledge/memory"},)
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.memory.routes", 450),
    )
    erasure_module: str | None = "modules.memory.erasure"
    erasure_order: int = 130


descriptor = MemoryDescriptor()
