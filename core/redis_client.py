"""Redis connection factories shared by the API and the ARQ workers, with optional Sentinel HA.

``REDIS_URL`` keeps authenticating (password, username, database) in every mode. When ``REDIS_SENTINELS`` is
set, the host/port in ``REDIS_URL`` are ignored and the master named ``REDIS_MASTER_NAME`` is discovered
through the listed Sentinels, so a failover only needs the clients to reconnect (``retry_on_timeout`` and the
health check below do that), never a configuration change. The public API of the returned clients is unchanged.
"""
from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

from arq.connections import ArqRedis, RedisSettings
from redis.asyncio.sentinel import Sentinel

from core.config import Settings

DEFAULT_SENTINEL_PORT = 26379


def parse_sentinels(raw: str | None) -> list[tuple[str, int]]:
    """Parse ``"host[:port],host[:port]"`` into ``(host, port)`` pairs (default port 26379); empty means none.

    Raises:
        ValueError: an entry has an empty host or a non-numeric/out-of-range port.
    """
    pairs: list[tuple[str, int]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, port = entry.rsplit(":", 1) if ":" in entry else (entry, "")
        if not host or (port and not (port.isdigit() and 0 < int(port) < 65536)):
            raise ValueError(f"Invalid Redis sentinel entry: {entry!r}")
        pairs.append((host, int(port) if port else DEFAULT_SENTINEL_PORT))
    return pairs


def build_redis_settings(settings: Settings) -> RedisSettings:
    """Return the ARQ ``RedisSettings`` for workers: Sentinel-aware when ``REDIS_SENTINELS`` is configured.

    Sentinel mode reuses the credentials and database of ``redis_url`` and asks arq to resolve the master by
    ``redis_master_name``; otherwise this is exactly ``RedisSettings.from_dsn(redis_url)``.
    """
    base = RedisSettings.from_dsn(settings.redis_url)
    sentinels = parse_sentinels(settings.redis_sentinels)
    if not sentinels:
        return base
    base.host = sentinels
    base.sentinel = True
    base.sentinel_master = settings.redis_master_name
    return base


def _sentinel_master_client(
    settings: Settings, sentinels: list[tuple[str, int]], *, redis_class: type[ArqRedis] = ArqRedis,
    **client_kwargs: Any,
) -> ArqRedis:
    """Build an ``ArqRedis`` (or ``redis_class`` subclass) bound to the Sentinel-resolved master.

    Shared by the API and the ARQ workers. Carries ``REDIS_SENTINEL_PASSWORD`` to the Sentinel connections (distinct from the data-node password in
    ``REDIS_URL``) and the same socket timeouts/retry policy in both paths. No I/O happens until first use.
    """
    url = urlsplit(settings.redis_url)
    sentinel_password = settings.redis_sentinel_password.get_secret_value() or None
    sentinel = Sentinel(  # type: ignore[no-untyped-call]  # redis-py ships Sentinel untyped
        sentinels,
        sentinel_kwargs={"password": sentinel_password, "socket_timeout": 2, "socket_connect_timeout": 2},
    )
    path = url.path.lstrip("/")
    client = sentinel.master_for(
        settings.redis_master_name,
        redis_class=redis_class,
        username=url.username,
        password=url.password,
        db=int(path) if path.isdigit() else 0,
        retry_on_timeout=True,
        socket_timeout=5,
        socket_connect_timeout=2,
        health_check_interval=30,
        **client_kwargs,
    )
    return cast(ArqRedis, client)  # redis_class guarantees the type


def build_worker_redis_pool(settings: Settings) -> ArqRedis | None:
    """Return a Sentinel-resolved ``ArqRedis`` pool for ARQ ``WorkerSettings.redis_pool``, or ``None`` when direct.

    arq's own Sentinel support cannot pass ``sentinel_kwargs``, so password-protected Sentinels would fail at
    worker startup; supplying the pool ourselves keeps the sentinel password and timeouts identical to the API.
    Responses stay bytes (arq decodes them itself), unlike the API client.
    """
    sentinels = parse_sentinels(settings.redis_sentinels)
    if not sentinels:
        return None
    return _sentinel_master_client(settings, sentinels, max_connections=100)


def create_api_redis(settings: Settings, *, redis_class: type[ArqRedis] = ArqRedis) -> ArqRedis:
    """Create the API's shared ``ArqRedis`` client (direct or Sentinel-resolved master); no I/O happens here.

    ``redis_class`` lets the API pass an ``ArqRedis`` subclass (``core.tracing.TracingArqRedis``, which adds
    the caller's trace context to enqueued jobs) that is used in both the direct and the Sentinel path.

    Socket timeouts bound every call, so a Redis outage or failover surfaces as ``RedisError``/``TimeoutError``
    that the API maps to a retryable 503 (see ``core.errors``) instead of hanging a request.
    """
    common: dict[str, Any] = {
        "decode_responses": True,
        "socket_timeout": 5,
        "socket_connect_timeout": 2,
        "health_check_interval": 30,
        "max_connections": 100,
    }
    sentinels = parse_sentinels(settings.redis_sentinels)
    if not sentinels:
        return cast(ArqRedis, redis_class.from_url(settings.redis_url, **common))
    return _sentinel_master_client(
        settings, sentinels, redis_class=redis_class, decode_responses=True, max_connections=100,
    )
