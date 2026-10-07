"""Egress client for chat web search (Tavily/Brave). Sends only the literal user query.

Security contract (p15 design W1 + review P2-4/P3-5/P3-6): key only in a header, never logged;
no redirects/retries/proxy; 8 s hard timeout; 256 KiB decoded-body cap; JSON content-type only;
every result string is sanitized before it can reach a prompt or storage.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.model_gateway.schemas import AIExecutionConfig

logger = logging.getLogger("bbd.chat.web_search")

WEB_QUERY_MAX_CHARS = 400
WEB_SEARCH_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESULTS = 5
TITLE_MAX = 200
SNIPPET_MAX = 500
URL_MAX = 2048

FailureCode = Literal["query_too_long", "timeout", "provider_error"]
_TAG = re.compile(r"<[^>]*>")
_BAD_URL_CHARS = re.compile(r"[\s<>\"'`]")


class WebSearchError(Exception):
    """Truthful failure type; the message is only the code, never a URL, query or key."""

    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str  # server-side only (citation); prompts get ``host``
    host: str
    snippet: str


def suppress_httpx_request_logging() -> None:
    """httpx logs full request URLs (Brave's ``?q=``) at INFO; keep them out of logs."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def normalize_query(message: str) -> str:
    """Literal query: strip + whitespace collapse. Over-long or empty input is refused, never truncated."""
    query = " ".join(message.split())
    if not query or len(query) > WEB_QUERY_MAX_CHARS:
        raise WebSearchError("query_too_long")
    return query


def sanitize_text(text: str, limit: int) -> str:
    """unescape, strip tags, drop Cc/Cf, <>-> single guillemets, []-> (), collapse, truncate (review P2-4)."""
    text = _TAG.sub("", html.unescape(text))
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))
    text = text.translate({0x3C: "‹", 0x3E: "›", 0x5B: "(", 0x5D: ")"})
    return " ".join(text.split())[:limit]


def sanitize_url(raw: str) -> tuple[str, str] | None:
    """Return (url without fragment, host) or None when the URL must be dropped."""
    if len(raw) > URL_MAX or _BAD_URL_CHARS.search(raw) or any(unicodedata.category(c) in ("Cc", "Cf") for c in raw):
        return None
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        _ = parts.port  # validates the port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    return urlunsplit(parts._replace(fragment="")), host


def _result(title: Any, url: Any, snippet: Any) -> WebSearchResult | None:
    if not (isinstance(title, str) and isinstance(url, str) and isinstance(snippet, str)):
        return None
    checked = sanitize_url(url)
    clean_title = sanitize_text(title, TITLE_MAX)
    if checked is None or not clean_title:
        return None
    return WebSearchResult(clean_title, checked[0], checked[1], sanitize_text(snippet, SNIPPET_MAX))


def parse_results(provider: str, payload: Any) -> list[WebSearchResult]:
    """Strict parse; wrong container shape -> provider_error, malformed items are dropped."""
    items: Any
    if provider == "tavily":
        items, snippet_key = (payload.get("results") if isinstance(payload, dict) else None), "content"
    else:
        web = payload.get("web") if isinstance(payload, dict) else None
        items, snippet_key = (web.get("results") if isinstance(web, dict) else None), "description"
    if not isinstance(items, list):
        raise WebSearchError("provider_error")
    parsed = (_result(i.get("title"), i.get("url"), i.get(snippet_key)) for i in items if isinstance(i, dict))
    return [r for r in parsed if r is not None][:MAX_RESULTS]


def web_search_permitted(config: AIExecutionConfig) -> bool:
    """Config/consent gate. Callers must evaluate it inside the fence (review P1-1)."""
    return (
        config.web_search_provider in ("tavily", "brave")
        and bool(config.web_search_endpoint)
        and config.web_search_credential_configured
        and bool(config.web_search_api_key)
        and config.privacy.allow_remote_web_search
    )


async def _fetch(
    provider: str, endpoint: str, api_key: str, query: str, transport: httpx.AsyncBaseTransport | None
) -> Any:
    base = endpoint.rstrip("/")
    if provider == "tavily":
        request = httpx.Request(
            "POST", f"{base}/search", headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": MAX_RESULTS, "search_depth": "basic",
                  "include_answer": False, "include_raw_content": False, "include_images": False},
        )
    else:
        request = httpx.Request(
            "GET", f"{base}/res/v1/web/search", headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            params={"q": query, "count": MAX_RESULTS, "safesearch": "moderate", "text_decorations": 0},
        )
    timeout = httpx.Timeout(connect=3, read=6, write=3, pool=1)
    body = bytearray()
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.send(request, stream=True)
        try:
            content_type = response.headers.get("content-type", "").lower()
            if not response.is_success or not content_type.startswith("application/json"):
                raise WebSearchError("provider_error")
            async for chunk in response.aiter_bytes():  # decoded bytes: defeats gzip bombs
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise WebSearchError("provider_error")
        finally:
            await response.aclose()
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise WebSearchError("provider_error") from exc


async def search(
    provider: str, endpoint: str, api_key: str, message: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[WebSearchResult]:
    """One provider call for the literal ``message``. Raises only WebSearchError (or CancelledError)."""
    query = normalize_query(message)
    if provider not in ("tavily", "brave"):
        raise WebSearchError("provider_error")
    suppress_httpx_request_logging()
    loop = asyncio.get_running_loop()
    started, code = loop.time(), "ok"
    try:
        async with asyncio.timeout(WEB_SEARCH_TIMEOUT_SECONDS):
            return parse_results(provider, await _fetch(provider, endpoint, api_key, query, transport))
    except WebSearchError as exc:
        code = exc.code
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        code = "timeout"
        raise WebSearchError("timeout") from exc
    except httpx.HTTPError as exc:  # never log str(exc): it embeds the URL/query
        code = f"provider_error:{type(exc).__name__}"
        raise WebSearchError("provider_error") from exc
    finally:
        logger.info("web_search provider=%s outcome=%s ms=%d", provider, code, int((loop.time() - started) * 1000))


async def consume_daily_quota(redis: Redis, owner_id: object, limit: int, now: datetime | None = None) -> bool:
    """Increment before egress (failures count). Limit 0 disables; a Redis error fails closed."""
    if limit <= 0:
        return False
    key = f"chat:websearch:{owner_id}:{(now or datetime.now(UTC)).date().isoformat()}"
    try:
        pipeline = redis.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, 172800)
        count = (await pipeline.execute())[0]
    except RedisError:
        return False
    return bool(count <= limit)
