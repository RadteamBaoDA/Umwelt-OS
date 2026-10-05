"""Maintained MCP SDK client operations with owner callbacks and bounded transport composition."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
import hashlib
import json
import time
from typing import Any, Literal
from uuid import UUID

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from modules.tools import mcp_repository
from modules.tools.mcp_schemas import (
    CapabilityDescriptor,
    ConnectionRead,
    DiscoveryPersist,
    DiscoveryRead,
    ExecutionFence,
    McpTransport,
)
from modules.tools.mcp_transport import (
    McpOperationNetworkBudget,
    McpTransportError,
    create_mcp_http_client,
)
from modules.tools.mcp_stdio import StdioDeploymentProfile, stdio_client_transport


class McpSdkClient:
    """Run bounded MCP draft checks, discovery and selected reads through the maintained SDK."""

    def __init__(
        self,
        *,
        load_connection: Callable[[int, UUID], Awaitable[tuple[ConnectionRead, str | None]]],
        record_draft_check: Callable[[int, UUID, int, str, str | None], Awaitable[ConnectionRead]],
        persist_discovery: Callable[[int, UUID, DiscoveryPersist], Awaitable[DiscoveryRead]],
        connection_is_current: Callable[[int, UUID, int, str | None], Awaitable[bool]],
        operation_slot: Callable[[UUID, float], AbstractAsyncContextManager[None]],
        resolve_stdio_profile: Callable[[int, ConnectionRead, str, Literal["ordinary", "discovery"]], Awaitable[StdioDeploymentProfile]] | None = None,
        approved_destination_cidrs: Mapping[tuple[str, str, int], tuple[str, ...]] | None = None,
    ) -> None:
        """Install owner-scoped persistence, fresh identity resolvers and shared admission callbacks.

        Connection loaders must use a short fresh transaction and return detached data plus the
        decrypted bearer only for bearer-authenticated HTTP rows. The optional stdio resolver
        accepts the owner, detached row and captured reviewed hash, returns copied manifest data,
        and closes its fresh database session before launch. Persistence callbacks are owner scoped;
        none may leave a database lock or session open across transport I/O.
        """
        self._load_connection = load_connection
        self._record_draft_check = record_draft_check
        self._persist_discovery = persist_discovery
        self._connection_is_current = connection_is_current
        self._resolve_stdio_profile = resolve_stdio_profile
        self._operation_slot = operation_slot
        self._approved_destination_cidrs = approved_destination_cidrs or {}

    async def _management_request_is_current(
        self,
        owner_id: int,
        connection_id: UUID,
        expected_revision: int | None,
        management_revalidator: Callable[[], Awaitable[bool]],
        expected_profile_hash: str | None = None,
    ) -> bool:
        """Require request owner authority plus the captured revision/profile identity and lease.

        The callback is supplied per operation and is never stored on this shared client. Its
        failures deny the operation; cancellation still propagates. Connection/revision and lease
        revision/profile/lease validation runs first so the fresh Auth query is the final awaited check before a send or
        persistence call, without claiming atomicity with later socket or database effects.
        """
        if expected_revision is not None and not await self._connection_is_current(
            owner_id, connection_id, expected_revision, expected_profile_hash,
        ):
            return False
        try:
            return await management_revalidator() is True
        except Exception:
            return False

    @asynccontextmanager
    async def _open_http_client(
        self,
        connection: ConnectionRead,
        bearer_token: str | None,
        budget: McpOperationNetworkBudget,
    ) -> AsyncIterator[Client]:
        """Enter caller-owned HTTP, SDK transport and SDK client contexts in safe reverse-close order."""
        if connection.transport != McpTransport.STREAMABLE_HTTP or not connection.endpoint:
            raise mcp_repository.McpUnavailable("MCP deployment profile transport is not assigned")
        if connection.auth_method == "bearer" and not bearer_token:
            raise mcp_repository.McpUnavailable("MCP bearer credential is unavailable")
        if connection.auth_method == "none" and bearer_token is not None:
            raise McpTransportError("Unauthenticated MCP connection unexpectedly supplied a credential")
        http_client = create_mcp_http_client(
            connection.endpoint,
            bearer_token,
            budget,
            timeout_seconds=min(connection.timeout_seconds, budget.remaining_seconds()),
            approved_destination_cidrs=self._approved_destination_cidrs,
        )
        transport_context = streamable_http_client(
            connection.endpoint,
            http_client=http_client,
            terminate_on_close=True,
            max_sse_event_size=min(budget.max_response_bytes, 1_000_000),
        )
        async with http_client:
            async with Client(
                transport_context,
                mode="auto",
                cache=None,
                read_timeout_seconds=budget.remaining_seconds(),
                sampling_callback=None,
                elicitation_callback=None,
                list_roots_callback=None,
            ) as client:
                yield client

    @asynccontextmanager
    async def _open_stdio_client(
        self,
        profile: StdioDeploymentProfile,
        before_request: Callable[[], Awaitable[bool]],
        read_timeout_seconds: float,
    ) -> AsyncIterator[Client]:
        """Give the maintained SDK the bounded two-stream stdio transport and reviewed callbacks.

        The SDK owns entering/exiting the supplied transport context, including handshake failure;
        the frozen transport verifies the deployment hash, process ancestry, per-frame fence and
        cleanup before this context can report success. No SDK stdio command launcher is used.
        """
        async with Client(
            stdio_client_transport(profile, before_request),
            mode="auto",
            cache=None,
            sampling_callback=None,
            elicitation_callback=None,
            list_roots_callback=None,
            read_timeout_seconds=read_timeout_seconds,
        ) as client:
            yield client

    @asynccontextmanager
    async def _open_client(
        self,
        owner_id: int,
        connection: ConnectionRead,
        bearer_token: str | None,
        budget: McpOperationNetworkBudget,
        operation_kind: Literal["ordinary", "discovery"],
    ) -> AsyncIterator[Client]:
        """Select HTTP or exact-hash stdio transport without changing shared admission or deadline.

        HTTP retains its origin/CIDR opener. Stdio accepts no bearer state, resolves only the
        captured connection hash through the short owner-scoped callback, and lets the trusted
        transport enforce its frame, process and cleanup bounds. Unknown transports fail closed.
        """
        if connection.transport == McpTransport.STREAMABLE_HTTP:
            if connection.deployment_profile_hash is not None:
                raise McpTransportError("HTTP connection unexpectedly carries a stdio profile identity")
            async with self._open_http_client(connection, bearer_token, budget) as client:
                yield client
            return
        if connection.transport != McpTransport.STDIO:
            raise mcp_repository.McpUnavailable("MCP transport is unavailable")
        if connection.auth_method != "none" or bearer_token is not None:
            raise mcp_repository.McpUnavailable("MCP stdio cannot use bearer credentials")
        profile_hash = connection.deployment_profile_hash
        if profile_hash is None or self._resolve_stdio_profile is None or budget.before_request is None:
            raise mcp_repository.McpUnavailable("MCP stdio profile resolution is unavailable")
        profile = await self._resolve_stdio_profile(owner_id, connection, profile_hash, operation_kind)
        if (profile.profile_id != connection.deployment_profile_id
                or profile.profile_hash != profile_hash
                or profile.reviewed_profile_hash != profile_hash
                or profile.operation_kind != operation_kind):
            raise mcp_repository.McpUnavailable("MCP stdio profile resolution did not match durable review")
        async with self._open_stdio_client(
            profile, budget.before_request,
            min(float(connection.timeout_seconds), budget.remaining_seconds()),
        ) as client:
            yield client

    async def check_connection(
        self,
        owner_id: int,
        connection_id: UUID,
        *,
        management_revalidator: Callable[[], Awaitable[bool]],
    ) -> ConnectionRead:
        """Negotiate HTTP or deployment-owned stdio and persist only a real, current transport outcome.

        The detached Auth callback and captured connection/profile identity are checked before and
        after admission, for each transport frame, and before persistence. Provider failures are
        recorded only while those identities still match; denial is never persisted as an outcome.
        Connected follows SDK negotiation and verified transport teardown. The shared operation
        deadline begins before queueing and includes profile resolution, transport and persistence.
        """
        deadline = time.monotonic() + 60.0
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            if not await self._management_request_is_current(
                owner_id, connection_id, None, management_revalidator,
            ):
                raise mcp_repository.McpConflict("MCP management authority is no longer current")
            async with self._operation_slot(connection_id, deadline):
                if not await self._management_request_is_current(
                    owner_id, connection_id, None, management_revalidator,
                ):
                    raise mcp_repository.McpConflict("MCP management authority changed while queued")
                connection, bearer_token = await self._load_connection(owner_id, connection_id)
                if connection.id != connection_id:
                    raise McpTransportError("MCP connection loader returned a mismatched record")
                if connection.transport == McpTransport.STDIO and (
                    connection.auth_method != "none" or bearer_token is not None
                ):
                    raise mcp_repository.McpUnavailable("MCP stdio cannot use bearer credentials")
                if connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}:
                    raise mcp_repository.McpUnavailable("MCP transport is unavailable")
                if not await self._management_request_is_current(
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                ):
                    raise mcp_repository.McpConflict("MCP connection or management authority changed before draft check")
                before_request = partial(
                    self._management_request_is_current,
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                )
                budget = McpOperationNetworkBudget(
                    deadline=deadline,
                    max_requests=32,
                    max_request_bytes=256_000,
                    max_response_bytes=256_000,
                    before_request=before_request,
                )
                try:
                    async with self._open_client(
                        owner_id, connection, bearer_token, budget, "ordinary",
                    ) as client:
                        # __aenter__ negotiates the protocol and assigns the session only on success.
                        if not client.protocol_version:
                            raise McpTransportError("MCP SDK negotiation did not select a protocol version")
                    outcome = "connected"
                except mcp_repository.McpUnavailable:
                    raise
                except mcp_repository.McpConflict:
                    raise
                except TimeoutError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from None
                    outcome = "timeout"
                except PermissionError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from None
                    outcome = "auth_error"
                except McpTransportError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from None
                    outcome = "unavailable"
                except Exception:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError from None
                    outcome = "protocol_error"
                if not await self._management_request_is_current(
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                ):
                    raise mcp_repository.McpConflict("MCP connection or management authority changed before draft persistence")
                return await self._record_draft_check(
                    owner_id, connection_id, connection.revision, outcome,
                    connection.deployment_profile_hash,
                )

    async def discover(
        self,
        owner_id: int,
        connection_id: UUID,
        *,
        management_revalidator: Callable[[], Awaitable[bool]],
    ) -> DiscoveryRead:
        """Discover a bounded descriptor snapshot over HTTP or reviewed stdio and persist its identity.

        The required per-request Auth callback is checked before and after admission queueing, at
        every transport frame, and immediately before the repository persistence callback. A revoked or
        expired session aborts without saving descriptors; the shared SDK client retains no owner
        token, principal, or callback between calls.
        """
        deadline = time.monotonic() + 60.0
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            if not await self._management_request_is_current(
                owner_id, connection_id, None, management_revalidator,
            ):
                raise mcp_repository.McpConflict("MCP management authority is no longer current")
            async with self._operation_slot(connection_id, deadline):
                if not await self._management_request_is_current(
                    owner_id, connection_id, None, management_revalidator,
                ):
                    raise mcp_repository.McpConflict("MCP management authority changed while queued")
                connection, bearer_token = await self._load_connection(owner_id, connection_id)
                if connection.id != connection_id:
                    raise McpTransportError("MCP connection loader returned a mismatched record")
                if connection.transport == McpTransport.STDIO and (
                    connection.auth_method != "none" or bearer_token is not None
                ):
                    raise mcp_repository.McpUnavailable("MCP stdio cannot use bearer credentials")
                if connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}:
                    raise mcp_repository.McpUnavailable("MCP transport is unavailable")
                if not await self._management_request_is_current(
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                ):
                    raise mcp_repository.McpConflict("MCP connection or management authority changed before discovery")
                before_request = partial(
                    self._management_request_is_current,
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                )
                budget = McpOperationNetworkBudget(
                    deadline=deadline,
                    max_requests=64,
                    max_request_bytes=1_000_000,
                    max_response_bytes=1_000_000,
                    before_request=before_request,
                )
                async with self._open_client(
                    owner_id, connection, bearer_token, budget, "discovery",
                ) as client:
                    page_count = 0
                    capability_count = 0
                    capabilities: list[CapabilityDescriptor] = []
                    server_capabilities = client.server_capabilities
                    for kind, capability_name in (
                        ("tool", "tools"),
                        ("resource", "resources"),
                        ("resource_template", "resources"),
                    ):
                        if getattr(server_capabilities, capability_name, None) is None:
                            continue
                        items, page_count, capability_count = await self._list_capability_pages(
                            client, kind, page_count, capability_count,
                        )
                        capabilities.extend(items)
                    ordered = tuple(sorted(capabilities, key=lambda item: (item.kind, item.remote_key)))
                    identity = [
                        {"kind": item.kind, "key": item.remote_key, "hash": item.descriptor_hash}
                        for item in ordered
                    ]
                    schema_set_hash = hashlib.sha256(
                        json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                    ).hexdigest()
                    server_info = client.server_info.model_dump(
                        by_alias=True, mode="json", exclude_none=True,
                    ) if client.server_info is not None else {}
                    payload = DiscoveryPersist(
                        connection_revision=connection.revision,
                        deployment_profile_hash=connection.deployment_profile_hash,
                        protocol=client.protocol_version,
                        server_info=server_info,
                        schema_set_hash=schema_set_hash,
                        capabilities=ordered,
                    )
                if not await self._management_request_is_current(
                    owner_id, connection_id, connection.revision, management_revalidator,
                    connection.deployment_profile_hash,
                ):
                    raise mcp_repository.McpConflict("MCP connection or management authority changed before discovery persistence")
                return await self._persist_discovery(owner_id, connection_id, payload)

    async def _list_capability_pages(
        self,
        client: Client,
        kind: str,
        page_count: int,
        capability_count: int,
    ) -> tuple[list[CapabilityDescriptor], int, int]:
        """Read one capability kind with the discovery-wide ten-page and 200-item limits."""
        if kind == "tool":
            list_page = client.list_tools
            field_name = "tools"
        elif kind == "resource":
            list_page = client.list_resources
            field_name = "resources"
        elif kind == "resource_template":
            list_page = client.list_resource_templates
            field_name = "resource_templates"
        else:
            raise ValueError("Unsupported MCP discovery capability kind")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        descriptors: list[CapabilityDescriptor] = []
        while True:
            if page_count >= 10:
                raise McpTransportError("MCP discovery exceeds the ten-page aggregate limit")
            if cursor is not None:
                if not cursor or cursor in seen_cursors:
                    raise McpTransportError("MCP discovery returned a repeated or empty page cursor")
                seen_cursors.add(cursor)
            page = await list_page(cursor=cursor, cache_mode="bypass")
            page_count += 1
            items = getattr(page, field_name, None)
            if not isinstance(items, list):
                raise McpTransportError("MCP discovery page has an invalid capability collection")
            for item in items:
                if capability_count >= 200:
                    raise McpTransportError("MCP discovery exceeds the 200-capability aggregate limit")
                descriptors.append(self._normalize_capability(kind, item))
                capability_count += 1
            next_cursor = getattr(page, "next_cursor", None)
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise McpTransportError("MCP discovery returned a repeated or invalid page cursor")
            cursor = next_cursor
        return descriptors, page_count, capability_count

    def _normalize_capability(self, kind: str, item: Any) -> CapabilityDescriptor:
        """Canonicalize one SDK capability and hash its exact public descriptor for owner review."""
        if not hasattr(item, "model_dump"):
            raise McpTransportError("MCP SDK returned a non-model capability")
        descriptor = item.model_dump(by_alias=True, mode="json", exclude_none=True)
        if not isinstance(descriptor, dict):
            raise McpTransportError("MCP SDK returned an invalid capability descriptor")
        key_field = {"tool": "name", "resource": "uri", "resource_template": "uriTemplate"}[kind]
        remote_key = descriptor.get(key_field)
        if not isinstance(remote_key, str) or not remote_key:
            raise McpTransportError("MCP capability has no stable server identity")
        if kind == "tool" and not isinstance(descriptor.get("inputSchema"), dict):
            raise McpTransportError("MCP tool capability has no valid input schema")
        self._validate_descriptor(descriptor)
        canonical = json.dumps(
            {"kind": kind, "remote_key": remote_key, "descriptor": descriptor},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return CapabilityDescriptor(
            kind=kind,
            remote_key=remote_key,
            descriptor=descriptor,
            descriptor_hash=hashlib.sha256(canonical).hexdigest(),
        )

    def _validate_descriptor(self, descriptor: dict[str, Any]) -> None:
        """Reject non-JSON or oversized server metadata before hashing or persisting it."""
        try:
            encoded = json.dumps(
                descriptor, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise McpTransportError("MCP capability descriptor is not bounded JSON data") from exc
        if len(encoded) > 65_536:
            raise McpTransportError("MCP capability descriptor exceeds 64 KiB")

    async def call_tool(
        self,
        owner_id: int,
        fence: ExecutionFence,
        arguments: dict[str, Any],
        before_request: Callable[[], Awaitable[bool]],
    ) -> dict[str, Any]:
        """Refresh the exact reviewed descriptor, call once, verify cleanup, and revalidate before returning.

        One admission slot and monotonic deadline cover fresh profile resolution, descriptor refresh,
        the SDK request, bounded output validation, transport teardown and final authority check.
        The deployment transport may spend its reserved cleanup time after cancellation; uncertain
        teardown raises and the result is never returned as success.
        """
        encoded_arguments = json.dumps(
            arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        argument_limit = min(fence.limits.get("argument_bytes", 64_000), 64_000)
        if len(encoded_arguments) > argument_limit:
            raise McpTransportError("MCP tool arguments exceed the reviewed size limit")
        if not before_request:
            raise McpTransportError("MCP selected-tool fence callback is unavailable")
        deadline = time.monotonic() + min(60.0, float(fence.timeout_seconds))
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            async with self._operation_slot(fence.connection_id, deadline):
                connection, bearer_token = await self._load_connection(owner_id, fence.connection_id)
                if (not connection.enabled or connection.revision != fence.connection_revision
                        or connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}
                        or connection.deployment_profile_hash != fence.deployment_profile_hash):
                    raise mcp_repository.McpConflict("MCP connection changed or is disabled")
                if connection.transport == McpTransport.STDIO and (
                    connection.auth_method != "none" or bearer_token is not None
                ):
                    raise mcp_repository.McpUnavailable("MCP stdio cannot use bearer credentials")
                response_limit = min(fence.limits.get("response_bytes", 256_000), 256_000)
                budget = McpOperationNetworkBudget(
                    deadline=deadline,
                    max_requests=32,
                    max_request_bytes=256_000,
                    max_response_bytes=response_limit,
                    before_request=before_request,
                )
                async with self._open_client(
                    owner_id, connection, bearer_token, budget, "ordinary",
                ) as client:
                    current, _pages, _count = await self._list_capability_pages(client, "tool", 0, 0)
                    matched = next((item for item in current if item.remote_key == fence.remote_capability_key), None)
                    if matched is None or matched.descriptor_hash != fence.descriptor_hash:
                        raise mcp_repository.McpConflict("MCP tool descriptor changed; renewed owner review is required")
                    # ClientSession is the SDK's public protocol API; it rejects InputRequiredResult
                    # instead of running the high-level Client convenience driver's callbacks/retries.
                    result = await client.session.call_tool(
                        fence.remote_capability_key,
                        arguments,
                        read_timeout_seconds=budget.remaining_seconds(),
                        allow_input_required=False,
                        allow_claimed=False,
                    )
                    payload = self._validate_result(result, response_limit)
                if not await before_request():
                    raise mcp_repository.McpConflict("MCP authorization changed before result return")
                return payload

    async def read_resource(
        self,
        owner_id: int,
        fence: ExecutionFence,
        capability_kind: str,
        before_request: Callable[[], Awaitable[bool]],
    ) -> dict[str, Any]:
        """Read the exact selected resource once and return only after bounded cleanup and revalidation.

        Resource templates remain unavailable without an owner-reviewed parameter binding. The
        shared slot/deadline covers profile resolution, descriptor refresh, the read, result bounds,
        transport teardown and the final current-authority check.
        """
        if capability_kind != "resource":
            raise mcp_repository.McpUnavailable("MCP resource-template reads require an owner-reviewed parameter binding")
        if not before_request:
            raise McpTransportError("MCP selected-resource fence callback is unavailable")
        deadline = time.monotonic() + min(60.0, float(fence.timeout_seconds))
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            async with self._operation_slot(fence.connection_id, deadline):
                connection, bearer_token = await self._load_connection(owner_id, fence.connection_id)
                if (not connection.enabled or connection.revision != fence.connection_revision
                        or connection.transport not in {McpTransport.STREAMABLE_HTTP, McpTransport.STDIO}
                        or connection.deployment_profile_hash != fence.deployment_profile_hash):
                    raise mcp_repository.McpConflict("MCP connection changed or is disabled")
                if connection.transport == McpTransport.STDIO and (
                    connection.auth_method != "none" or bearer_token is not None
                ):
                    raise mcp_repository.McpUnavailable("MCP stdio cannot use bearer credentials")
                response_limit = min(fence.limits.get("response_bytes", 256_000), 256_000)
                budget = McpOperationNetworkBudget(
                    deadline=deadline,
                    max_requests=32,
                    max_request_bytes=256_000,
                    max_response_bytes=response_limit,
                    before_request=before_request,
                )
                async with self._open_client(
                    owner_id, connection, bearer_token, budget, "ordinary",
                ) as client:
                    current_resources, _pages, _count = await self._list_capability_pages(
                        client, "resource", 0, 0,
                    )
                    matched = next((
                        item for item in current_resources
                        if item.remote_key == fence.remote_capability_key
                    ), None)
                    if matched is None or matched.descriptor_hash != fence.descriptor_hash:
                        raise mcp_repository.McpConflict(
                            "MCP resource descriptor changed; renewed owner review is required"
                        )
                    result = await client.session.read_resource(
                        fence.remote_capability_key,
                        allow_input_required=False,
                    )
                    payload = self._validate_result(result, response_limit)
                if not await before_request():
                    raise mcp_repository.McpConflict("MCP authorization changed before result return")
                return payload

    def _validate_result(self, result: Any, max_bytes: int) -> dict[str, Any]:
        """Convert only an SDK-validated non-error result to bounded JSON for native registry validation."""
        if getattr(result, "is_error", False):
            raise McpTransportError("MCP server returned a tool error result")
        if not hasattr(result, "model_dump"):
            raise McpTransportError("MCP SDK returned an unsupported result shape")
        payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise McpTransportError("MCP result is not bounded JSON data") from exc
        if not isinstance(payload, dict) or len(encoded) > max_bytes:
            raise McpTransportError("MCP result exceeds the authorized output limit")
        return payload
