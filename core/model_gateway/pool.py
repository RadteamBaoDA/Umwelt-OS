"""Per-process pool of pinned model-gateway HTTP clients (one per origin/policy), closed on shutdown.

``ModelGateway`` used to build a fresh ``httpx.AsyncClient`` (and TLS handshake) for every call. The
pinned transport already resolves and validates DNS on EVERY request and dials only the checked literal
IP, so a client can safely outlive one call: pooled connections are keyed by that literal IP, a changed
or unapproved DNS answer is rejected before any connection is chosen, and the request-origin check still
runs per request. Credentials are never part of the pooled client (the SDK sends them per request), so
one client can serve any workspace whose network policy has the same key.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import logging
import weakref
from collections import OrderedDict
from collections.abc import Sequence

import httpx

from core.model_gateway.transport import approved_transport

logger = logging.getLogger(__name__)

MAX_POOLED_CLIENTS = 32
# An evicted client may still serve an in-flight call, so it is closed after this grace period.
EVICTION_CLOSE_DELAY_SECONDS = 300.0

_CacheKey = tuple[str, str, int | None, tuple[str, ...], bool, str]


class _NoCookiePolicy(http.cookiejar.CookiePolicy):
    """Cookie policy that stores and returns nothing, so no Set-Cookie ever crosses tenants."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        """Reject every cookie a provider response tries to set."""
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        """Never replay a cookie."""
        return False

    def domain_return_ok(self, domain: str, request: object) -> bool:
        """Never replay a cookie for any domain."""
        return False

    def path_return_ok(self, path: str, request: object) -> bool:
        """Never replay a cookie for any path."""
        return False


class SharedHttpClient(httpx.AsyncClient):
    """Pinned client whose ``aclose`` is a no-op so SDK context exits cannot close the shared pool.

    ``AsyncOpenAI.__aexit__`` closes the ``http_client`` it was given; for a pooled client that would
    tear down every other caller's connections. Only ``force_close`` (pool eviction/shutdown) closes it.
    The cookie jar rejects everything: one client serves many workspaces/credentials, so provider
    cookies must never be stored and replayed across tenants (each call used to start with an empty jar).
    """

    def __init__(self, **kwargs: object) -> None:
        """Build the client and swap in a jar whose policy accepts and returns no cookies."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.cookies.jar.set_policy(_NoCookiePolicy())

    async def aclose(self) -> None:
        """Ignore close requests from per-call SDK clients; the pool owns the lifetime."""

    async def force_close(self) -> None:
        """Really close the underlying transport and connection pool (pool eviction or shutdown)."""
        await super().aclose()


# value: (weak ref to the owning event loop, client); a client is only reused on its own loop.
_POOL: OrderedDict[
    _CacheKey, tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], SharedHttpClient]
] = OrderedDict()
_CLOSERS: set[asyncio.Task[None]] = set()


def _key(
    base_url: str, approved_cidrs: Sequence[str], allow_global: bool, protocol: str
) -> _CacheKey:
    """Build the cache key: origin (scheme/host/port), sorted CIDR policy, global flag, protocol."""
    origin = httpx.URL(base_url)
    return (
        origin.scheme,
        origin.raw_host.decode("ascii").lower(),
        origin.port,
        tuple(sorted(approved_cidrs)),
        allow_global,
        protocol,
    )


def _schedule_close(client: SharedHttpClient) -> None:
    """Close an evicted client after a grace period (best effort; never raises)."""

    async def close_later() -> None:
        """Wait out the eviction grace period, then really close the evicted client."""
        await asyncio.sleep(EVICTION_CLOSE_DELAY_SECONDS)
        with contextlib.suppress(Exception):
            await client.force_close()

    task = asyncio.get_running_loop().create_task(close_later())
    _CLOSERS.add(task)
    task.add_done_callback(_CLOSERS.discard)


def pinned_client(
    base_url: str,
    approved_cidrs: Sequence[str],
    *,
    allow_global: bool = False,
    protocol: str = "",
) -> httpx.AsyncClient:
    """Return the shared pinned client for this origin/policy/protocol, creating it on first use.

    Same construction as ``transport.approved_http_client`` (no proxy, no redirects, HTTP/1.1, no env
    trust) so the DNS-pinning and SSRF guarantees are identical. Raises ``EndpointNetworkPolicyError``
    (never cached) for malformed or empty CIDR policy. LRU bounded to ``MAX_POOLED_CLIENTS``; must be
    called from a running event loop.
    """
    key = _key(base_url, approved_cidrs, allow_global, protocol)
    loop = asyncio.get_running_loop()
    entry = _POOL.get(key)
    if entry is not None and entry[0]() is loop:
        _POOL.move_to_end(key)
        return entry[1]
    # An httpx pool is bound to the loop that opened its connections: an entry from another (e.g.
    # closed) loop is dropped, never reused.
    _POOL.pop(key, None)
    transport = approved_transport(base_url, approved_cidrs, allow_global=allow_global)
    client = SharedHttpClient(transport=transport, trust_env=False, follow_redirects=False)
    _POOL[key] = (weakref.ref(loop), client)
    while len(_POOL) > MAX_POOLED_CLIENTS:
        _schedule_close(_POOL.popitem(last=False)[1][1])
    return client


async def close_pinned_clients() -> None:
    """Close every pooled client (API lifespan / worker shutdown); safe to call repeatedly."""
    clients = [client for _loop, client in _POOL.values()]
    _POOL.clear()
    for task in list(_CLOSERS):
        task.cancel()
    for client in clients:
        try:
            await client.force_close()
        except Exception as exc:  # noqa: BLE001  # boundary: shutdown must close the remaining clients
            logger.warning("Model gateway client close failed (%s)", type(exc).__name__)
