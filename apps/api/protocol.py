"""uvicorn HTTP protocol that restores TCP_NODELAY for the multi-worker pre-bound socket."""

import asyncio
import socket

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol


class NoDelayHttpToolsProtocol(HttpToolsProtocol):
    """HttpToolsProtocol that sets TCP_NODELAY on each accepted connection.

    With --workers > 1 uvicorn pre-binds the socket with proto=0, so asyncio skips its own
    TCP_NODELAY and Nagle + delayed ACK adds ~40 ms to small keep-alive responses.
    """

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        """Enable TCP_NODELAY (best effort; non-TCP transports are ignored) then defer to uvicorn."""
        sock = transport.get_extra_info("socket")
        if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        super().connection_made(transport)
