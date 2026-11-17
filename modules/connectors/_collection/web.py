"""REST, RSS, static web and MCP collection adapters.

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urldefrag, urljoin

from core.heavy_work import to_thread_joined
from modules.connectors._collection.base import (
    CollectionFenceLost,
    Run,
    TermsIneligible,
    Walk,
    _accept_generic,
    _accept_walk,
    _conditional,
    _generic_update,
    _require_body,
    _rest_secret,
    _state,
    _with_header,
)
from modules.connectors.providers import rest
from modules.ingestion import public as ingestion


async def _run_rest(run: Run) -> None:
    """Collect one generic REST source: gated fetch (optional owner header), checkpoint resume, atomic receipt."""
    from modules.connectors.registry import configuration

    a = run.attempt
    fetch: Callable[..., Awaitable[rest.Fetched]] = run.gate.fetch_bytes
    if a.authenticated:
        fetch = _with_header(run.gate.fetch_bytes, *await _rest_secret(run))
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state,
        revision=a.connector_revision,
        cursor=state.cursor,
        origin_url=str(config.url),
    )
    collected = await rest.collect_rest(
        config,
        state.cursor,
        fetch=fetch,
        resume=resume,
        conditional=None if resume else _conditional(state, a.connector_revision),
    )
    await _accept_walk(
        run,
        state,
        Walk(
            collected.records,
            collected.continuation_url,
            collected.max_time,
            collected.etag,
            collected.last_modified,
            collected.not_modified,
        ),
        resume,
    )


class _NotModified(Exception):  # control-flow signal for HTTP 304
    """The first feed page answered 304."""


async def _run_rss(run: Run) -> None:
    """Generic RSS/Atom collection: bounded walk, checkpoint resume, then a walk commit.

    A page with more than the 500-record cap degrades (cap taken, run stats flag ``truncated``) rather than failing.
    """
    from modules.connectors.n8n import read_rss
    from modules.connectors.registry import configuration

    a = run.attempt
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state,
        revision=a.connector_revision,
        cursor=state.cursor,
        origin_url=None,
    )
    conditional = None if resume else _conditional(state, a.connector_revision)
    stats: dict[str, Any] = {}
    first: dict[str, Any] = {"pending": True}

    async def fetch(url: str) -> bytes:
        """Fetch one feed page, adding conditional headers on the first page and remembering its validators; 304 raises ``_NotModified``."""
        headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
        is_first, first["pending"] = first["pending"], False
        if is_first and conditional:
            headers.update(conditional)
        fetched = await run.gate.fetch_bytes(url, headers=headers)
        if is_first and resume is None:
            first["etag"], first["lm"] = fetched.etag, fetched.last_modified
        if fetched.body is None:
            raise _NotModified
        return fetched.body

    try:
        result = await read_rss(
            resume.url if resume else str(config.feed_url), state.cursor, fetch=fetch, stats=stats
        )
    except _NotModified:
        await _accept_walk(run, state, Walk([], None, None, None, None, not_modified=True), resume)
        return
    if stats.get("truncated"):
        # Unread items remain inside a page and no exact continuation exists. Failing here would repeat forever on
        # the same oversized page, so take the 500 read and advance the cursor only to the newest taken (max_time
        # below); the rest is reported and drains on later syncs when it is newer than that cursor.
        run.extras["selection"] = {"backlog": 0, "truncated": True}
    skip = set(resume.ids) if resume else set()
    seen: set[str] = set()
    records = []
    for record in result["records"]:  # type: ignore[attr-defined]
        identifier = str(record["provider_id"])
        if rest.id_hash(identifier) in skip or identifier in seen:
            continue
        seen.add(identifier)
        records.append(record)
    newest = str(result["cursor_after"]) if result["cursor_after"] is not None else None
    max_time = rest.later(
        resume.max_time if resume else None, newest if newest != state.cursor else None
    )
    await _accept_walk(
        run,
        state,
        Walk(
            records,
            stats.get("resume_url"),
            max_time,
            resume.etag if resume else first.get("etag"),
            resume.last_modified if resume else first.get("lm"),
        ),
        resume,
    )


# ---------------------------------------------------------------- native web + MCP (generic sources)


class _PageText(HTMLParser):
    """Visible text and anchors of one HTML page (stdlib; no optional crawl extra needed)."""

    def __init__(self) -> None:
        """Initialise the visible-text and link accumulators."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track skipped-element depth for script/style/noscript and collect anchor hrefs."""
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag: str) -> None:
        """Leave a skipped element when its closing tag arrives."""
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        """Append non-blank text that is outside skipped elements."""
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _decode_page(body: bytes, content_type: str | None) -> str:
    """Decode a fetched page using the ``Content-Type`` charset; unknown/absent/invalid charsets fall back to UTF-8.

    Undecodable bytes are replaced, never raised, so one badly encoded page cannot fail the crawl.
    """
    charset = "utf-8"
    for param in (content_type or "").split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _parse_page(html: str) -> tuple[list[str], list[str]]:
    """Return the visible text parts and anchor hrefs of one HTML page.

    Pure CPU work (stdlib HTMLParser on up to ~10 MB), so callers run it through
    ``to_thread_joined``; it touches no shared state and raises whatever the parser raises.
    """
    page = _PageText()
    page.feed(html)
    return page.parts, page.links


async def _run_web(run: Run) -> None:
    """Collect static web pages through bounded HTTP or queue a fenced browser-worker crawl.

    Browser mode hands its existing scheduler admission to the durable ingestion
    crawl event; the worker later owns the bounded browser request and receipt. In bounded-HTTP mode a
    redirect is recorded under the final URL (also the base for relative links), and a page whose final
    origin differs from the configured origin is skipped.
    """
    from modules.connectors.registry import configuration, normalize

    config = configuration(run.attempt.source)
    start = str(config.url)
    state = await _state(run)
    if config.js_render:
        if not run.settings.browser_shared_token.get_secret_value():
            raise rest.ProviderHttpError(503)
        a = run.attempt
        async with run.factory() as session:
            await ingestion.queue_connector_crawl(
                session,
                a.source.id,
                a.source.generation,
                a.connector_revision,
                state.cursor,
                {
                    "url": start,
                    "mode": "playwright",
                    "max_pages": config.max_pages,
                    "max_depth": config.max_depth,
                    "timeout_seconds": config.timeout_seconds,
                },
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                request_ref=a.ref,
            )
        return
    origin = rest._origin(start)
    queue, visited = [(start, 0)], {start}
    records: list[dict[str, Any]] = []
    spent = 0
    while queue and len(records) < config.max_pages:
        url, depth = queue.pop(0)
        fetched = await run.gate.fetch_bytes(
            url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            max_bytes=min(rest.MAX_PAGE_BYTES, rest.MAX_TRANSPORT_BYTES - spent),
            timeout=config.timeout_seconds,
        )
        body = _require_body(fetched)
        spent += len(body)
        # Provenance and relative links follow the page that actually answered, not the requested URL.
        page_url = urldefrag(fetched.final_url)[0] if fetched.final_url else url
        if depth == 0:
            # The start URL may legitimately redirect (apex->www, http->https); every hop was already
            # SSRF-checked by the gate, so the answering origin becomes the crawl origin.
            origin = rest._origin(page_url)
            visited.add(page_url)  # a linked redirect target must not be fetched a second time
        elif rest._origin(page_url) != origin:
            continue  # a deeper redirect left the crawl origin: never ingest or crawl from it
        # HTMLParser.feed on a multi-MB page would stall the worker loop (heartbeats, heavy-slot renewal).
        parts, links = await to_thread_joined(_parse_page, _decode_page(body, fetched.content_type))
        records.append(
            normalize(
                {
                    "provider_id": page_url,
                    "content": "\n".join(parts)[:200_000],
                    "observed_at": datetime.now(UTC).isoformat(),
                    "metadata": {"url": page_url},
                }
            )
        )
        if depth < config.max_depth:
            for href in links:
                target = urldefrag(urljoin(page_url, href))[0]
                if target not in visited and len(queue) < 100 and rest._origin(target) == origin:
                    visited.add(target)
                    queue.append((target, depth + 1))
    await _accept_generic(run, records, state.cursor, state.cursor)


async def _run_mcp(run: Run) -> None:
    """MCP collection: each reviewed call is authorized by the gate, then ingested through the fenced receipt."""
    from modules.connectors import mcp as mcp_collection
    from modules.tools import public as tools

    a = run.attempt
    runtime = run.ctx.get("agent_mcp_runtime")
    if runtime is None:
        raise mcp_collection.McpCollectionError("mcp_runtime_unavailable")
    config = mcp_collection.validate(a.source)
    state = await _state(run)
    collected_at = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    failed = 0
    # mcp_collection.authorized swallows exceptions from authorize_extra; remember lost authority here
    # so a fence loss fails closed instead of counting as a retryable per-call failure.
    lost: CollectionFenceLost | TermsIneligible | TimeoutError | None = None

    async def authorized() -> bool:
        """Per-call authorization hook: run the gate, remember the first lost-authority exception and re-raise it."""
        nonlocal lost
        try:
            await (
                run.gate()
            )  # raises CollectionFenceLost / TermsIneligible / TimeoutError; never returns False
        except (CollectionFenceLost, TermsIneligible, TimeoutError) as exc:
            lost = exc
            raise
        return True

    for call in config.calls:
        try:
            read = await tools.read_collection_capability(
                cast(Any, runtime),
                connection_id=config.connection_id,
                grant_id=call.grant_id,
                source_id=a.source.id,
                source_generation=a.source.generation,
                arguments=call.arguments,
                authorize_extra=authorized,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )
            records.extend(mcp_collection.normalize(read, call.arguments, collected_at))
        except (CollectionFenceLost, TermsIneligible, TimeoutError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001  # one failing call never blocks the others (as in mcp.collect)
            if lost is not None:
                raise lost from None
            failed += 1
    if failed == len(config.calls):
        raise mcp_collection.McpCollectionError("mcp_collection_failed")
    kept = records[: mcp_collection.MAX_RECORDS]
    # The generic accept path carries no coverage, so the dropped tail and failed calls are reported in receipt stats.
    selection = {"backlog": len(records) - len(kept), "failed_calls": failed}
    await _accept_generic(
        run,
        kept,
        state.cursor,
        "mcp:" + collected_at.isoformat(),
        _generic_update(None, selection, len(kept)),
    )


# ---------------------------------------------------------------- simple native providers
