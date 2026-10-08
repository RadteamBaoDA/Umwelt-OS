"""Compose owner-scoped MCP transport callbacks with the native registry."""

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.realtime import commit_with_replay
from core.tools import ToolDestination, ToolExecutionPrincipal, ToolRisk
from core.tools.registry import ToolRegistry
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.sources import public as sources_public
from modules.tools import mcp_repository
from modules.tools.mcp_admission import McpAdmission
from modules.tools.mcp_client import McpSdkClient
from modules.tools.mcp_dispatch import McpDispatchAdapter, build_mcp_tool_definition
from modules.tools.mcp_profiles import StdioProfileCatalog
from modules.tools.mcp_schemas import (
    ConnectionRead,
    DiscoveryPersist,
    DiscoveryRead,
    ExecutionFence,
    InboundBinding,
    InboundPrincipal,
    McpRisk,
    McpTransport,
)
from modules.tools.mcp_stdio import StdioDeploymentProfile
from modules.tools.models import McpConnection


def derive_mcp_public_endpoint(settings: Settings) -> tuple[str, str, str]:
    """Return the one normalized MCP audience, Host authority, and browser Origin.

    The validated Pydantic URL owns IDNA and default-port normalization. Credentials,
    non-origin URL components, scoped IPv6 and port zero are rejected; the canonical
    audience retains its trailing slash and is bounded to the persisted audience field.
    """
    public_url = str(settings.public_origin)
    parts = urlsplit(public_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or "%" in parts.hostname
    ):
        raise ValueError("MCP public origin must be an unscoped origin without credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("MCP public origin has an invalid port") from exc
    if port == 0:
        raise ValueError("MCP public origin port must be between 1 and 65535")
    authority = parts.netloc
    if not authority:
        raise ValueError("MCP public origin has no authority")
    origin = f"{parts.scheme}://{authority}"
    audience = f"{origin}/api/v1/mcp/"
    if len(audience) > 255:
        raise ValueError("MCP public audience exceeds its persisted length limit")
    return audience, authority, origin


logger = logging.getLogger(__name__)


class McpRuntime:
    """Own finite MCP client/dispatch composition and short-session authorization fences.

    Construction installs callbacks only: it performs no provider request or database I/O.
    Persistence owner methods are called in fresh sessions, mutations commit before returning,
    and registrations use detached reviewed snapshots without becoming durable authority.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        redis: Redis,
        settings: Settings,
        admission: McpAdmission,
        approved_destination_cidrs: Mapping[tuple[str, str, int], tuple[str, ...]] | None = None,
    ) -> None:
        """Wire the reviewed SDK adapter and dispatcher to owner-scoped callbacks.

        The registry, session factory, Redis client, settings and deployment-owned CIDRs
        are injected by application composition. No SQL session, raw inbound identity,
        token, provider client or global execution principal is retained here. The optional
        manifest is loaded once as deployment authority; its launch fields stay private to the
        profile catalog and are passed to the client only as detached copies.
        """
        self.registry = registry
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.admission = admission
        self.profile_catalog = StdioProfileCatalog(settings.mcp_stdio_profile_manifest)
        self.audience, self.authority, self.origin = derive_mcp_public_endpoint(settings)
        self.client = McpSdkClient(
            load_connection=self.load_connection,
            record_draft_check=self.record_draft_check,
            persist_discovery=self.persist_discovery,
            connection_is_current=self.connection_is_current,
            operation_slot=admission.operation_slot,
            resolve_stdio_profile=self.resolve_stdio_profile,
            approved_destination_cidrs=approved_destination_cidrs or {},
        )
        self.dispatch = McpDispatchAdapter(
            registry,
            self.client,
            resolve_fence=self.resolve_fence,
            revalidate_fence=self.revalidate_fence,
        )

    async def _admit(self, session: AsyncSession, scope: Scope, *, lock: bool = False) -> AccessFence:
        """Owner admission; lock only in short commit sessions, never across MCP network I/O."""
        return await mcp_repository.admit(
            session, scope=scope, multi_workspace_enabled=self.settings.multi_workspace_enabled, lock=lock,
        )

    async def authenticate_inbound(self, raw: str) -> InboundPrincipal:
        """Verify one bearer against the canonical audience and return detached identity only.

        Verification runs in a short owner-repository session. Repository denial is left
        for the HTTP boundary to map to its generic authentication response; this callback
        never persists or retains the raw bearer.
        """
        async with self.session_factory() as session:
            return await mcp_repository.verify_inbound_client(session, raw, self.audience)

    async def authorize_inbound(self, identity: InboundPrincipal) -> ToolExecutionPrincipal | None:
        """Build current non-owner authority from exact bindings and active remote-safe sources.

        Only the fixed Search/Knowledge read tools are eligible. Registry module state,
        READ_ONLY risk, confirmation policy, exact version/fingerprint, permission subset,
        current persisted client scope and the bounded Sources projection must all agree.
        Invalid, stale or uncertain identities return None. A valid identity with no eligible
        source or binding receives a typed non-owner principal with both scopes empty, allowing
        catalog/control responses to be revalidated while native tool calls remain denied.
        """
        if not isinstance(identity, InboundPrincipal):
            return None
        try:
            flag = self.settings.multi_workspace_enabled
            async with self.session_factory() as session:
                if not await mcp_repository.revalidate_inbound_principal(session, identity):
                    return None
                owner = await workspaces.resolve_workspace_owner_context(
                    session, identity.workspace_id, multi_workspace_enabled=flag,
                )
            if owner is None or owner.user_id != identity.owner_id:
                return None
            scope = InternalJobScope(identity.workspace_id, owner.user_id, owner.membership_revision)

            source_ids = frozenset(identity.source_ids)
            if len(source_ids) > 100:
                return None
            async with self.session_factory() as session:
                page = await sources_public.list_tool_sources(
                    session,
                    scope=scope,
                    multi_workspace_enabled=flag,
                    limit=100,
                    cursor=None,
                    source_ids=source_ids,
                    owner_all=False,
                    destination=ToolDestination.REMOTE,
                )
            if page.next_cursor is not None:
                return None
            effective_source_ids = frozenset(str(item.id) for item in page.items)

            supported_names = frozenset({
                "knowledge.get_document",
                "knowledge.list_documents",
                "search.query",
            })
            capabilities = frozenset(identity.capabilities) & frozenset({"source.read"})
            definitions = {
                definition.name: definition
                for definition in self.registry.list_tools(allowed_tools=supported_names)
            }
            allowed_names: set[str] = set()
            for binding in identity.bindings:
                definition = definitions.get(binding.name)
                if (
                    binding.name in supported_names
                    and definition is not None
                    and definition.version == binding.version
                    and definition.schema_fingerprint == binding.schema_fingerprint
                    and definition.risk == ToolRisk.READ_ONLY
                    and not definition.confirmation_required
                    and set(definition.permissions).issubset(capabilities)
                ):
                    allowed_names.add(binding.name)
            if not effective_source_ids or not allowed_names:
                # Empty active scope must not advertise otherwise-valid tool contracts.
                effective_source_ids = frozenset()
                allowed_names.clear()
            return ToolExecutionPrincipal(
                actor_id=f"mcp-client:{identity.client_id}",
                scope=scope,
                is_owner=False,
                owner_all_sources=False,
                allowed_tools=frozenset(allowed_names),
                source_ids=effective_source_ids,
                destinations=frozenset({identity.destination_id}),
                capabilities=capabilities,
            )
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # Owner/query uncertainty cannot widen a detached inbound principal.
            return None

    async def revalidate_inbound(
        self,
        identity: InboundPrincipal,
        expected: ToolExecutionPrincipal,
    ) -> bool:
        """Require fresh client, source and exact native-tool authority to equal the captured principal."""
        if not isinstance(expected, ToolExecutionPrincipal):
            return False
        current = await self.authorize_inbound(identity)
        return current is not None and current == expected

    async def revalidate_inbound_output(
        self,
        identity: InboundPrincipal,
        expected: ToolExecutionPrincipal,
        *,
        binding: InboundBinding | None,
        sink: object | None,
    ) -> bool:
        """Fence successful native output by its binding/evidence, then recheck caller authority.

        A missing binding and sink is accepted only for the adapter's bounded control,
        catalog, notification or sanitized-failure response. Successful native data needs
        both the exact captured binding and its fresh server-created output-fence sink.
        """
        if not await self.revalidate_inbound(identity, expected):
            return False
        if binding is None:
            if sink is not None:
                return False
        else:
            if binding not in identity.bindings or sink is None:
                return False
            definition = self.registry.get_tool(binding.name, binding.version)
            capabilities = frozenset(identity.capabilities) & frozenset({"source.read"})
            if (
                definition is None
                or binding.name not in expected.allowed_tools
                or definition.schema_fingerprint != binding.schema_fingerprint
                or definition.risk != ToolRisk.READ_ONLY
                or definition.confirmation_required
                or not set(definition.permissions).issubset(capabilities)
            ):
                return False
            # Import the public facade lazily because it may re-export this runtime later.
            from modules.tools import public as tools_public

            try:
                if not await tools_public.revalidate_native_output_fences(
                    self.session_factory,
                    sink,
                    expected,
                    destination_kind=ToolDestination.REMOTE.value,
                    multi_workspace_enabled=self.settings.multi_workspace_enabled,
                ):
                    return False
            except (HTTPException, SQLAlchemyError, ValueError, OSError, TimeoutError):
                # Admission/database failures deny; TypeError/AttributeError are bugs and propagate.
                return False

            # A successful result is rechecked against the exact current registry binding.
            current_definition = self.registry.get_tool(binding.name, binding.version)
            if (
                current_definition is None
                or current_definition.schema_fingerprint != binding.schema_fingerprint
                or current_definition.risk != ToolRisk.READ_ONLY
                or current_definition.confirmation_required
                or not set(current_definition.permissions).issubset(capabilities)
            ):
                return False
        return await self.revalidate_inbound(identity, expected)

    async def load_connection(
        self,
        scope: Scope,
        connection_id: UUID,
    ) -> tuple[ConnectionRead, str | None]:
        """Load detached owner metadata and a credential only for bearer-authenticated HTTP.

        The fresh session is closed before the returned snapshot can be used for network work;
        repository policy rejects any stdio bearer or retained ciphertext, and credentials are
        never logged or exposed to management/native execution principals.
        """
        async with self.session_factory() as session:
            await self._admit(session, scope)
            return await mcp_repository.load_transport_connection(
                session,
                connection_id,
                scope=scope,
                encryption_key=self.settings.connector_credential_encryption_key.get_secret_value(),
            )

    async def record_draft_check(
        self,
        scope: Scope,
        connection_id: UUID,
        expected_revision: int,
        result_code: str,
        captured_profile_hash: str | None = None,
    ) -> ConnectionRead:
        """Commit the SDK outcome against its captured revision and stdio profile identity.

        A connected stdio result records only the exact hash negotiated by the client; nonconnected
        results clear that binding. Repository checks close the session before control returns.
        """
        async with self.session_factory() as session:
            fence = await self._admit(session, scope, lock=True)
            result = await mcp_repository.record_draft_check(
                session,
                connection_id,
                expected_revision,
                scope=scope,
                result_code=result_code,
                captured_profile_hash=captured_profile_hash,
            )
            await commit_with_replay(
                session, [], scope=scope, multi_workspace_enabled=self.settings.multi_workspace_enabled,
                access_fence=fence,
            )
            return result

    async def persist_discovery(
        self,
        scope: Scope,
        connection_id: UUID,
        payload: DiscoveryPersist,
    ) -> DiscoveryRead:
        """Commit a complete validated discovery only against its captured revision and profile hash."""
        async with self.session_factory() as session:
            fence = await self._admit(session, scope, lock=True)
            result = await mcp_repository.persist_discovery(session, connection_id, payload, scope=scope)
            await commit_with_replay(
                session, [], scope=scope, multi_workspace_enabled=self.settings.multi_workspace_enabled,
                access_fence=fence,
            )
            return result

    async def resolve_stdio_profile(
        self,
        scope: Scope,
        connection: ConnectionRead,
        reviewed_profile_hash: str,
        operation_kind: Literal["ordinary", "discovery"],
    ) -> StdioDeploymentProfile:
        """Resolve current owner-scoped connection identity before returning detached launch data.

        The callback holds no session over stdio setup: it reloads the exact owner row, checks
        connection/revision/profile ID/hash and credential state, verifies the retained catalog
        identity and admission lease, then returns a copied deployment-owned profile. Discovery
        additionally requires a successful draft check for the same profile hash.
        """
        profile_id = connection.deployment_profile_id
        if (connection.transport != McpTransport.STDIO or profile_id is None
                or connection.deployment_profile_hash != reviewed_profile_hash
                or not await self.admission.lease_current()):
            raise mcp_repository.McpConflict("MCP stdio profile authority is no longer current")
        if self.profile_catalog.get_identity(profile_id) != reviewed_profile_hash:
            raise mcp_repository.McpConflict("MCP stdio deployment profile changed")
        async with self.session_factory() as session:
            await self._admit(session, scope)
            row = await mcp_repository.get_connection(session, connection.id, scope=scope)
            current = mcp_repository.to_connection_read(row)
            check_hash = row.draft_check_profile_hash
            credential_present = row.encrypted_credential is not None or row.auth_method != "none"
        if (current.id != connection.id or current.revision != connection.revision
                or current.deployment_profile_id != profile_id
                or current.deployment_profile_hash != reviewed_profile_hash
                or connection.id != current.id or connection.revision != current.revision
                or credential_present):
            raise mcp_repository.McpConflict("MCP stdio connection changed before profile resolution")
        if operation_kind == "discovery" and check_hash != reviewed_profile_hash:
            raise mcp_repository.McpConflict("MCP stdio discovery requires a successful current profile check")
        if not await self.admission.lease_current():
            raise mcp_repository.McpConflict("MCP stdio admission lease expired during profile resolution")
        return self.profile_catalog.resolve(
            profile_id,
            reviewed_profile_hash=reviewed_profile_hash,
            operation_kind=operation_kind,
        )

    async def connection_is_current(
        self, scope: Scope, connection_id: UUID, revision: int,
        expected_profile_hash: str | None = None,
    ) -> bool:
        """Compare current owner revision, profile hash and admission lease after bounded reads.

        The initial lease check avoids unnecessary queries; the final check prevents SQL latency
        from making a lost/replaced Redis token appear current to the transport callback.
        """
        try:
            if not await self.admission.lease_current():
                return False
            async with self.session_factory() as session:
                await self._admit(session, scope)
                connections = await mcp_repository.list_runtime_connections(session, scope=scope)
            is_current = any(
                item.id == connection_id and item.revision == revision
                and item.deployment_profile_hash == expected_profile_hash
                for item in connections
            )
            return is_current and await self.admission.lease_current()
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    async def resolve_fence(
        self,
        scope: Scope,
        connection_id: UUID,
        grant_id: UUID,
        destination_id: str,
    ) -> ExecutionFence:
        """Resolve a freshly locked, exact grant fence and close the short transaction before SDK I/O."""
        if destination_id != "local" and destination_id != "omniroute":
            if not destination_id.startswith("mcp-client:"):
                raise PermissionError("Unknown MCP destination identity")
            try:
                if str(UUID(destination_id.removeprefix("mcp-client:"))) != destination_id.removeprefix("mcp-client:"):
                    raise ValueError("Noncanonical client identity")
            except ValueError as exc:
                raise PermissionError("Unknown MCP destination identity") from exc
        async with self.session_factory() as session:
            await self._admit(session, scope)
            return await mcp_repository.resolve_capability_fence(
                session, connection_id, grant_id, destination_id, scope=scope,
            )

    async def revalidate_fence(self, scope: Scope, fence: ExecutionFence) -> bool:
        """Require current reviewed remote transport, enabled registry, source and admission authority.

        HTTP and stdio both cross a remote-provider boundary, so LOCAL recipient identity never
        permits a local-only source. A deployment-owned launch profile authenticates only the
        reviewed command identity; it does not establish lack of network egress. The native
        registry projection is reconstructed from current durable review data and checked at
        the final synchronous success boundary after awaited source and lease validation.
        """
        if not await self.admission.lease_current():
            return False
        destination_id = fence.destination_id
        if destination_id != "local" and destination_id != "omniroute":
            if not destination_id.startswith("mcp-client:"):
                return False
            try:
                if str(UUID(destination_id.removeprefix("mcp-client:"))) != destination_id.removeprefix("mcp-client:"):
                    return False
            except ValueError:
                return False
        if not fence.source_ids or len(fence.source_ids) > 100:
            return False
        try:
            async with self.session_factory() as session:
                await self._admit(session, scope)
                if not await mcp_repository.revalidate_capability_fence(session, fence, scope=scope):
                    return False
                selection = await mcp_repository.get_current_selection(
                    session, fence.connection_id, scope=scope,
                )
            if selection is None:
                return False
            connection, discovery, grants = selection
            grant = next((item for item in grants if item.id == fence.grant_id), None)
            capability = next((
                item for item in discovery.capabilities
                if grant is not None and item.id == grant.capability_id
            ), None)
            if (
                not connection.enabled
                or connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}
                or connection.revision != fence.connection_revision
                or discovery.id != fence.discovery_id
                or discovery.connection_revision != connection.revision
                or grant is None
                or capability is None
                or capability.kind not in {"tool", "resource"}
                or grant.connection_id != connection.id
                or grant.reviewed_connection_revision != connection.revision
                or grant.descriptor_hash != capability.descriptor_hash
                or grant.descriptor_hash != fence.descriptor_hash
                or grant.grant_revision != fence.grant_revision
                or grant.risk != McpRisk.READ_ONLY
                or grant.purpose != "chat"
                or destination_id not in grant.destinations
                or grant.source_ids != fence.source_ids
                or capability.remote_key != fence.remote_capability_key
                or connection.deployment_profile_hash != fence.deployment_profile_hash
            ):
                return False
            if connection.transport == McpTransport.STDIO:
                if connection.deployment_profile_id is None or self.profile_catalog.get_identity(
                    connection.deployment_profile_id,
                ) != fence.deployment_profile_hash:
                    return False
            elif fence.deployment_profile_hash is not None:
                return False
            assert grant is not None and capability is not None
            expected_definition = build_mcp_tool_definition(
                connection, discovery.id, capability, grant,
            )
            selected_sources = frozenset(fence.source_ids)
            async with self.session_factory() as session:
                page = await sources_public.list_tool_sources(
                    session,
                    scope=scope,
                    multi_workspace_enabled=self.settings.multi_workspace_enabled,
                    limit=100,
                    cursor=None,
                    source_ids=selected_sources,
                    owner_all=False,
                    destination=ToolDestination.REMOTE,
                )
            if page.next_cursor is not None or frozenset(item.id for item in page.items) != selected_sources:
                return False
            if not await self.admission.lease_current():
                return False
            # No await follows this enabled public catalog check and successful return.
            current_definition = next((
                item for item in self.registry.list_tools(
                    allowed_tools=frozenset({expected_definition.name}),
                )
                if item.name == expected_definition.name
            ), None)
            return (
                current_definition is not None
                and current_definition.version == expected_definition.version
                and current_definition.schema_fingerprint == expected_definition.schema_fingerprint
            )
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    async def refresh_connection(self, scope: Scope, connection_id: UUID) -> tuple[str, ...]:
        """Replace one native registration after enforcing the owner's bounded catalog.

        Registration is only a catalog snapshot; every dispatch still resolves and revalidates
        the durable owner fence before sending a request or returning a result. The shared
        repository projection rejects more than 100 connections before any registration change.
        """
        async with self.session_factory() as session:
            await self._admit(session, scope)
            await mcp_repository.list_runtime_connections(session, scope=scope)
        async with self.session_factory() as session:
            await self._admit(session, scope)
            selection = await mcp_repository.get_current_selection(session, connection_id, scope=scope)
        if selection is None:
            self.dispatch.unregister_connection(connection_id)
            return ()
        connection, discovery, grants = selection
        if (not connection.enabled
                or connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}):
            self.dispatch.unregister_connection(connection_id)
            return ()
        if connection.transport == McpTransport.STDIO:
            try:
                if (connection.deployment_profile_id is None
                        or connection.deployment_profile_hash is None
                        or self.profile_catalog.get_identity(connection.deployment_profile_id)
                        != connection.deployment_profile_hash):
                    self.dispatch.unregister_connection(connection_id)
                    return ()
            except mcp_repository.McpUnavailable:
                self.dispatch.unregister_connection(connection_id)
                return ()
        return self.dispatch.register_selected_capabilities(scope.workspace_id, connection, discovery, grants)

    async def hydrate_connections(self) -> None:
        """Hydrate enabled owner connections per workspace without contacting providers.

        Each workspace owner is resolved from durable state; the repository rejects a catalog over
        100 connections before registration starts, and no disabled or stale descriptor is published.
        """
        flag = self.settings.multi_workspace_enabled
        async with self.session_factory() as session:
            # ponytail: first 100 workspace owners at startup; cursor-page when installs exceed it
            pairs = (await session.execute(
                select(McpConnection.workspace_id, McpConnection.owner_id)
                .where(McpConnection.enabled.is_(True)).distinct()
                .order_by(McpConnection.workspace_id, McpConnection.owner_id).limit(100)
            )).all()
        for workspace_id, row_owner_id in pairs:
            try:
                async with self.session_factory() as session:
                    owner = await workspaces.resolve_workspace_owner_context(
                        session, workspace_id, multi_workspace_enabled=flag,
                    )
                    if owner is None or owner.user_id != row_owner_id:
                        continue
                    scope = InternalJobScope(workspace_id, owner.user_id, owner.membership_revision)
                    await self._admit(session, scope)
                    connections = await mcp_repository.list_runtime_connections(session, scope=scope)
                for connection in connections:
                    await self.refresh_connection(scope, connection.id)
            except (HTTPException, mcp_repository.McpConflict, mcp_repository.McpNotFound,
                    mcp_repository.McpUnavailable, SQLAlchemyError) as exc:
                logger.warning("MCP hydrate skipped a workspace (%s)", type(exc).__name__)
