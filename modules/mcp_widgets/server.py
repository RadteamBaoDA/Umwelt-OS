"""Widget MCP server: skills resources, prompts, assemble_document; ASGI bearer guard; stdio entry point."""

import hmac
import json

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import Settings
from modules.chat.public import load_generative_ui_skill
from modules.mcp_widgets.assemble import assemble_document

# Public skill name -> bundled chat skill folder. Only these names are ever resolved (allowlist).
SKILLS: dict[str, str] = {
    "master-playbook": "master-agent-playbook",
    "svg-diagrams": "svg-diagram-skill",
    "advanced-visualization": "agent-skills-vol2",
}


def load_skill(name: str) -> str:
    """Return the text of an allowlisted skill.

    Raises:
        ValueError: If name is not one of SKILLS (this also rejects path traversal attempts).
    """
    if name not in SKILLS:
        raise ValueError(f"Unknown skill: {name!r}")
    return load_generative_ui_skill(SKILLS[name])


def parent_origin_for(settings: Settings) -> str:
    """Resolve the bridge target origin: explicit MCP_WIDGETS_PARENT_ORIGIN, else PUBLIC_ORIGIN."""
    return settings.mcp_widgets_parent_origin or str(settings.public_origin).rstrip("/")


def create_widget_server(settings: Settings) -> MCPServer:
    """Build the MCP server object shared by the HTTP and stdio transports."""
    server = MCPServer(
        "umwelt-widgets", instructions="Create sandbox-safe interactive widgets and diagrams."
    )
    origin = parent_origin_for(settings)

    @server.resource("skills://list", mime_type="application/json")
    def skills_list() -> str:
        """JSON array of available skill names."""
        return json.dumps(sorted(SKILLS))

    @server.resource("skills://{name}", mime_type="text/plain")
    def skill(name: str) -> str:
        """Full text of one allowlisted skill document."""
        return load_skill(name)

    @server.prompt(
        name="create_widget", description="Instructions for creating interactive HTML widgets"
    )
    def create_widget() -> str:
        """Master playbook text."""
        return load_skill("master-playbook")

    @server.prompt(
        name="create_svg_diagram", description="Instructions for creating inline SVG diagrams"
    )
    def create_svg_diagram() -> str:
        """SVG diagram skill text."""
        return load_skill("svg-diagrams")

    @server.prompt(name="create_visualization", description="Advanced visualization instructions")
    def create_visualization() -> str:
        """Advanced visualization skill text."""
        return load_skill("advanced-visualization")

    @server.tool(
        name="assemble_document",
        description="Wrap an HTML fragment in a CSP-locked standalone document with theme CSS and a postMessage bridge.",
    )
    def assemble_document_tool(title: str, description: str, html: str) -> str:
        """Return the complete standalone HTML5 document (see assemble.assemble_document for limits)."""
        return assemble_document(title, description, html, parent_origin=origin)

    return server


class BearerGuard:
    """ASGI wrapper rejecting requests without the exact configured bearer token (constant-time compare)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        """Wrap app; token must be non-empty."""
        if not token:
            raise ValueError("MCP widgets bearer token is empty")
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward authorized HTTP requests; answer 401 otherwise."""
        if scope["type"] == "http":
            header = Request(scope).headers.get("authorization", "")
            scheme, _, supplied = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                supplied.strip().encode(), self._token
            ):
                await PlainTextResponse("unauthorized", 401, {"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self._app(scope, receive, send)


def create_http_app(settings: Settings) -> tuple[MCPServer, ASGIApp]:
    """Return (server, guarded ASGI app) for mounting at /mcp.

    Raises:
        RuntimeError: If the bearer token is unset (refuse to serve unauthenticated).
    """
    token = settings.mcp_widgets_token.get_secret_value()
    if not token:
        raise RuntimeError("MCP_WIDGETS_ENABLED requires MCP_WIDGETS_TOKEN")
    server = create_widget_server(settings)
    # Bearer auth (no ambient cookies) makes DNS-rebinding protection redundant behind arbitrary proxy Host headers.
    child = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        max_request_body_size=512_000,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return server, BearerGuard(child, token)


def main() -> None:
    """stdio entry point: `python -m modules.mcp_widgets` for desktop MCP clients."""
    create_widget_server(Settings()).run("stdio")
