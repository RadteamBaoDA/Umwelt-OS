"""SDK-native, stateless inbound MCP server for approved read-only tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.mcpserver.context import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent, Tool, ToolAnnotations
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

from core.config import Settings
from core.tools.registry import ToolRegistry
from core.tools.schemas import ToolExecutionPrincipal, ToolResult, ToolRisk
from modules.tools.mcp_admission import McpAdmission
from modules.tools.mcp_auth import InboundMcpGuard, InboundRequestState
from modules.tools.mcp_schemas import InboundBinding, InboundPrincipal


@dataclass(frozen=True)
class McpServerBundle:
    """Return the constructed SDK server and its independently guarded ASGI child."""

    server: ScopedMcpServer
    guarded_asgi_app: ASGIApp


class ScopedMcpServer(MCPServer):
    """Expose only the current client's reviewed native read-only tool bindings."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        authenticate_inbound: Callable[[str], Awaitable[InboundPrincipal]],
        authorize_inbound: Callable[[InboundPrincipal], Awaitable[ToolExecutionPrincipal | None]],
        revalidate_inbound: Callable[[InboundPrincipal, ToolExecutionPrincipal], Awaitable[bool]],
        revalidate_inbound_output: Callable[..., Awaitable[bool]],
    ) -> None:
        """Retain injected services/callbacks and install the public SDK message boundary."""
        self.registry = registry
        self.session_factory = session_factory
        self.redis = redis
        self.runtime_settings = settings
        self.authenticate_inbound = authenticate_inbound
        self.authorize_inbound = authorize_inbound
        self.revalidate_inbound = revalidate_inbound
        self.revalidate_inbound_output = revalidate_inbound_output
        self._request_state: ContextVar[InboundRequestState | None] = ContextVar(
            "bbd_mcp_request_state", default=None,
        )
        super().__init__(
            name="bbd-os",
            version="1.0.0",
            instructions="Authenticated read-only search and knowledge tools.",
            tools=[],
            resources=[],
            subscriptions=False,
            middleware=[self._scoped_message_boundary],
        )

    async def _scoped_message_boundary(
        self, ctx: ServerRequestContext, call_next: CallNext,
    ) -> HandlerResult:
        """Load server-owned ASGI identity, reject unsupported protocol features, and scope each SDK task."""
        request = ctx.request
        state = (
            request.scope.get("state", {}).get("bbd_mcp_authorization")
            if request is not None else None
        )
        if not isinstance(state, InboundRequestState):
            raise MCPError(code=-32001, message="Inbound authorization is unavailable")

        method = ctx.method
        supported = {
            "initialize", "notifications/initialized", "notifications/cancelled", "ping",
            "server/discover", "tools/list", "tools/call",
        }
        if method not in supported:
            raise MCPError(code=-32601, message="Method is unavailable")
        if method == "tools/call":
            # Register before the first authorization await so disconnect cleanup can always find this SDK task.
            state.native_task = asyncio.current_task()
        params = ctx.params or {}
        if method == "tools/list" and params.get("cursor"):
            raise MCPError(code=-32602, message="Pagination is unavailable")
        if method == "tools/call" and any(
            key in params for key in ("task", "requestState", "request_state", "inputResponses", "input_responses")
        ):
            raise MCPError(code=-32602, message="This request mode is unavailable")
        if not await self.revalidate_inbound(state.identity, state.expected):
            raise MCPError(code=-32003, message="Inbound authorization changed")

        token = self._request_state.set(state)
        try:
            result = await call_next(ctx)
            if method == "tools/list":
                for binding in state.response_fence.catalog_bindings:
                    definition = self.registry.get_tool(binding.name, binding.version)
                    if (
                        definition is None
                        or definition.schema_fingerprint != binding.schema_fingerprint
                        or definition.risk != ToolRisk.READ_ONLY
                        or definition.confirmation_required
                    ):
                        raise MCPError(code=-32003, message="Tool catalog changed")
            return result
        finally:
            # SDK stateless tasks do not inherit the outer ASGI context; always reset this task-local value.
            self._request_state.reset(token)

    async def list_tools(self) -> list[Tool]:
        """Return the bounded current catalog intersected with exact inbound grants and source scope."""
        state = self._request_state.get()
        if state is None:
            return []
        principal = state.expected
        if (
            principal.is_owner
            or principal.owner_all_sources
            or not principal.source_ids
            or principal.actor_id != f"mcp-client:{state.identity.client_id}"
            or state.identity.destination_id != f"mcp-client:{state.identity.client_id}"
            or principal.destinations != frozenset({state.identity.destination_id})
            or not principal.capabilities.issubset({"source.read"})
        ):
            return []

        bindings = {
            (binding.name, binding.version, binding.schema_fingerprint): binding
            for binding in state.identity.bindings
            if binding.name in {"search.query", "knowledge.get_document", "knowledge.list_documents"}
            and binding.name in principal.allowed_tools
        }
        definitions = self.registry.list_tools(frozenset(binding.name for binding in bindings.values()))
        tools: list[Tool] = []
        captured: list[InboundBinding] = []
        for definition in definitions:
            binding = bindings.get((definition.name, definition.version, definition.schema_fingerprint))
            if (
                binding is None
                or definition.risk != ToolRisk.READ_ONLY
                or definition.confirmation_required
                or not set(definition.permissions).issubset(principal.capabilities)
            ):
                continue
            tools.append(
                Tool(
                    name=definition.name,
                    description=definition.description,
                    input_schema=definition.input_schema,
                    output_schema={
                        "type": "object",
                        "properties": {
                            "data": definition.output_schema,
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["data", "evidence_refs"],
                        "additionalProperties": False,
                    },
                    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
                )
            )
            captured.append(binding)
        state.response_fence.catalog_bindings = tuple(captured)
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None,
    ) -> CallToolResult:
        """Invoke one exact approved native binding and fence its result before SDK serialization."""
        state = self._request_state.get()
        if state is None or context is None:
            return CallToolResult(content=[TextContent(text='{"error":"forbidden"}')], is_error=True)
        request = context.request_context.request
        if (
            request is None
            or request.scope.get("state", {}).get("bbd_mcp_authorization") is not state
            or name not in {"search.query", "knowledge.get_document", "knowledge.list_documents"}
            or name not in state.expected.allowed_tools
            or not state.expected.source_ids
            or state.expected.is_owner
            or state.expected.owner_all_sources
        ):
            return CallToolResult(content=[TextContent(text='{"error":"forbidden"}')], is_error=True)
        binding = next((item for item in state.identity.bindings if item.name == name), None)
        definition = self.registry.get_tool(name, binding.version) if binding is not None else None
        if (
            not isinstance(arguments, dict)
            or binding is None
            or definition is None
            or definition.schema_fingerprint != binding.schema_fingerprint
            or definition.risk != ToolRisk.READ_ONLY
            or definition.confirmation_required
            or not set(definition.permissions).issubset(state.expected.capabilities)
            or state.expected.actor_id != f"mcp-client:{state.identity.client_id}"
            or state.identity.destination_id != f"mcp-client:{state.identity.client_id}"
            or state.expected.destinations != frozenset({state.identity.destination_id})
            or not state.expected.capabilities.issubset({"source.read"})
        ):
            return CallToolResult(content=[TextContent(text='{"error":"tool_unavailable"}')], is_error=True)
        try:
            encoded_args = json.dumps(arguments, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return CallToolResult(content=[TextContent(text='{"error":"invalid_arguments"}')], is_error=True)
        if len(encoded_args) > min(64_000, definition.max_arguments_bytes):
            return CallToolResult(content=[TextContent(text='{"error":"invalid_arguments"}')], is_error=True)

        sink: dict[str, Any] = {"records": [], "source_generations": {}}
        native_context = {
            "session_factory": self.session_factory,
            "redis": self.redis,
            "settings": self.runtime_settings,
            "destination_id": state.identity.destination_id,
            "destination_kind": "remote",
            "output_fence_sink": sink,
            "principal_revalidator": self._revalidate_active_principal,
        }
        try:
            result: ToolResult = await self.registry.invoke_tool(
                name,
                arguments,
                state.expected,
                version=binding.version,
                context=native_context,
            )
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            return CallToolResult(content=[TextContent(text='{"error":"execution_failed"}')], is_error=True)
        if not result.success:
            code = result.error_code if result.error_code in {
                "tool_unavailable", "forbidden", "approval_required", "invalid_arguments",
                "invalid_result", "timeout", "execution_failed",
            } else "execution_failed"
            text = json.dumps(
                {"success": False, "error": {"code": code, "message": "Tool execution failed"}},
                separators=(",", ":"),
            )
            return CallToolResult(content=[TextContent(text=text)], is_error=True)

        try:
            current = await self.revalidate_inbound_output(
                state.identity, state.expected, binding=binding, sink=sink,
            )
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            current = False
        if not current:
            return CallToolResult(
                content=[TextContent(text='{"success":false,"error":{"code":"forbidden","message":"Tool execution failed"}}')],
                is_error=True,
            )

        structured = {"data": result.data, "evidence_refs": list(result.evidence_refs)}
        try:
            text = json.dumps(
                {"success": True, **structured}, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            return CallToolResult(
                content=[TextContent(text='{"success":false,"error":{"code":"invalid_result","message":"Tool execution failed"}}')],
                is_error=True,
            )
        # The same sink and reviewed binding are required again at the final ASGI send boundary.
        state.response_fence.binding = binding
        state.response_fence.sink = sink
        state.response_fence.has_successful_native_result = True
        return CallToolResult(content=[TextContent(text=text)], structured_content=structured, is_error=False)

    async def _revalidate_active_principal(self, principal: ToolExecutionPrincipal) -> bool:
        """Revalidate only the principal bound to this SDK task's authenticated request state."""
        state = self._request_state.get()
        if state is None or principal != state.expected:
            return False
        return await self.revalidate_inbound(state.identity, state.expected)


def create_inbound_mcp_bundle(
    *,
    registry: ToolRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    admission: McpAdmission,
    audience: str,
    expected_host: str,
    allowed_origins: frozenset[str],
    authenticate_inbound: Callable[[str], Awaitable[InboundPrincipal]],
    authorize_inbound: Callable[[InboundPrincipal], Awaitable[ToolExecutionPrincipal | None]],
    revalidate_inbound: Callable[[InboundPrincipal, ToolExecutionPrincipal], Awaitable[bool]],
    revalidate_inbound_output: Callable[..., Awaitable[bool]],
) -> McpServerBundle:
    """Create the pinned SDK server and exact-path guarded stateless JSON ASGI app.

    `authenticate_inbound(raw)` verifies the opaque bearer against the frozen
    audience and returns a detached identity. `authorize_inbound(identity)`
    derives the current non-owner principal; `revalidate_inbound(identity,
    expected)` returns whether that exact scope remains current. The output
    callback is awaited as `revalidate_inbound_output(identity, expected,
    binding=binding, sink=sink)`; `binding` and `sink` are keyword-only and
    server-created. No callback may retain a SQL session across a tool await.

    The root FastAPI lifespan must run `bundle.server.session_manager.run()` once;
    mounting this guarded child does not enter the SDK manager lifespan.
    """
    server = ScopedMcpServer(
        registry=registry,
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        authenticate_inbound=authenticate_inbound,
        authorize_inbound=authorize_inbound,
        revalidate_inbound=revalidate_inbound,
        revalidate_inbound_output=revalidate_inbound_output,
    )
    child = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        max_request_body_size=128_000,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[expected_host],
            allowed_origins=list(allowed_origins),
        ),
    )
    guarded = InboundMcpGuard(
        child,
        audience=audience,
        expected_host=expected_host,
        allowed_origins=allowed_origins,
        admission=admission,
        authenticate_inbound=authenticate_inbound,
        authorize_inbound=authorize_inbound,
        revalidate_inbound=revalidate_inbound,
        revalidate_inbound_output=revalidate_inbound_output,
    )
    return McpServerBundle(server=server, guarded_asgi_app=guarded)
