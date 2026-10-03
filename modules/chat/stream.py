"""Server-Sent Events (SSE) formatting, event sequence parsing, and bounded stream buffering."""

import asyncio
import json
import re
from typing import Any
from uuid import UUID

_EVENT_ID_RE = re.compile(r"^(.*?):(\d+)$")
MAX_STREAM_BUFFER_SIZE = 500


def make_event_id(response_id: UUID | str, seq: int) -> str:
    """Construct a deterministic SSE event identifier from response run ID and sequence number.

    Args:
        response_id: Unique UUID or string identifier for the response run.
        seq: Monotonic 1-based sequence integer within the run.

    Returns:
        Formatted event ID string in '<response_id>:<seq>' format.
    """
    return f"{response_id}:{seq}"


def parse_event_id(raw_id: str) -> tuple[str, int]:
    """Parse an SSE event identifier or resume cursor into run identifier and sequence integer.

    Accepts '<response_id>:<seq>' or bare integer sequence strings.

    Args:
        raw_id: Raw Last-Event-ID string from client request headers or query params.

    Returns:
        Tuple of (prefix_or_response_id, sequence_number).

    Raises:
        ValueError: When the input string cannot be parsed into a sequence number.
    """
    cleaned = raw_id.strip()
    match = _EVENT_ID_RE.match(cleaned)
    if match:
        prefix, seq_str = match.groups()
        return prefix, int(seq_str)
    if cleaned.isdigit():
        return "", int(cleaned)
    raise ValueError(f"Invalid SSE event ID format: {raw_id}")


def format_sse_event(
    event: str,
    data: Any,
    event_id: str | None = None,
    retry_ms: int | None = None,
) -> str:
    """Format an SSE frame conforming to standard text/event-stream protocol.

    Args:
        event: SSE event type name (e.g., 'message.delta', 'status', 'message.done').
        data: Payload dictionary, string, or JSON-serializable object.
        event_id: Optional unique event identifier for client resume tracking.
        retry_ms: Optional reconnection retry timeout in milliseconds.

    Returns:
        Properly newline-delimited SSE text chunk ending with double newline.
    """
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"event: {event}")

    if isinstance(data, str):
        payload_text = data
    else:
        payload_text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    for line in payload_text.splitlines():
        lines.append(f"data: {line}")
    if not payload_text:
        lines.append("data: ")

    return "\n".join(lines) + "\n\n"


class StreamBuffer:
    """Bounded asynchronous queue buffer for decoupled producer-consumer streaming.

    Provides bounded capacity to avoid unbounded memory growth when client consumption
    lags behind model token generation.
    """

    def __init__(self, maxsize: int = MAX_STREAM_BUFFER_SIZE) -> None:
        """Initialize the bounded stream queue.

        Args:
            maxsize: Maximum number of buffered events before backpressure is applied.
        """
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=maxsize)
        self._closed: bool = False

    async def put(self, event_chunk: str, timeout: float = 5.0) -> bool:
        """Push a formatted SSE event chunk into the buffer with timeout.

        Args:
            event_chunk: Formatted SSE chunk text.
            timeout: Maximum seconds to wait if the queue is full.

        Returns:
            True if chunk was enqueued, False if buffer was closed or timed out.
        """
        if self._closed:
            return False
        try:
            await asyncio.wait_for(self._queue.put(event_chunk), timeout=timeout)
            return True
        except (TimeoutError, asyncio.QueueFull):
            return False

    async def get(self) -> str | None:
        """Retrieve the next event chunk from the buffer.

        Returns:
            The event string, or None if the stream has finished and was closed.
        """
        return await self._queue.get()

    def close(self) -> None:
        """Signal end of stream to consumer and close the buffer."""
        if not self._closed:
            self._closed = True
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    @property
    def is_closed(self) -> bool:
        """Check whether the buffer has been closed."""
        return self._closed
