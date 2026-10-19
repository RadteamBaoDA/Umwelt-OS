"""Widget MCP server: skills resources, prompts, assemble_document; ASGI bearer guard; stdio entry point."""

import hmac
import json

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import Settings
from modules.chat.public import load_generative_ui_skill
from modules.mcp_widgets.assemble import assemble_document

# Public skill name (upstream OpenIntelligentUI names) -> bundled chat skill folder. Allowlist only.
SKILLS: dict[str, str] = {
    "master-agent-playbook": "master-agent-playbook",
    "svg-diagram-skill": "svg-diagram-skill",
    "agent-skills-vol2": "agent-skills-vol2",
}
# Earlier Umwelt-OS names stay resolvable but are not advertised by skills://list.
SKILL_ALIASES: dict[str, str] = {
    "master-playbook": "master-agent-playbook",
    "svg-diagrams": "svg-diagram-skill",
    "advanced-visualization": "agent-skills-vol2",
}


def load_skill(name: str) -> str:
    """Return the text of an allowlisted skill (upstream name or alias).

    Raises:
        ValueError: If name is not in SKILLS or SKILL_ALIASES (this also rejects path traversal attempts).
    """
    canonical = SKILL_ALIASES.get(name, name)
    if canonical not in SKILLS:
        raise ValueError(f"Unknown skill: {name!r}")
    return load_generative_ui_skill(SKILLS[canonical])


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
        return load_skill("master-agent-playbook")

    @server.prompt(
        name="create_svg_diagram", description="Instructions for creating inline SVG diagrams"
    )
    def create_svg_diagram() -> str:
        """SVG diagram skill text."""
        return load_skill("svg-diagram-skill")

    @server.prompt(name="create_visualization", description="Advanced visualization instructions")
    def create_visualization() -> str:
        """Advanced visualization skill text."""
        return load_skill("agent-skills-vol2")

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
            if (
                scope["method"] == "GET"
                and scope["path"].removeprefix(scope.get("root_path", "")) == "/health"
            ):
                # Liveness only: reveals nothing but status, so no token is needed.
                await JSONResponse({"status": "ok"})(scope, receive, send)
                return
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
    guarded: ASGIApp = BearerGuard(child, token)
    origins = allowed_origins_for(settings)
    if origins:
        # Outermost, so preflights (which carry no Authorization header) are answered before the bearer check.
        guarded = CORSMiddleware(
            guarded,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "authorization",
                "content-type",
                "accept",
                "mcp-session-id",
                "mcp-protocol-version",
            ],
            expose_headers=["mcp-session-id"],
        )
    return server, guarded


def allowed_origins_for(settings: Settings) -> list[str]:
    """Return the exact CORS origins from MCP_WIDGETS_ALLOWED_ORIGINS (comma separated; empty = no CORS)."""
    return [
        o.strip().rstrip("/") for o in settings.mcp_widgets_allowed_origins.split(",") if o.strip()
    ]


class RootPath:
    """ASGI wrapper serving the guarded app for the exact path `/mcp` (no trailing slash, no redirect)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the guarded app that expects requests at `/`."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite the path to `/` and forward."""
        await self._app({**scope, "path": "/", "raw_path": b"/"}, receive, send)


def main() -> None:
    """stdio entry point: `python -m modules.mcp_widgets` for desktop MCP clients."""
    create_widget_server(Settings()).run("stdio")
