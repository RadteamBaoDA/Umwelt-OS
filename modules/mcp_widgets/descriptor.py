"""Platform module descriptor for mcp_widgets: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor


@dataclass(frozen=True)
class McpWidgetsDescriptor(ModuleDescriptor):
    """Declare mcp_widgets routers and erasure ownership for application composition."""

    id: str = "mcp_widgets"
    name: str = "MCP widgets"
    description: str = (
        "Optional bearer-guarded widget MCP server mounted at /mcp when MCP_WIDGETS_ENABLED is set."
    )
    routes: tuple[str, ...] = ("/mcp",)


descriptor = McpWidgetsDescriptor()
