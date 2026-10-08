"""Bounded, DNS-pinned HTTP transport and REST record mapping for native collection.

Every request resolves the host once, requires every address to be public and connects to the
validated IP while keeping the original Host header and TLS SNI/certificate name, so a DNS answer
that changes after validation cannot redirect the send. Redirects are never followed. Limits are
hard: a page, record, byte or deadline cap reached while more data exists raises
``CollectionIncomplete`` and the caller must not advance the cursor (no slice-and-advance).
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address
from socket import getaddrinfo
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from modules.connectors.providers.feed_catalog import _retry_deadline
from modules.connectors.public import ProviderRateLimited, overlap_floor

MAX_PAGES = 10
MAX_RECORDS = 500
MAX_BATCH_BYTES = 10 * 1024 * 1024
MAX_TRANSPORT_BYTES = 25 * 1024 * 1024
MAX_PAGE_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
RUN_DEADLINE_SECONDS = 90

Resolver = Callable[[str, int], Awaitable[list[str]]]
BeforeSend = Callable[[], Awaitable[None]]


class CollectionIncomplete(Exception):  # noqa: N818  # control-flow signal named by the protocol's collection_incomplete code
    """A cap was reached while more data exists; nothing may be accepted or advanced."""


class UnsafeDestination(ValueError):
    """The URL, its resolution or a pagination link is not an allowed public destination."""


class RestSchemaChanged(ValueError):
    """The response is not the configured JSON shape; owner action is required."""


class ProviderHttpError(RuntimeError):
    """A non-success status without response text; 5xx and 408 are retryable."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http_{status_code}")
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """Report whether the same request may succeed later."""
        return self.status_code >= 500 or self.status_code in {408, 425}


@dataclass(frozen=True)
class Fetched:
    """One bounded response body; ``body is None`` means HTTP 304."""

    body: bytes | None
    etag: str | None = None
    last_modified: str | None = None


async def resolve_public(host: str, port: int) -> list[str]:
    """Resolve once and require every returned address to be globally routable."""
    try:
        infos = await asyncio.to_thread(getaddrinfo, host, port, 0, 0, 0)
    except OSError as exc:
        raise UnsafeDestination("host_unresolvable") from exc
    addresses = [str(item[4][0]) for item in infos]
    if not addresses or any(not ip_address(address).is_global for address in addresses):
        raise UnsafeDestination("non_public_destination")
    return addresses


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def _pinned_request(url: str, address: str, headers: dict[str, str]) -> tuple[str, dict[str, str], dict[str, object]]:
    """Rewrite ``url`` to the validated IP, preserving Host and the TLS server name."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{port}" if port else literal
    pinned = parts._replace(netloc=netloc).geturl()
    request_headers = {**headers, "Host": f"{host}:{port}" if port else host}
    extensions: dict[str, object] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, request_headers, extensions


async def fetch_bounded(
    url: str, *, headers: dict[str, str] | None = None, before_send: BeforeSend,
    resolve: Resolver = resolve_public, transport: httpx.AsyncBaseTransport | None = None,
    max_bytes: int = MAX_PAGE_BYTES, timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Fetched:
    """GET one URL through the pinned public destination with byte, status and time bounds.

    ``before_send`` runs exactly once, after resolution and immediately before the request is
    handed to the transport; it is where the caller revalidates fences and debits quota, and
    raising from it aborts the send. 429 raises ``ProviderRateLimited`` with the full
    Retry-After; redirects and other 3xx raise ``UnsafeDestination``; 304 returns no body.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise UnsafeDestination("url_not_allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = await resolve(parts.hostname, port)
    if not addresses or any(not ip_address(address).is_global for address in addresses):
        raise UnsafeDestination("non_public_destination")
    pinned, request_headers, extensions = _pinned_request(url, addresses[0], dict(headers or {}))
    async with httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(timeout), trust_env=False,
        follow_redirects=False, verify=True,
    ) as client:
        await before_send()
        async with asyncio.timeout(timeout), client.stream(
            "GET", pinned, headers=request_headers, extensions=extensions,
        ) as response:
            status = response.status_code
            if status == 429:
                raise ProviderRateLimited(_retry_deadline(response.headers, datetime.now(UTC)))
            if status == 304:
                return Fetched(None)
            if 300 <= status < 400:
                raise UnsafeDestination("redirect_blocked")
            if status >= 400:
                raise ProviderHttpError(status)
            advertised = response.headers.get("content-length")
            if advertised is not None and advertised.isdigit() and int(advertised) > max_bytes:
                raise CollectionIncomplete
            body = bytearray()
            async for chunk in response.aiter_bytes():  # decoded bytes: a compression bomb is cut at the cap
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise CollectionIncomplete
            return Fetched(bytes(body), response.headers.get("etag"), response.headers.get("last-modified"))


def _walk(value: Any, path: str | None) -> Any:
    if not path:
        return value
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _parse_time(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))  # noqa: FURB162  # exact 'Z' handling
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


@dataclass
class RestCollection:
    """Mapped records plus the cursor that is only valid when the walk was complete."""

    records: list[dict[str, Any]] = field(default_factory=list)
    cursor_after: str | None = None
    pages: int = 0
    transport_bytes: int = 0


def map_page(payload: Any, config: Any, floor: datetime | None) -> list[tuple[dict[str, Any], datetime | None]]:
    """Map one decoded page with the configured dotted paths; no expression evaluation.

    Rows without an id are skipped; rows with a parseable time older than ``floor`` are
    overlap-filtered; rows with no usable time always pass (as the managed template did).
    """
    rows = _walk(payload, config.items_path)
    if not isinstance(rows, list):
        raise RestSchemaChanged("items_path_not_array")
    from modules.connectors.registry import normalize

    mapped: list[tuple[dict[str, Any], datetime | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = _walk(row, config.id_field)
        if identifier is None:
            continue
        raw_time = _walk(row, config.updated_field) if config.updated_field else None
        moment = _parse_time(raw_time)
        if moment is not None and floor is not None and moment < floor:
            continue
        title = _walk(row, config.title_field) if config.title_field else None
        body = _walk(row, config.content_field) if config.content_field else None
        mapped.append((normalize({
            "provider_id": str(identifier),
            "content": str(body if body is not None else (title or "")),
            "observed_at": datetime.now(UTC).isoformat(),
            "version": str(raw_time) if moment is not None else None,
            "metadata": {"title": title[:500] if isinstance(title, str) else None, "published_at": None},
        }), moment))
    return mapped


async def collect_rest(
    config: Any, cursor: str | None, *, fetch: Callable[[str], Awaitable[Fetched]],
) -> RestCollection:
    """Walk ``next_url`` pages (same origin only) and map them; incomplete walks raise.

    ``fetch`` is the caller's gated transport (one debited/fenced physical send per call).
    Returns an empty record list when nothing is new; the caller then settles ``no_changes``.
    """
    start = str(config.url)
    origin = _origin(start)
    floor = overlap_floor(cursor)
    seen: set[str] = set()
    result = RestCollection()
    times: list[datetime] = []
    batch_bytes = 0
    url: str | None = start
    while url is not None:
        if url in seen:
            raise UnsafeDestination("pagination_loop")
        if result.pages >= MAX_PAGES:
            raise CollectionIncomplete  # more pages exist beyond the cap
        seen.add(url)
        fetched = await fetch(url)
        result.pages += 1
        if fetched.body is None:
            break
        result.transport_bytes += len(fetched.body)
        if result.transport_bytes > MAX_TRANSPORT_BYTES:
            raise CollectionIncomplete
        try:
            payload = json.loads(fetched.body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RestSchemaChanged("response_not_json") from exc
        for record, moment in map_page(payload, config, floor):
            result.records.append(record)
            batch_bytes += len(json.dumps(record, ensure_ascii=False))
            if moment is not None:
                times.append(moment)
        if len(result.records) > MAX_RECORDS or batch_bytes > MAX_BATCH_BYTES:
            raise CollectionIncomplete
        next_url = payload.get("next_url") if isinstance(payload, dict) else None
        if next_url in (None, ""):
            url = None
            continue
        if not isinstance(next_url, str):
            raise RestSchemaChanged("next_url_invalid")
        url = urljoin(url, next_url)
        if _origin(url) != origin:
            raise UnsafeDestination("cross_origin_pagination")
    result.cursor_after = max(times).isoformat() if times else cursor
    return result


__all__ = [
    "MAX_PAGES", "MAX_RECORDS", "CollectionIncomplete", "Fetched", "ProviderHttpError",
    "RestCollection", "RestSchemaChanged", "UnsafeDestination", "collect_rest", "fetch_bounded", "map_page",
    "resolve_public",
]
