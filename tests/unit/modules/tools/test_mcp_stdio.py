"""Unit tests for modules.tools.mcp_stdio.

Covers POSIX stdio deployment profiles, pipe stream wrappers,
process protocol management, JSON-RPC stdout/stdin framing,
buffer slicing across chunks, EOF handling, and execution fencing.
"""

from __future__ import annotations

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from anyio.abc import ByteReceiveStream, ByteSendStream
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCResponse

from modules.tools.mcp_stdio import (
    StdioDeploymentProfile,
    _read_stdio_stdout,
    _StdioProcess,
    _StdioReadPipe,
    _StdioWritePipe,
    _write_stdio_stdin,
    stdio_client_transport,
)
from modules.tools.mcp_transport import McpTransportError


class TestStdioDeploymentProfile:
    """Unit tests for the immutable StdioDeploymentProfile structure."""

    def test_profile_fields_and_immutability(self) -> None:
        """Verify StdioDeploymentProfile fields and frozen dataclass semantics."""
        profile = StdioDeploymentProfile(
            profile_id="test-profile",
            profile_hash="a" * 64,
            reviewed_profile_hash="a" * 64,
            enabled=True,
            platform="posix",
            executable="/bin/echo",
            argv=("/bin/echo", "hello"),
            cwd="/tmp",
            environment={"LANG": "en_US.UTF-8"},
            immutable_root="/tmp",
            artifact_sha256={"/bin/echo": "b" * 64},
            operation_kind="ordinary",
            runtime_kind="native",
            entry_script=None,
        )
        assert profile.profile_id == "test-profile"
        assert profile.enabled is True
        with pytest.raises(AttributeError):
            profile.enabled = False  # type: ignore


import time


class TestStdioProcess:
    """Unit tests for the _StdioProcess asyncio subprocess protocol."""

    def test_process_properties_and_signals(self) -> None:
        """Verify _StdioProcess exposes none for pipes and forwards signals to transport."""
        proc = _StdioProcess(spawn_deadline=time.monotonic() + 10)
        assert proc.stdin is None
        assert proc.stdout is None
        assert proc.stderr is None
        assert proc.returncode is None

        mock_transport = MagicMock(spec=asyncio.SubprocessTransport)
        mock_transport.get_pid.return_value = 1234
        mock_transport.get_returncode.return_value = None

        proc.connection_made(mock_transport)
        assert proc.pid == 1234
        assert proc.returncode is None

        proc.terminate()
        mock_transport.send_signal.assert_called_once_with(signal.SIGTERM)

        sigkill = getattr(signal, "SIGKILL", 9)
        with patch.object(signal, "SIGKILL", sigkill, create=True):
            proc.kill()
            mock_transport.send_signal.assert_called_with(sigkill)

    @pytest.mark.asyncio
    async def test_process_wait_and_exit(self) -> None:
        """Verify _StdioProcess wait completes when process_exited is called."""
        proc = _StdioProcess(spawn_deadline=time.monotonic() + 10)
        mock_transport = MagicMock(spec=asyncio.SubprocessTransport)
        mock_transport.get_pid.return_value = 1234
        mock_transport.get_returncode.return_value = 0

        proc.connection_made(mock_transport)
        proc.process_exited()

        code = await proc.wait()
        assert code == 0

    def test_process_abort_before_connection(self) -> None:
        """Verify abort prior to connection marks process aborted."""
        proc = _StdioProcess(spawn_deadline=time.monotonic() + 10)
        proc.abort()
        assert proc._aborted is True


class TestStdioReadAndWritePipes:
    """Unit tests for low-level _StdioReadPipe and _StdioWritePipe."""

    @pytest.mark.asyncio
    async def test_read_pipe_closed_resource_error(self) -> None:
        """Verify receive on closed _StdioReadPipe raises ClosedResourceError."""
        pipe = _StdioReadPipe(fd=999)
        pipe._closed = True
        with pytest.raises(anyio.ClosedResourceError):
            await pipe.receive()

    @pytest.mark.asyncio
    async def test_read_pipe_invalid_max_bytes(self) -> None:
        """Verify non-positive max_bytes raises ValueError."""
        pipe = _StdioReadPipe(fd=999)
        with pytest.raises(ValueError, match="max_bytes must be positive"):
            await pipe.receive(0)

    @pytest.mark.asyncio
    async def test_write_pipe_closed_resource_error(self) -> None:
        """Verify send on closed _StdioWritePipe raises ClosedResourceError."""
        pipe = _StdioWritePipe(fd=999)
        pipe._closed = True
        with pytest.raises(anyio.ClosedResourceError):
            await pipe.send(b"data")


class MockByteReceiveStream(ByteReceiveStream):
    """Mock ByteReceiveStream delivering pre-configured chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Initialize with a sequence of chunks."""
        self._chunks = list(chunks)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        """Pop next chunk or raise EndOfStream."""
        if not self._chunks:
            raise anyio.EndOfStream
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        """Close stream."""
        self._chunks.clear()


class MockByteSendStream(ByteSendStream):
    """Mock ByteSendStream accumulating sent bytes."""

    def __init__(self) -> None:
        """Initialize empty buffer."""
        self.sent: list[bytes] = []

    async def send(self, item: bytes) -> None:
        """Record written bytes."""
        self.sent.append(item)

    async def aclose(self) -> None:
        """Close stream."""


class TestStdioFraming:
    """Unit tests for JSON-RPC frame parsing, buffer slicing, and EOF handling."""

    @pytest.mark.asyncio
    async def test_read_stdio_stdout_single_frame(self) -> None:
        """Verify _read_stdio_stdout reads one complete JSON-RPC frame and sends SessionMessage."""
        json_data = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": "ok"}}).encode("utf-8") + b"\n"
        stream = MockByteReceiveStream([json_data])
        rs, rr = anyio.create_memory_object_stream[SessionMessage | Exception](10)
        shutdown = anyio.Event()
        counters = [0, 0]

        async def run_reader():
            await _read_stdio_stdout(
                stdout=stream,
                messages=rs,
                fence=AsyncMock(return_value=True),
                counters=counters,
                max_frame_bytes=65536,
                max_total_bytes=65536,
                max_frames=10,
                shutdown=shutdown,
            )

        with pytest.raises(McpTransportError, match="server closed stdout unexpectedly"):
            await run_reader()

        # Before raising EOF, it should have sent the message
        msg = await rr.receive()
        assert isinstance(msg, SessionMessage)
        assert counters[1] == 1

    @pytest.mark.asyncio
    async def test_read_stdio_stdout_buffer_slicing_across_chunks(self) -> None:
        """Verify _read_stdio_stdout correctly reassembles a frame split across multiple chunks."""
        full_json = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"split": "data"}}).encode("utf-8") + b"\n"
        part1 = full_json[:10]
        part2 = full_json[10:]

        stream = MockByteReceiveStream([part1, part2])
        rs, rr = anyio.create_memory_object_stream[SessionMessage | Exception](10)
        shutdown = anyio.Event()
        counters = [0, 0]

        async def run_reader():
            await _read_stdio_stdout(
                stdout=stream,
                messages=rs,
                fence=AsyncMock(return_value=True),
                counters=counters,
                max_frame_bytes=65536,
                max_total_bytes=65536,
                max_frames=10,
                shutdown=shutdown,
            )

        with pytest.raises(McpTransportError, match="server closed stdout unexpectedly"):
            await run_reader()

        msg = await rr.receive()
        assert isinstance(msg, SessionMessage)
        assert counters[1] == 1

    @pytest.mark.asyncio
    async def test_read_stdio_stdout_truncated_frame_on_eof(self) -> None:
        """Verify EOF in the middle of a frame raises McpTransportError for truncated frame."""
        partial = b'{"jsonrpc": "2.0", "id": 3'
        stream = MockByteReceiveStream([partial])
        rs, _ = anyio.create_memory_object_stream[SessionMessage | Exception](10)
        shutdown = anyio.Event()
        counters = [0, 0]

        with pytest.raises(McpTransportError, match="truncated frame"):
            await _read_stdio_stdout(
                stdout=stream,
                messages=rs,
                fence=AsyncMock(return_value=True),
                counters=counters,
                max_frame_bytes=65536,
                max_total_bytes=65536,
                max_frames=10,
                shutdown=shutdown,
            )

    @pytest.mark.asyncio
    async def test_read_stdio_stdout_exceeds_frame_limit(self) -> None:
        """Verify exceeding max_frame_bytes raises McpTransportError."""
        oversized = b"x" * 200 + b"\n"
        stream = MockByteReceiveStream([oversized])
        rs, _ = anyio.create_memory_object_stream[SessionMessage | Exception](10)
        shutdown = anyio.Event()
        counters = [0, 0]

        with pytest.raises(McpTransportError, match="frame exceeds its byte limit"):
            await _read_stdio_stdout(
                stdout=stream,
                messages=rs,
                fence=AsyncMock(return_value=True),
                counters=counters,
                max_frame_bytes=100,
                max_total_bytes=65536,
                max_frames=10,
                shutdown=shutdown,
            )

    @pytest.mark.asyncio
    async def test_read_stdio_stdout_fence_change_raises(self) -> None:
        """Verify fence check failure before frame delivery raises McpTransportError."""
        json_data = json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"status": "ok"}}).encode("utf-8") + b"\n"
        stream = MockByteReceiveStream([json_data])
        rs, _ = anyio.create_memory_object_stream[SessionMessage | Exception](10)
        shutdown = anyio.Event()
        counters = [0, 0]

        with pytest.raises(McpTransportError, match="fence changed before result delivery"):
            await _read_stdio_stdout(
                stdout=stream,
                messages=rs,
                fence=AsyncMock(return_value=False),
                counters=counters,
                max_frame_bytes=65536,
                max_total_bytes=65536,
                max_frames=10,
                shutdown=shutdown,
            )

    @pytest.mark.asyncio
    async def test_write_stdio_stdin_success(self) -> None:
        """Verify _write_stdio_stdin serializes message with newline delimiter."""
        send_stream = MockByteSendStream()
        ws, wr = anyio.create_memory_object_stream[SessionMessage](10)
        writer_done = anyio.Event()
        counters = [0, 0]

        message = JSONRPCResponse(jsonrpc="2.0", id=1, result={"status": "ok"})
        session_msg = SessionMessage(message)
        await ws.send(session_msg)
        ws.close()

        await _write_stdio_stdin(
            stdin=send_stream,
            messages=wr,
            fence=AsyncMock(return_value=True),
            counters=counters,
            max_total_bytes=65536,
            max_frames=10,
            writer_done=writer_done,
        )

        assert writer_done.is_set()
        assert len(send_stream.sent) == 1
        assert send_stream.sent[0].endswith(b"\n")
        assert counters[1] == 1

    @pytest.mark.asyncio
    async def test_write_stdio_stdin_fence_denied_raises(self) -> None:
        """Verify revoked fence before writing raises McpTransportError."""
        send_stream = MockByteSendStream()
        ws, wr = anyio.create_memory_object_stream[SessionMessage](10)
        writer_done = anyio.Event()
        counters = [0, 0]

        message = JSONRPCResponse(jsonrpc="2.0", id=1, result={})
        await ws.send(SessionMessage(message))
        ws.close()

        with pytest.raises(McpTransportError, match="fence changed before request delivery"):
            await _write_stdio_stdin(
                stdin=send_stream,
                messages=wr,
                fence=AsyncMock(return_value=False),
                counters=counters,
                max_total_bytes=65536,
                max_frames=10,
                writer_done=writer_done,
            )
        assert writer_done.is_set()


class TestStdioClientTransportPlatform:
    """Unit tests for platform compatibility check of stdio_client_transport."""

    @pytest.mark.asyncio
    async def test_transport_fails_on_non_posix(self) -> None:
        """Verify stdio_client_transport raises McpTransportError on non-POSIX platforms."""
        profile = StdioDeploymentProfile(
            profile_id="test",
            profile_hash="a" * 64,
            reviewed_profile_hash="a" * 64,
            enabled=True,
            platform="posix",
            executable="/bin/echo",
            argv=("/bin/echo",),
            cwd="/tmp",
            environment={},
            immutable_root="/tmp",
            artifact_sha256={},
            operation_kind="ordinary",
            runtime_kind="native",
            entry_script=None,
        )
        with patch("os.name", "nt"):  # noqa: SIM117  # style-only rewrite skipped to avoid touching control flow
            with pytest.raises(McpTransportError, match="MCP stdio requires POSIX"):
                async with stdio_client_transport(profile, AsyncMock(return_value=True)):
                    pass  # pragma: no cover
