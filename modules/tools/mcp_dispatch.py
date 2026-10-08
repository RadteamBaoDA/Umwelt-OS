"""Register persisted MCP selections as native bounded tool handlers."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

from core.tools.registry import ToolRegistry
from core.tools.schemas import ToolDefinition, ToolExecutionPrincipal, ToolRisk
from core.workspaces.schemas import Scope
from modules.tools import mcp_repository
from modules.tools.mcp_client import McpSdkClient
from modules.tools.mcp_schemas import (
    CapabilityRead,
    ConnectionRead,
    DiscoveryRead,
    ExecutionFence,
    GrantRead,
    McpRisk,
)
from modules.tools.mcp_transport import McpTransportError


def build_mcp_tool_definition(
    connection: ConnectionRead,
    discovery_id: UUID,
    capability: CapabilityRead,
    grant: GrantRead,
) -> ToolDefinition:
    """Project one current reviewed capability into its bounded native registry contract.

    The exact name/version, schema, limits and permissions bind the durable review identity.
    Callers must supply detached owner DTOs; the projection performs no I/O or authorization.

    Raises McpTransportError for invalid read-only chat schemas and McpUnavailable for
    resource-template capabilities, which have no owner-bound expansion contract.
    """
    if grant.risk != McpRisk.READ_ONLY or grant.purpose != "chat":
        raise McpTransportError("Only reviewed chat read capabilities can enter native tool dispatch")
    if capability.kind == "tool":
        input_schema = capability.descriptor.get("inputSchema")
        if not isinstance(input_schema, dict):
            raise McpTransportError("Selected MCP tool has no valid input schema")
    elif capability.kind == "resource":
        input_schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    else:
        raise mcp_repository.McpUnavailable("MCP resource-template expansion is not owner-bound")
    description = capability.descriptor.get("description", "")
    if not isinstance(description, str):
        description = ""
    review_identity = (
        f"{grant.id.hex}:{grant.grant_revision}:{discovery_id.hex}:{capability.descriptor_hash}"
    )
    # Keep the opaque catalog version under ToolDefinition's 40-character limit while
    # binding the complete reviewed identity instead of relying on handler object identity.
    contract_version = f"review-{hashlib.sha256(review_identity.encode('ascii')).hexdigest()[:33]}"
    return ToolDefinition(
        name=f"mcp.{connection.id.hex}.{capability.id.hex}",
        version=contract_version,
        description=description[:2_000],
        input_schema=input_schema,
        output_schema={},
        risk=ToolRisk.READ_ONLY,
        confirmation_required=False,
        timeout_seconds=float(connection.timeout_seconds),
        max_arguments_bytes=64_000,
        max_result_bytes=256_000,
        permissions=(),
        module="tools",
    )


class McpDispatchAdapter:
    """Bridge exact owner-reviewed MCP read selections into the canonical native registry."""

    def __init__(
        self,
        registry: ToolRegistry,
        client: McpSdkClient,
        *,
        resolve_fence: Callable[[Scope, UUID, UUID, str], Awaitable[ExecutionFence]],
        revalidate_fence: Callable[[Scope, ExecutionFence], Awaitable[bool]],
    ) -> None:
        """Install the native registry and server-owned fresh MCP/source authorization callbacks.

        Fence callbacks must use fresh short owner transactions, compose current source privacy and
        deletion checks, close the transaction before returning, and never derive authority from tool args.
        Dispatch also requires the registry's typed principal revalidator and awaits it together with
        this owner fence before each outbound request and again before returning a result.
        """
        self._registry = registry
        self._client = client
        self._resolve_fence = resolve_fence
        self._revalidate_fence = revalidate_fence
        self._registered: dict[UUID, set[str]] = {}
        self._workspace_of: dict[UUID, UUID] = {}
        registry.hides_tool = self.hides

    def register_selected_capabilities(
        self,
        workspace_id: UUID,
        connection: ConnectionRead,
        discovery: DiscoveryRead,
        grants: tuple[GrantRead, ...],
    ) -> tuple[str, ...]:
        """Replace one connection's native projection with current, exact, read-only reviewed tools/resources."""
        self.unregister_connection(connection.id)
        if (not connection.enabled or discovery.connection_id != connection.id
                or discovery.connection_revision != connection.revision):
            return ()
        grants_by_capability = {grant.capability_id: grant for grant in grants}
        registered: set[str] = set()
        try:
            for capability in discovery.capabilities:
                grant = grants_by_capability.get(capability.id)
                if grant is None:
                    continue
                if (
                    grant.connection_id != connection.id
                    or grant.descriptor_hash != capability.descriptor_hash
                    or grant.reviewed_connection_revision != connection.revision
                    or grant.revoked_at is not None
                    or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC))
                    or grant.risk != McpRisk.READ_ONLY
                    or grant.purpose != "chat"
                    or capability.kind not in {"tool", "resource"}
                ):
                    continue
                definition = self._build_definition(connection, discovery.id, capability, grant)
                handler = self._build_async_handler(
                    workspace_id,
                    connection.id,
                    capability.id,
                    grant.id,
                    capability.kind,
                    capability.remote_key,
                    capability.descriptor_hash,
                )
                self._registry.register_tool(definition, handler)
                registered.add(definition.name)
        except Exception:
            for name in registered:
                self._registry.unregister_tool(name)
            raise
        self._registered[connection.id] = registered
        self._workspace_of[connection.id] = workspace_id
        return tuple(sorted(registered))

    def unregister_connection(self, connection_id: UUID) -> None:
        """Remove native handlers for one connection while durable fences protect already-running calls."""
        self._workspace_of.pop(connection_id, None)
        for name in self._registered.pop(connection_id, set()):
            self._registry.unregister_tool(name)

    def hides(self, name: str, workspace_id: UUID) -> bool:
        """True when ``name`` is an MCP tool owned by another workspace (or unknown); native tools are never hidden."""
        parts = name.split(".")
        if len(parts) != 3 or parts[0] != "mcp":
            return False
        try:
            return self._workspace_of.get(UUID(hex=parts[1])) != workspace_id
        except ValueError:
            return True

    def _build_definition(
        self,
        connection: ConnectionRead,
        discovery_id: UUID,
        capability: CapabilityRead,
        grant: GrantRead,
    ) -> ToolDefinition:
        """Project one discovery/grant review identity into a bounded native read-only contract.

        Version binds the reviewed grant UUID/revision, discovery UUID, and descriptor hash so
        revocation/reselection publishes a distinguishable native contract. The remote key remains
        a separate SDK target and is never replaced with this local registry identity.
        """
        return build_mcp_tool_definition(connection, discovery_id, capability, grant)

    def _build_async_handler(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        capability_id: UUID,
        grant_id: UUID,
        capability_kind: str,
        remote_key: str,
        descriptor_hash: str,
    ) -> Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]:
        """Bind immutable selection identity to the async method accepted by ToolRegistry."""
        return partial(
            self._invoke_tool,
            workspace_id,
            connection_id,
            capability_id,
            grant_id,
            capability_kind,
            remote_key,
            descriptor_hash,
        )

    async def _revalidate_execution(
        self,
        scope: Scope,
        fence: ExecutionFence,
        principal: ToolExecutionPrincipal,
        destination_id: str,
        registered_tool_name: str,
        principal_revalidator: Callable[[ToolExecutionPrincipal], Awaitable[bool]],
    ) -> bool:
        """Recheck native caller identity/scope and owner source fences before egress or result return.

        The supplied native callback must re-read current caller authentication and source authority.
        The injected owner callback must separately re-read grant, source privacy and deletion state
        inside a short fresh transaction. A non-owner may proceed only when every source in the
        durable grant is within its current caller scope; MCP calls have no provider-enforced
        source intersection, so a partial metadata intersection is not safe to execute.
        Any stale, malformed or exceptional check fails closed; cancellation still propagates.
        """
        if (not principal.actor_id or registered_tool_name not in principal.allowed_tools
                or destination_id != fence.destination_id
                or destination_id not in principal.destinations
                or (principal.owner_all_sources and not principal.is_owner)):
            return False
        grant_sources = frozenset(str(source_id) for source_id in fence.source_ids)
        if not principal.owner_all_sources:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
            if not grant_sources or not grant_sources.issubset(principal.source_ids):
                return False
        try:
            if await principal_revalidator(principal) is not True:
                return False
            return await self._revalidate_fence(scope, fence) is True
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # Guard failures are collapsed to denial so the registry never exposes callback details.
            return False

    async def _invoke_tool(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        capability_id: UUID,
        grant_id: UUID,
        capability_kind: str,
        remote_key: str,
        descriptor_hash: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-resolve the grant, then invoke one SDK read guarded by current caller and owner fences."""
        principal = context.get("principal")
        destination_id = context.get("destination_id")
        principal_revalidator = context.get("principal_revalidator")
        if (not isinstance(principal, ToolExecutionPrincipal) or not principal.actor_id
                or not isinstance(destination_id, str)
                or not callable(principal_revalidator)):
            raise McpTransportError("Native MCP execution context is incomplete")
        if (not principal.is_owner and (principal.owner_all_sources or not principal.source_ids)
                or principal.owner_all_sources and not principal.is_owner
                or destination_id not in principal.destinations):
            raise McpTransportError("Native MCP principal has no source scope")
        # The registry is process-global: another workspace's principal must not reach this connection.
        if principal.scope.workspace_id != workspace_id:
            raise McpTransportError("MCP connection is outside the caller workspace")
        scope = principal.scope
        fence = await self._resolve_fence(scope, connection_id, grant_id, destination_id)
        if (
            fence.connection_id != connection_id
            or fence.grant_id != grant_id
            or fence.descriptor_hash != descriptor_hash
            or fence.remote_capability_key != remote_key
            or fence.destination_id != destination_id
            or fence.purpose != "chat"
        ):
            raise mcp_repository.McpConflict("MCP selection changed after native registration")
        # Partial source intersections have no provider-enforced semantics in this adapter, so
        # reject before the client opens a protocol session rather than letting remote args widen it.
        grant_sources = frozenset(str(source_id) for source_id in fence.source_ids)
        if (not principal.owner_all_sources
                and (not grant_sources or not grant_sources.issubset(principal.source_ids))):
            raise McpTransportError("Current caller scope does not fully contain the reviewed MCP source scope")
        registered_tool_name = f"mcp.{connection_id.hex}.{capability_id.hex}"
        before_request = partial(
            self._revalidate_execution,
            scope,
            fence,
            principal,
            destination_id,
            registered_tool_name,
            principal_revalidator,
        )
        if capability_kind == "tool":
            return await self._client.call_tool(
                scope,
                fence,
                arguments,
                before_request,
            )
        if capability_kind == "resource":
            if arguments:
                raise McpTransportError("Selected MCP resource does not accept caller-supplied URI arguments")
            return await self._client.read_resource(
                scope,
                fence,
                capability_kind,
                before_request,
            )
        raise mcp_repository.McpUnavailable("MCP capability kind is not dispatchable")
