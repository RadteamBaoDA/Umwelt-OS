"""Native tools module descriptor contributed to application composition."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ToolsDescriptor(ModuleDescriptor):
    """Declare the protected native registry, owner management API, and guarded inbound mount."""
    id: str = "tools"
    name: str = "Tools"
    version: str = "1.0.0"
    description: str = "Bounded registered native tool dispatch."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources", "knowledge.documents", "search")
    optional_dependencies: tuple[str, ...] = ("agents", "connectors", "knowledge.timeline")
    provides: tuple[str, ...] = ("tool_registry", "mcp_runtime", "mcp_inbound_server")
    requires: tuple[str, ...] = ("sources", "documents", "search")
    routes: tuple[str, ...] = ("/api/v1/tools", "/api/v1/mcp/connections", "/api/v1/mcp/inbound-clients", "/api/v1/mcp/")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("knowledge.get_document", "knowledge.list_documents", "search.query", "sources.list_sources", "sources.get_source", "github.list_project_events", "webhook.send", "browser.read")
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.tools.browser_control", 170),
        RouterSpec("modules.tools.routes:browser_jobs_router", 490),
        RouterSpec("modules.tools.routes", 500, when_enabled=True),
        RouterSpec("modules.tools.mcp_management_routes", 510, when_enabled=True),
    )
    erasure_module: str | None = "modules.tools.erasure"
    erasure_order: int = 220


descriptor = ToolsDescriptor()
