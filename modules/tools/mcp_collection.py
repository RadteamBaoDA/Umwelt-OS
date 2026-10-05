"""Owner-fenced single read of one collection-reviewed MCP capability for the connector adapter.

This is the only path that executes a ``purpose="collection"`` grant. It reuses the reviewed SDK
client, admission slots, egress/credential handling and durable grant fences of chat dispatch, but
never registers a native tool: collection arguments come from owner-saved source configuration,
never from model or UI free text, and stdio launch material stays in the admin profile catalog.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.tools.validator import validate_json_schema
from modules.sources import public as sources_public
from modules.tools import mcp_repository
from modules.tools.mcp_runtime import McpRuntime
from modules.tools.mcp_schemas import McpRisk, McpTransport

# Collection ingests into the local store; the reviewed grant must name this destination.
COLLECTION_DESTINATION = "local"


@dataclass(frozen=True)
class McpCollectionRead:
    """Detached provenance and bounded JSON payload from one reviewed capability read."""

    connection_id: UUID
    grant_id: UUID
    grant_revision: int
    connection_revision: int
    kind: str
    remote_key: str
    descriptor_hash: str
    payload: dict[str, Any]


async def read_collection_capability(
    runtime: McpRuntime,
    owner_id: int,
    *,
    connection_id: UUID,
    grant_id: UUID,
    source_id: UUID,
    source_generation: int,
    arguments: dict[str, Any],
) -> McpCollectionRead:
    """Read one tool or resource through a collection grant scoped to exactly ``source_id``.

    Raises McpNotFound/McpConflict/McpUnavailable (or a transport error) when the connection is
    disabled, the grant is revoked, expired or stale, the descriptor drifted, the source changed
    generation, or arguments do not satisfy the reviewed input schema. Nothing is sent in those
    cases. Every outbound request and the final result return re-run the same checks.
    """
    fence = await runtime.resolve_fence(owner_id, connection_id, grant_id, COLLECTION_DESTINATION)
    if fence.purpose != "collection" or fence.source_ids != (source_id,):
        raise mcp_repository.McpConflict("Grant is not a collection grant for this source")
    async with runtime.session_factory() as session:
        selection = await mcp_repository.get_current_selection(session, owner_id, connection_id)
    if selection is None:
        raise mcp_repository.McpUnavailable("MCP discovery is unavailable")
    connection, discovery, grants = selection
    grant = next((item for item in grants if item.id == grant_id), None)
    capability = next((item for item in discovery.capabilities if grant and item.id == grant.capability_id), None)
    if grant is None or capability is None or capability.kind not in {"tool", "resource"}:
        raise mcp_repository.McpConflict("Collection capability is not currently granted")
    if capability.kind == "tool":
        schema = capability.descriptor.get("inputSchema")
        if not isinstance(schema, dict) or validate_json_schema(arguments, schema):
            raise mcp_repository.McpConflict("Collection arguments do not match the reviewed input schema")
    elif arguments:
        raise mcp_repository.McpConflict("Collection resources accept no arguments")

    async def authorized() -> bool:
        """Re-read durable grant, connection, profile and source state; any drift denies the request."""
        if not await runtime.admission.lease_current():
            return False
        try:
            async with runtime.session_factory() as session:
                if not await mcp_repository.revalidate_capability_fence(session, owner_id, fence):
                    return False
                current = await mcp_repository.get_current_selection(session, owner_id, connection_id)
                source = await sources_public.get_source_fence(session, source_id)
            if current is None or source is None:
                return False
            live, live_discovery, live_grants = current
            live_grant = next((item for item in live_grants if item.id == grant_id), None)
            return bool(
                source.status == "active" and source.generation == source_generation
                and live.enabled and live.revision == fence.connection_revision
                and live_discovery.id == fence.discovery_id
                and live_grant is not None and live_grant.purpose == "collection"
                and live_grant.risk == McpRisk.READ_ONLY
                and live_grant.grant_revision == fence.grant_revision
                and live_grant.source_ids == (source_id,)
                and (live.transport != McpTransport.STDIO or (
                    live.deployment_profile_id is not None
                    and runtime.profile_catalog.get_identity(live.deployment_profile_id)
                    == fence.deployment_profile_hash
                ))
            )
        except Exception:
            return False

    if capability.kind == "tool":
        payload = await runtime.client.call_tool(owner_id, fence, arguments, authorized)
    else:
        payload = await runtime.client.read_resource(owner_id, fence, "resource", authorized)
    return McpCollectionRead(
        connection_id=connection.id, grant_id=grant.id, grant_revision=grant.grant_revision,
        connection_revision=connection.revision, kind=capability.kind,
        remote_key=capability.remote_key, descriptor_hash=capability.descriptor_hash, payload=payload,
    )
