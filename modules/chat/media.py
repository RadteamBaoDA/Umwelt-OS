"""Opt-in remote media for Generative UI widgets: Wikipedia/Commons trip images and a signed media proxy.

Everything here is egress to a fixed, tiny host allowlist, gated by the owner privacy flag
``allow_widget_remote_media`` (default off) and refused on ``local_only`` turns. Callers pass the transport
factory so production uses the approved-endpoint transport (DNS/CIDR pinned, public addresses only, no
redirects) and tests use ``httpx.MockTransport``. Nothing is logged with URLs, tokens or response bodies.

Tile usage: ``GET /api/v1/media/tile/{z}/{x}/{y}`` (authenticated, flag-gated) returns a USGS Topo tile; a
map widget must show ``TILE_ATTRIBUTION``. Images: ``GET /api/v1/media/proxy?token=...`` with a token issued by
``sign_media_url`` (the ``get_trip_stop_images`` tool already returns proxied URLs).
"""

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel

from core.media_proxy import (  # noqa: F401 - re-exported: the proxy helpers moved to core
    _CTRL,
    PROXY_HOSTS,
    PROXY_PATH,
    SEARCH_THUMB_HOST,
    TOKEN_TTL_SECONDS,
    MediaError,
    check_upstream_url,
    media_permitted,
    sign_media_url,
    verify_media_token,
)
from core.model_gateway.schemas import AIExecutionConfig
from core.model_gateway.transport import EndpointNetworkPolicyError, approved_transport

USER_AGENT = "Umwelt-OS"
TILE_HOST = "basemap.nationalmap.gov"
TILE_PATH = "/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
TILE_ATTRIBUTION = "Tiles courtesy of the U.S. Geological Survey (USGS), The National Map"
MAX_MEDIA_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
LOOKUP_TIMEOUT_SECONDS = 12.0
LOOKUP_WORKERS = 4
MAX_TITLES = 8
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

TransportFactory = Callable[[str], httpx.AsyncBaseTransport]

# CC-BY / CC-BY-SA / CC0 / public domain only; NC and ND variants are not reusable in a generated widget.
_LICENSE_OK = re.compile(r"^(cc0|cc[ -]by([ -]sa)?\b|public domain|pd\b)", re.IGNORECASE)
_LICENSE_BAD = re.compile(r"\b(nc|nd)\b|non-?commercial|no[ -]?deriv", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]*>")


class TripStopImagesArgs(BaseModel):
    """Arguments of the non-terminal ``get_trip_stop_images`` tool."""

    titles: list[str]


def default_transport_factory(host: str) -> httpx.AsyncBaseTransport:
    """Approved-endpoint transport pinned to ``https://<host>``: public addresses only, no proxy, no retry."""
    return approved_transport(f"https://{host}", (), allow_global=True)


def tile_url(z: int, x: int, y: int) -> str:
    """USGS Topo tile URL for a valid slippy-map coordinate; MediaError on out-of-range input."""
    if not (0 <= z <= 19 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise MediaError("bad_tile")
    return f"https://{TILE_HOST}" + TILE_PATH.format(z=z, x=x, y=y)


async def _stream(url: str, factory: TransportFactory, limit: int, kind: str) -> tuple[bytes, str]:
    """GET ``url`` once (no redirects), enforcing 2xx, content type for ``kind`` and a decoded-byte cap.

    ``kind`` is ``"image"`` (any ``image/*`` except SVG, which can carry script) or ``"json"``.
    """
    host = urlsplit(url).hostname or ""
    body = bytearray()
    accept = "image/*" if kind == "image" else "application/json"
    async with httpx.AsyncClient(
        transport=factory(host),
        timeout=httpx.Timeout(LOOKUP_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request = client.build_request(
            "GET", url, headers={"User-Agent": USER_AGENT, "Accept": accept}
        )
        response = await client.send(request, stream=True)
        try:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind == "image":
                ok_type = content_type.startswith("image/") and content_type != "image/svg+xml"
            else:
                ok_type = content_type == "application/json"
            if not response.is_success or not ok_type:
                raise MediaError("upstream_rejected")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise MediaError("too_large")
            async for chunk in response.aiter_bytes():  # decoded bytes: defeats compression bombs
                body += chunk
                if len(body) > limit:
                    raise MediaError("too_large")
        finally:
            await response.aclose()
    return bytes(body), content_type


async def fetch_media(
    url: str, factory: TransportFactory = default_transport_factory
) -> tuple[bytes, str]:
    """Fetch an allowlisted image (<= 5 MB, non-SVG ``image/*``, no redirects); returns (bytes, content-type)."""
    try:
        return await _stream(check_upstream_url(url), factory, MAX_MEDIA_BYTES, "image")
    except (httpx.HTTPError, EndpointNetworkPolicyError):
        raise MediaError("upstream_error") from None


async def _json(url: str, factory: TransportFactory) -> Any:
    """GET small JSON (<= 1 MB) from a fixed Wikimedia host; MediaError on any failure."""
    try:
        data, _ = await _stream(url, factory, MAX_JSON_BYTES, "json")
        return json.loads(data)
    except (httpx.HTTPError, EndpointNetworkPolicyError, ValueError):
        raise MediaError("upstream_error") from None


def _commons_name(image_url: str) -> str:
    """File name on Commons from an upload.wikimedia.org ``/wikipedia/commons/...`` (thumb or original) URL."""
    parts = urlsplit(image_url)
    segments = parts.path.split("/")
    if (
        parts.hostname != "upload.wikimedia.org"
        or len(segments) < 5
        or segments[1:3] != ["wikipedia", "commons"]
    ):
        raise MediaError(
            "not_commons"
        )  # local en.wikipedia files carry no machine-checked licence here
    name = segments[-2] if segments[3] == "thumb" else segments[-1]
    return unquote(name)


def _clean(value: Any) -> str:
    """Strip HTML from an extmetadata value and collapse whitespace."""
    raw = value.get("value") if isinstance(value, dict) else value
    return " ".join(_TAGS.sub(" ", str(raw or "")).split())[:200]


async def _lookup(
    title: str, secret: str, factory: TransportFactory, subject: str = ""
) -> dict[str, str]:
    """Resolve one article title to a licensed, credited, proxied image; raises MediaError when unusable."""
    summary = await _json(WIKI_SUMMARY + quote(title.replace(" ", "_"), safe=""), factory)
    if not isinstance(summary, dict):
        raise MediaError("upstream_error")
    chosen = summary.get("thumbnail") or summary.get("originalimage")
    source = chosen.get("source") if isinstance(chosen, dict) else None
    if not isinstance(source, str):
        raise MediaError("no_image")
    check_upstream_url(source)
    name = _commons_name(source)
    query = (
        f"{COMMONS_API}?action=query&format=json&formatversion=2&prop=imageinfo&iiprop=extmetadata%7Curl"
        "&titles=" + quote("File:" + name, safe="")
    )
    result = await _json(query, factory)
    pages = result.get("query", {}).get("pages", []) if isinstance(result, dict) else []
    info = (pages[0].get("imageinfo") or [{}])[0] if pages and isinstance(pages[0], dict) else {}
    meta = info.get("extmetadata") or {}
    licence = _clean(meta.get("LicenseShortName"))
    credit = _clean(meta.get("Artist")) or _clean(meta.get("Credit"))
    if not licence or not credit or not _LICENSE_OK.search(licence) or _LICENSE_BAD.search(licence):
        raise MediaError("license_rejected")
    article = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page")
    if not (isinstance(article, str) and urlsplit(article).hostname == "en.wikipedia.org"):
        article = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")
    return {
        "title": title,
        "status": "ok",
        "image_url": f"{PROXY_PATH}?token={sign_media_url(secret, source, subject=subject)}",
        "credit": credit,
        "license": licence,
        "article_url": article,
    }


async def get_trip_stop_images(
    titles: list[str],
    *,
    secret: str,
    factory: TransportFactory = default_transport_factory,
    subject: str = "",
) -> list[dict[str, str]]:
    """Look up 1-8 English Wikipedia titles concurrently (4 workers, 12 s each, no redirects).

    Returns one dict per input title, in order: ``{title, status:"ok", image_url, credit, license, article_url}``
    or ``{title, status:"unavailable"}``. Per-title failures never raise; image URLs are same-origin proxy URLs.
    """
    clean = [t.strip() for t in titles if isinstance(t, str) and t.strip()][:MAX_TITLES]
    gate = asyncio.Semaphore(LOOKUP_WORKERS)

    async def one(title: str) -> dict[str, str]:
        """Bounded single lookup mapping every failure to ``unavailable``."""
        async with gate:
            try:
                async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
                    return await _lookup(title[:200], secret, factory, subject)
            except (MediaError, TimeoutError):
                return {"title": title, "status": "unavailable"}

    return list(await asyncio.gather(*(one(t) for t in clean)))


async def run_trip_images_tool(
    raw_arguments: str,
    *,
    config: AIExecutionConfig,
    local_only: bool,
    secret: str,
    factory: TransportFactory = default_transport_factory,
    subject: str = "",
) -> str:
    """Execute the ``get_trip_stop_images`` tool call and return the JSON string for the tool message.

    Refuses (``{"error": "disabled"}``) when the privacy flag is off or the turn is local-only, and
    ``{"error": "invalid_arguments"}`` for malformed input; never raises for model-controlled input.
    """
    if not media_permitted(config, local_only=local_only):
        return json.dumps({"error": "disabled"})
    try:
        args = TripStopImagesArgs.model_validate_json(raw_arguments or "{}")
        if not 1 <= len(args.titles) <= MAX_TITLES:
            raise ValueError("titles")
    except ValueError:
        return json.dumps({"error": "invalid_arguments"})
    return json.dumps(
        {
            "stops": await get_trip_stop_images(
                args.titles, secret=secret, factory=factory, subject=subject
            )
        }
    )
