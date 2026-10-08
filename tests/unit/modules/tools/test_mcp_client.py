"""Unit tests for modules.tools.mcp_client.McpSdkClient.

Covers client initialization, connection verification, error translation,
timeout handling, capability discovery pagination, descriptor validation,
and fenced tool / resource execution.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from core.workspaces.schemas import WorkspaceContext
from modules.tools import mcp_repository
from modules.tools.mcp_client import McpSdkClient
from modules.tools.mcp_schemas import (
    CapabilityDescriptor,
    ConnectionRead,
    ExecutionFence,
    McpTransport,
)
from modules.tools.mcp_transport import McpOperationNetworkBudget, McpTransportError

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)


class DummyModel(BaseModel):
    """Simple Pydantic model for mocking MCP capability and result objects."""

    name: str = "test-tool"
    description: str = "A test tool"
    inputSchema: dict[str, Any] = {"type": "object"}


class DummyResult(BaseModel):
    """Mock result model returned by MCP call_tool or read_resource."""

    content: list[dict[str, Any]] = [{"type": "text", "text": "hello"}]
    is_error: bool = False


from datetime import UTC, datetime


@pytest.fixture
def dummy_connection() -> ConnectionRead:
    """Return a mock HTTP connection descriptor for testing."""
    return ConnectionRead(
        id=uuid4(),
        name="Test Server",
        transport=McpTransport.STREAMABLE_HTTP,
        endpoint="https://mcp.example.com/api/v1/mcp/",
        deployment_profile_id=None,
        deployment_profile_hash=None,
        revision=1,
        enabled=True,
        auth_method="bearer",
        credential_configured=True,
        timeout_seconds=30,
        health="healthy",
        error_code=None,
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_operation_slot():
    """Return an async context manager mock for the operation slot."""
    @asynccontextmanager
    async def _slot(connection_id: UUID, deadline: float):
        yield

    return _slot


class TestMcpSdkClientManagementRevalidation:
    """Unit tests for authority revalidation and current-management checks."""

    @pytest.mark.asyncio
    async def test_management_request_is_current_success(self, mock_operation_slot) -> None:
        """Verify _management_request_is_current succeeds when authority and connection match."""
        load_connection = AsyncMock()
        record_draft_check = AsyncMock()
        persist_discovery = AsyncMock()
        connection_is_current = AsyncMock(return_value=True)

        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=record_draft_check,
            persist_discovery=persist_discovery,
            connection_is_current=connection_is_current,
            operation_slot=mock_operation_slot,
        )

        revalidator = AsyncMock(return_value=True)
        conn_id = uuid4()
        is_current = await client._management_request_is_current(
            scope=SCOPE,
            connection_id=conn_id,
            expected_revision=2,
            management_revalidator=revalidator,
            expected_profile_hash="abc",
        )
        assert is_current is True
        connection_is_current.assert_awaited_once_with(SCOPE, conn_id, 2, "abc")
        revalidator.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_management_request_fails_when_connection_not_current(self, mock_operation_slot) -> None:
        """Verify _management_request_is_current returns False when connection_is_current is False."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(return_value=False),
            operation_slot=mock_operation_slot,
        )
        revalidator = AsyncMock(return_value=True)
        is_current = await client._management_request_is_current(
            scope=SCOPE,
            connection_id=uuid4(),
            expected_revision=1,
            management_revalidator=revalidator,
        )
        assert is_current is False
        revalidator.assert_not_called()

    @pytest.mark.asyncio
    async def test_management_request_fails_on_revalidator_exception(self, mock_operation_slot) -> None:
        """Verify _management_request_is_current returns False when revalidator raises."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(return_value=True),
            operation_slot=mock_operation_slot,
        )
        revalidator = AsyncMock(side_effect=RuntimeError("Auth error"))
        is_current = await client._management_request_is_current(
            scope=SCOPE,
            connection_id=uuid4(),
            expected_revision=None,
            management_revalidator=revalidator,
        )
        assert is_current is False


class TestMcpSdkClientOpenClient:
    """Unit tests for client opening, transport routing, and profile checks."""

    @pytest.mark.asyncio
    async def test_open_http_client_validates_transport(self, mock_operation_slot, dummy_connection) -> None:
        """Verify _open_http_client rejects mismatched transport types."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        invalid_conn = dummy_connection.model_copy(update={"transport": McpTransport.STDIO})
        budget = McpOperationNetworkBudget(
            deadline=100.0, max_requests=10, max_request_bytes=1000, max_response_bytes=1000,
            before_request=AsyncMock(return_value=True),
        )

        with pytest.raises(mcp_repository.McpUnavailable, match="transport is not assigned"):
            async with client._open_http_client(invalid_conn, None, budget):
                pass

    @pytest.mark.asyncio
    async def test_open_http_client_requires_bearer_token(self, mock_operation_slot, dummy_connection) -> None:
        """Verify _open_http_client raises McpUnavailable when bearer token is missing."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        budget = McpOperationNetworkBudget(
            deadline=100.0, max_requests=10, max_request_bytes=1000, max_response_bytes=1000,
            before_request=AsyncMock(return_value=True),
        )

        with pytest.raises(mcp_repository.McpUnavailable, match="bearer credential is unavailable"):
            async with client._open_http_client(dummy_connection, None, budget):
                pass

    @pytest.mark.asyncio
    async def test_open_http_client_rejects_unexpected_credential(self, mock_operation_slot, dummy_connection) -> None:
        """Verify _open_http_client raises McpTransportError when unauthenticated connection has token."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        unauth_conn = dummy_connection.model_copy(update={"auth_method": "none"})
        budget = McpOperationNetworkBudget(
            deadline=100.0, max_requests=10, max_request_bytes=1000, max_response_bytes=1000,
            before_request=AsyncMock(return_value=True),
        )

        with pytest.raises(McpTransportError, match="unexpectedly supplied a credential"):
            async with client._open_http_client(unauth_conn, "extra-token", budget):
                pass

    @pytest.mark.asyncio
    async def test_open_client_rejects_http_with_profile_hash(self, mock_operation_slot, dummy_connection) -> None:
        """Verify HTTP connection carrying deployment profile hash is rejected."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        invalid_conn = dummy_connection.model_copy(update={"deployment_profile_hash": "a" * 64})
        budget = McpOperationNetworkBudget(
            deadline=100.0, max_requests=10, max_request_bytes=1000, max_response_bytes=1000,
            before_request=AsyncMock(return_value=True),
        )

        with pytest.raises(McpTransportError, match="unexpectedly carries a stdio profile identity"):
            async with client._open_client(SCOPE, invalid_conn, "token", budget, "ordinary"):
                pass

    @pytest.mark.asyncio
    async def test_open_client_stdio_rejects_bearer(self, mock_operation_slot, dummy_connection) -> None:
        """Verify stdio transport rejects bearer credentials."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        stdio_conn = dummy_connection.model_copy(update={"transport": McpTransport.STDIO, "auth_method": "bearer"})
        budget = McpOperationNetworkBudget(
            deadline=100.0, max_requests=10, max_request_bytes=1000, max_response_bytes=1000,
            before_request=AsyncMock(return_value=True),
        )

        with pytest.raises(mcp_repository.McpUnavailable, match="cannot use bearer credentials"):
            async with client._open_client(SCOPE, stdio_conn, "bearer-token", budget, "ordinary"):
                pass


class TestMcpSdkClientCheckConnection:
    """Unit tests for check_connection outcome reporting and error translation."""

    @pytest.mark.asyncio
    async def test_check_connection_success(self, mock_operation_slot, dummy_connection) -> None:
        """Verify successful check_connection records 'connected' outcome."""
        load_connection = AsyncMock(return_value=(dummy_connection, "secret-token"))
        record_draft_check = AsyncMock(return_value=dummy_connection)
        connection_is_current = AsyncMock(return_value=True)

        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=record_draft_check,
            persist_discovery=AsyncMock(),
            connection_is_current=connection_is_current,
            operation_slot=mock_operation_slot,
        )

        mock_mcp_client = MagicMock()
        mock_mcp_client.protocol_version = "2024-11-05"

        @asynccontextmanager
        async def mock_open_client(*args, **kwargs):
            yield mock_mcp_client

        with patch.object(client, "_open_client", side_effect=mock_open_client):
            result = await client.check_connection(
                scope=SCOPE,
                connection_id=dummy_connection.id,
                management_revalidator=AsyncMock(return_value=True),
            )

        assert result == dummy_connection
        record_draft_check.assert_awaited_once_with(
            SCOPE, dummy_connection.id, dummy_connection.revision, "connected", None
        )

    @pytest.mark.asyncio
    async def test_check_connection_timeout_error_translation(self, mock_operation_slot, dummy_connection) -> None:
        """Verify TimeoutError translates to 'timeout' outcome."""
        load_connection = AsyncMock(return_value=(dummy_connection, "secret-token"))
        record_draft_check = AsyncMock(return_value=dummy_connection)
        connection_is_current = AsyncMock(return_value=True)

        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=record_draft_check,
            persist_discovery=AsyncMock(),
            connection_is_current=connection_is_current,
            operation_slot=mock_operation_slot,
        )

        @asynccontextmanager
        async def mock_open_client(*args, **kwargs):
            raise TimeoutError("Connection timed out")
            yield  # pragma: no cover

        with patch.object(client, "_open_client", side_effect=mock_open_client):
            await client.check_connection(
                scope=SCOPE,
                connection_id=dummy_connection.id,
                management_revalidator=AsyncMock(return_value=True),
            )

        record_draft_check.assert_awaited_once_with(
            SCOPE, dummy_connection.id, dummy_connection.revision, "timeout", None
        )

    @pytest.mark.asyncio
    async def test_check_connection_permission_error_translation(self, mock_operation_slot, dummy_connection) -> None:
        """Verify PermissionError translates to 'auth_error' outcome."""
        load_connection = AsyncMock(return_value=(dummy_connection, "secret-token"))
        record_draft_check = AsyncMock(return_value=dummy_connection)
        connection_is_current = AsyncMock(return_value=True)

        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=record_draft_check,
            persist_discovery=AsyncMock(),
            connection_is_current=connection_is_current,
            operation_slot=mock_operation_slot,
        )

        @asynccontextmanager
        async def mock_open_client(*args, **kwargs):
            raise PermissionError("Denied")
            yield  # pragma: no cover

        with patch.object(client, "_open_client", side_effect=mock_open_client):
            await client.check_connection(
                scope=SCOPE,
                connection_id=dummy_connection.id,
                management_revalidator=AsyncMock(return_value=True),
            )

        record_draft_check.assert_awaited_once_with(
            SCOPE, dummy_connection.id, dummy_connection.revision, "auth_error", None
        )

    @pytest.mark.asyncio
    async def test_check_connection_transport_error_translation(self, mock_operation_slot, dummy_connection) -> None:
        """Verify McpTransportError translates to 'unavailable' outcome."""
        load_connection = AsyncMock(return_value=(dummy_connection, "secret-token"))
        record_draft_check = AsyncMock(return_value=dummy_connection)
        connection_is_current = AsyncMock(return_value=True)

        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=record_draft_check,
            persist_discovery=AsyncMock(),
            connection_is_current=connection_is_current,
            operation_slot=mock_operation_slot,
        )

        @asynccontextmanager
        async def mock_open_client(*args, **kwargs):
            raise McpTransportError("Connection failed")
            yield  # pragma: no cover

        with patch.object(client, "_open_client", side_effect=mock_open_client):
            await client.check_connection(
                scope=SCOPE,
                connection_id=dummy_connection.id,
                management_revalidator=AsyncMock(return_value=True),
            )

        record_draft_check.assert_awaited_once_with(
            SCOPE, dummy_connection.id, dummy_connection.revision, "unavailable", None
        )

    @pytest.mark.asyncio
    async def test_check_connection_conflict_when_authority_revoked(self, mock_operation_slot, dummy_connection) -> None:
        """Verify McpConflict is raised when authority changes before check."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(return_value=False),
            operation_slot=mock_operation_slot,
        )

        with pytest.raises(mcp_repository.McpConflict, match="authority is no longer current"):
            await client.check_connection(
                scope=SCOPE,
                connection_id=dummy_connection.id,
                management_revalidator=AsyncMock(return_value=False),
            )


class TestMcpSdkClientDiscovery:
    """Unit tests for capability discovery, pagination limits, and schema normalization."""

    @pytest.mark.asyncio
    async def test_discovery_exceeds_page_limit_raises(self, mock_operation_slot) -> None:
        """Verify discovery raises McpTransportError when pages exceed limit of 10."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )

        mock_sdk_client = MagicMock()
        mock_sdk_client.list_tools = AsyncMock()

        with pytest.raises(McpTransportError, match="ten-page aggregate limit"):
            await client._list_capability_pages(mock_sdk_client, "tool", page_count=10, capability_count=0)

    @pytest.mark.asyncio
    async def test_discovery_repeated_cursor_raises(self, mock_operation_slot) -> None:
        """Verify discovery detects cycle in pagination cursors."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )

        mock_page = MagicMock()
        mock_page.tools = [DummyModel()]
        mock_page.next_cursor = "cursor-1"

        mock_sdk_client = MagicMock()
        mock_sdk_client.list_tools = AsyncMock(return_value=mock_page)

        with pytest.raises(McpTransportError, match="repeated or invalid page cursor"):
            await client._list_capability_pages(mock_sdk_client, "tool", page_count=0, capability_count=0)

    @pytest.mark.asyncio
    async def test_discovery_exceeds_capability_limit_raises(self, mock_operation_slot) -> None:
        """Verify discovery raises McpTransportError when total capabilities exceed 200."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )

        mock_page = MagicMock()
        mock_page.tools = [DummyModel() for _ in range(5)]
        mock_page.next_cursor = None

        mock_sdk_client = MagicMock()
        mock_sdk_client.list_tools = AsyncMock(return_value=mock_page)

        with pytest.raises(McpTransportError, match="200-capability aggregate limit"):
            await client._list_capability_pages(mock_sdk_client, "tool", page_count=0, capability_count=198)

    def test_normalize_capability_validates_and_hashes(self, mock_operation_slot) -> None:
        """Verify capability descriptor canonicalization and SHA-256 hashing."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        item = DummyModel(name="calculator")
        descriptor = client._normalize_capability("tool", item)

        assert descriptor.kind == "tool"
        assert descriptor.remote_key == "calculator"
        assert len(descriptor.descriptor_hash) == 64
        assert descriptor.descriptor["name"] == "calculator"

    def test_validate_descriptor_rejects_oversized_json(self, mock_operation_slot) -> None:
        """Verify descriptor exceeding 64 KiB raises McpTransportError."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        oversized = {"big_payload": "x" * 70_000}
        with pytest.raises(McpTransportError, match="exceeds 64 KiB"):
            client._validate_descriptor(oversized)


class TestMcpSdkClientToolAndResourceCalls:
    """Unit tests for call_tool and read_resource operations."""

    @pytest.mark.asyncio
    async def test_call_tool_rejects_oversized_arguments(self, mock_operation_slot) -> None:
        """Verify call_tool enforces argument size limits."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        fence = ExecutionFence(
            connection_id=uuid4(),
            connection_revision=1,
            deployment_profile_hash=None,
            grant_id=uuid4(),
            grant_revision=1,
            discovery_id=uuid4(),
            descriptor_hash="hash123",
            remote_capability_key="test-tool",
            purpose="chat",
            source_ids=(),
            destination_id="local",
            timeout_seconds=30,
            limits={"argument_bytes": 100},
        )
        with pytest.raises(McpTransportError, match="arguments exceed the reviewed size limit"):
            await client.call_tool(
                scope=SCOPE,
                fence=fence,
                arguments={"payload": "x" * 200},
                before_request=AsyncMock(return_value=True),
            )

    @pytest.mark.asyncio
    async def test_call_tool_descriptor_conflict(self, mock_operation_slot, dummy_connection) -> None:
        """Verify call_tool raises McpConflict when server descriptor does not match fence."""
        load_connection = AsyncMock(return_value=(dummy_connection, "secret-token"))
        client = McpSdkClient(
            load_connection=load_connection,
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        fence = ExecutionFence(
            connection_id=dummy_connection.id,
            connection_revision=1,
            deployment_profile_hash=None,
            grant_id=uuid4(),
            grant_revision=1,
            discovery_id=uuid4(),
            descriptor_hash="b" * 64,
            remote_capability_key="test-tool",
            purpose="chat",
            source_ids=(),
            destination_id="local",
            timeout_seconds=30,
            limits={"argument_bytes": 1000},
        )

        mock_client = MagicMock()
        # Mock _list_capability_pages returning a capability with a different hash
        descriptor = CapabilityDescriptor(
            kind="tool",
            remote_key="test-tool",
            descriptor={"name": "test-tool", "inputSchema": {}},
            descriptor_hash="a" * 64,
        )

        @asynccontextmanager
        async def mock_open_client(*args, **kwargs):
            yield mock_client

        with patch.object(client, "_open_client", side_effect=mock_open_client), \
             patch.object(client, "_list_capability_pages", AsyncMock(return_value=([descriptor], 1, 1))):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(mcp_repository.McpConflict, match="descriptor changed"):
                await client.call_tool(
                    scope=SCOPE,
                    fence=fence,
                    arguments={"input": "test"},
                    before_request=AsyncMock(return_value=True),
                )

    def test_validate_result_rejects_error_flag(self, mock_operation_slot) -> None:
        """Verify _validate_result raises when result.is_error is True."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        bad_result = DummyResult(is_error=True)
        with pytest.raises(McpTransportError, match="server returned a tool error result"):
            client._validate_result(bad_result, 10_000)

    def test_validate_result_rejects_exceeded_byte_limit(self, mock_operation_slot) -> None:
        """Verify _validate_result raises when output exceeds authorized max_bytes."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        large_result = DummyResult(content=[{"type": "text", "text": "x" * 500}])
        with pytest.raises(McpTransportError, match="exceeds the authorized output limit"):
            client._validate_result(large_result, max_bytes=50)

    @pytest.mark.asyncio
    async def test_read_resource_rejects_non_resource_kind(self, mock_operation_slot) -> None:
        """Verify read_resource refuses template reads without reviewed parameter binding."""
        client = McpSdkClient(
            load_connection=AsyncMock(),
            record_draft_check=AsyncMock(),
            persist_discovery=AsyncMock(),
            connection_is_current=AsyncMock(),
            operation_slot=mock_operation_slot,
        )
        fence = ExecutionFence(
            connection_id=uuid4(),
            connection_revision=1,
            deployment_profile_hash=None,
            grant_id=uuid4(),
            grant_revision=1,
            discovery_id=uuid4(),
            descriptor_hash="hash",
            remote_capability_key="resource://test",
            purpose="chat",
            source_ids=(),
            destination_id="local",
            timeout_seconds=30,
            limits={},
        )
        with pytest.raises(mcp_repository.McpUnavailable, match="resource-template reads require"):
            await client.read_resource(
                scope=SCOPE,
                fence=fence,
                capability_kind="resource_template",
                before_request=AsyncMock(return_value=True),
            )
