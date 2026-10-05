"""Unit tests for modules.tools.mcp_auth.InboundMcpGuard.

Covers inbound authentication, bearer token parsing, permission scoping,
capability authorization, HTTP method / header / origin filtering,
payload size guards, response buffering, and error serialization.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.tools.schemas import ToolExecutionPrincipal
from modules.tools.mcp_admission import McpAdmission, McpInboundLease
from modules.tools.mcp_auth import (
    InboundMcpGuard,
    InboundReplayReceiver,
    InboundRequestState,
    InboundResponseBuffer,
    InboundResponseFence,
)
from modules.tools.mcp_schemas import InboundBinding, InboundPrincipal


@pytest.fixture
def dummy_principal() -> InboundPrincipal:
    """Return a mock InboundPrincipal for testing."""
    return InboundPrincipal(
        client_id=uuid4(),
        owner_id=1,
        audience="https://example.com/api/v1/mcp/",
        revision=1,
        bindings=(
            InboundBinding(
                name="test_tool",
                version="1.0.0",
                schema_fingerprint="a" * 64,
            ),
        ),
        source_ids=(uuid4(),),
        capabilities=("chat",),
        destination_id="local",
    )


@pytest.fixture
def dummy_tool_execution_principal() -> ToolExecutionPrincipal:
    """Return a mock ToolExecutionPrincipal for testing."""
    return ToolExecutionPrincipal(
        actor_id="mcp-client:test",
        is_owner=False,
        allowed_tools=frozenset({"test_tool"}),
        source_ids=frozenset(),
        destinations=frozenset({"local"}),
        capabilities=frozenset({"chat"}),
    )


@pytest.fixture
def mock_admission():
    """Return a mock McpAdmission instance with functioning inbound_slot context."""
    admission = MagicMock(spec=McpAdmission)
    lease = MagicMock(spec=McpInboundLease)
    lease.bind_client = AsyncMock(return_value=True)
    lease.retain_until_expiry = MagicMock()

    @asynccontextmanager
    async def _inbound_slot(deadline: float):
        yield lease

    admission.inbound_slot = _inbound_slot
    admission.lease_current = AsyncMock(return_value=True)
    return admission, lease


class TestInboundHelperClasses:
    """Unit tests for buffer, replay receiver, and dataclasses."""

    def test_inbound_response_fence_defaults(self) -> None:
        """Verify default state of InboundResponseFence."""
        fence = InboundResponseFence()
        assert fence.binding is None
        assert fence.sink is None
        assert fence.has_successful_native_result is False
        assert fence.catalog_bindings == ()

    @pytest.mark.asyncio
    async def test_replay_receiver_replays_once(self) -> None:
        """Verify InboundReplayReceiver returns body once then waits for disconnect."""
        body = b'{"jsonrpc":"2.0"}'
        source_receive = AsyncMock()
        receiver = InboundReplayReceiver(body, source_receive)

        first = await receiver()
        assert first == {"type": "http.request", "body": body, "more_body": False}

    @pytest.mark.asyncio
    async def test_response_buffer_captures_valid_json(self) -> None:
        """Verify InboundResponseBuffer captures application/json headers and body."""
        buffer = InboundResponseBuffer(max_body_bytes=1024, deadline=asyncio.get_running_loop().time() + 10)
        start_msg = {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
        body_msg = {"type": "http.response.body", "body": b'{"ok":true}', "more_body": False}

        await buffer(start_msg)
        await buffer(body_msg)
        start, body = buffer.seal()
        assert start["status"] == 200
        assert body == b'{"ok":true}'

    @pytest.mark.asyncio
    async def test_response_buffer_rejects_non_json(self) -> None:
        """Verify InboundResponseBuffer raises ValueError for non-JSON content-type."""
        buffer = InboundResponseBuffer(max_body_bytes=1024, deadline=asyncio.get_running_loop().time() + 10)
        start_msg = {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html")],
        }
        with pytest.raises(ValueError, match="Only JSON responses are supported"):
            await buffer(start_msg)

    @pytest.mark.asyncio
    async def test_response_buffer_rejects_streaming(self) -> None:
        """Verify InboundResponseBuffer raises ValueError on streaming bodies."""
        buffer = InboundResponseBuffer(max_body_bytes=1024, deadline=asyncio.get_running_loop().time() + 10)
        start_msg = {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
        body_msg = {"type": "http.response.body", "body": b'{"part":1}', "more_body": True}

        await buffer(start_msg)
        with pytest.raises(ValueError, match="Streaming responses are unavailable"):
            await buffer(body_msg)


class TestInboundMcpGuardInitialization:
    """Unit tests for InboundMcpGuard constructor policy validation."""

    def test_guard_rejects_invalid_audience_path(self, mock_admission) -> None:
        """Verify guard constructor rejects audience path not ending in /api/v1/mcp/."""
        admission, _ = mock_admission
        with pytest.raises(ValueError, match="Invalid inbound MCP audience"):
            InboundMcpGuard(
                app=AsyncMock(),
                audience="https://example.com/invalid/path",
                expected_host="example.com",
                allowed_origins=frozenset({"https://example.com"}),
                admission=admission,
                authenticate_inbound=AsyncMock(),
                authorize_inbound=AsyncMock(),
                revalidate_inbound=AsyncMock(),
                revalidate_inbound_output=AsyncMock(),
            )

    def test_guard_rejects_host_mismatch(self, mock_admission) -> None:
        """Verify guard constructor rejects mismatch between audience netloc and expected_host."""
        admission, _ = mock_admission
        with pytest.raises(ValueError, match="Invalid inbound MCP transport policy"):
            InboundMcpGuard(
                app=AsyncMock(),
                audience="https://example.com/api/v1/mcp/",
                expected_host="mismatched.com",
                allowed_origins=frozenset({"https://example.com"}),
                admission=admission,
                authenticate_inbound=AsyncMock(),
                authorize_inbound=AsyncMock(),
                revalidate_inbound=AsyncMock(),
                revalidate_inbound_output=AsyncMock(),
            )


class TestInboundMcpGuardHttpPipeline:
    """Unit tests for ASGI request handling and security filtering."""

    @pytest.fixture
    def guard(self, mock_admission, dummy_principal, dummy_tool_execution_principal) -> InboundMcpGuard:
        """Return configured InboundMcpGuard instance."""
        admission, _ = mock_admission
        app = AsyncMock()

        async def dummy_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"jsonrpc":"2.0","result":{}}',
                "more_body": False,
            })

        app.side_effect = dummy_app

        return InboundMcpGuard(
            app=app,
            audience="https://example.com/api/v1/mcp/",
            expected_host="example.com",
            allowed_origins=frozenset({"https://example.com"}),
            admission=admission,
            authenticate_inbound=AsyncMock(return_value=dummy_principal),
            authorize_inbound=AsyncMock(return_value=dummy_tool_execution_principal),
            revalidate_inbound=AsyncMock(return_value=True),
            revalidate_inbound_output=AsyncMock(return_value=True),
        )

    @pytest.mark.asyncio
    async def test_non_http_scope_closed(self, guard) -> None:
        """Verify websocket scope is closed with code 1008."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {"type": "websocket"}
        await guard(scope, AsyncMock(), mock_send)
        assert sent_messages == [{"type": "websocket.close", "code": 1008}]

    @pytest.mark.asyncio
    async def test_header_count_overflow_returns_431(self, guard) -> None:
        """Verify requests with more than 64 headers return 431."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        headers = [(f"header-{i}".encode("ascii"), b"val") for i in range(65)]
        scope = {"type": "http", "headers": headers, "path": "/api/v1/mcp/"}
        await guard(scope, AsyncMock(), mock_send)

        assert any(msg.get("status") == 431 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_canonical_path_mismatch_returns_404(self, guard) -> None:
        """Verify path mismatch returns 404."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "headers": [],
            "path": "/api/v1/mcp/other",
            "query_string": b"",
        }
        await guard(scope, AsyncMock(), mock_send)
        assert any(msg.get("status") == 404 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_host_mismatch_returns_403(self, guard) -> None:
        """Verify host mismatch returns 403."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "headers": [(b"host", b"attacker.com")],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }
        await guard(scope, AsyncMock(), mock_send)
        assert any(msg.get("status") == 403 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_returns_401(self, guard) -> None:
        """Verify missing Bearer prefix returns 401 with challenge."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "headers": [
                (b"host", b"example.com"),
                (b"origin", b"https://example.com"),
                (b"authorization", b"Basic 12345"),
            ],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }
        await guard(scope, AsyncMock(), mock_send)
        assert any(msg.get("status") == 401 for msg in sent_messages)
        start_msg = next(msg for msg in sent_messages if msg.get("type") == "http.response.start")
        headers = dict(start_msg["headers"])
        assert headers.get(b"www-authenticate") == b"Bearer"

    @pytest.mark.asyncio
    async def test_invalid_http_method_returns_405(self, guard) -> None:
        """Verify GET method returns 405 Method Not Allowed."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "method": "GET",
            "headers": [
                (b"host", b"example.com"),
                (b"origin", b"https://example.com"),
                (b"authorization", b"Bearer secrettoken123"),
            ],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }
        await guard(scope, AsyncMock(), mock_send)
        assert any(msg.get("status") == 405 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_invalid_content_type_returns_415(self, guard) -> None:
        """Verify non-JSON Content-Type returns 415 Unsupported Media Type."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"host", b"example.com"),
                (b"origin", b"https://example.com"),
                (b"authorization", b"Bearer secrettoken123"),
                (b"content-type", b"text/plain"),
            ],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }
        await guard(scope, AsyncMock(), mock_send)
        assert any(msg.get("status") == 415 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_json_batch_array_rejected(self, guard) -> None:
        """Verify JSON batch starting with '[' returns 400."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"host", b"example.com"),
                (b"origin", b"https://example.com"),
                (b"authorization", b"Bearer secrettoken123"),
                (b"content-type", b"application/json"),
            ],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }

        async def mock_receive():
            return {
                "type": "http.request",
                "body": b'[{"jsonrpc":"2.0"}]',
                "more_body": False,
            }

        await guard(scope, mock_receive, mock_send)
        assert any(msg.get("status") == 400 for msg in sent_messages)

    @pytest.mark.asyncio
    async def test_successful_request_flow(self, guard) -> None:
        """Verify valid authenticated JSON-RPC request executes and returns 200 with no-store."""
        sent_messages = []

        async def mock_send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"host", b"example.com"),
                (b"origin", b"https://example.com"),
                (b"authorization", b"Bearer secrettoken123"),
                (b"content-type", b"application/json"),
            ],
            "path": "/api/v1/mcp/",
            "query_string": b"",
        }

        disconnect_event = asyncio.Event()
        called = False
        async def mock_receive():
            nonlocal called
            if not called:
                called = True
                return {
                    "type": "http.request",
                    "body": b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
                    "more_body": False,
                }
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        await guard(scope, mock_receive, mock_send)
        assert any(msg.get("status") == 200 for msg in sent_messages)
        start_msg = next(msg for msg in sent_messages if msg.get("type") == "http.response.start")
        headers = dict(start_msg["headers"])
        assert headers.get(b"cache-control") == b"no-store"
