"""Free/pure/keyed provider adapters (world data, jobs, feeds, HN, GDELT, SEC, OpenAlex).

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from modules.connectors import provider_options, quota
from modules.connectors._collection.base import (
    ADAPTERS,
    KEYED_PROVIDERS,
    NATIVE_REST_PLACEMENTS,
    PURE_MAX_BYTES,
    PURE_PROVIDERS,
    CredentialMissing,
    Run,
    _accept_native,
    _conditional,
    _coverage,
    _get_body,
    _lease,
    _payload,
    _payload_async,
    _require_body,
    _rest_secret,
    _resume_cursor,
    _select,
    _state,
)
from modules.connectors.provider_specs import INCREMENTAL_PROVIDERS
from modules.connectors.providers import news as _news
from modules.connectors.providers import rest
from modules.connectors.providers.keyed import KEYED, KEYED_MAX_BYTES
from modules.ingestion import public as ingestion
from modules.ingestion.public import (
    CollectionStateUpdate,
    IngestionRecord,
)


async def _run_pure(run: Run) -> None:
    """P3 measurement providers: one fixed request, mapped by ``map_pure_provider_body`` (no world_data for news)."""
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.world_data import PURE_ADAPTERS, map_pure_provider_body

    provider = str(run.attempt.source.provider)
    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    cursor = await _resume_cursor(run, lease)
    request = PURE_ADAPTERS[provider].request()
    if (
        url := provider_options.build_url(provider, cfg, cursor=cursor, now=collected_at)
    ) is not None:
        request = replace(
            request, url=url
        )  # owner options / native resume; headers stay the adapter's
    fetched = await _get_body(run, request, max_bytes=PURE_MAX_BYTES.get(provider, MAX_BODY_BYTES))
    records = await _payload_async(
        lambda *a: map_pure_provider_body(*a, **provider_options.map_options(provider, cfg)),
        provider,
        _require_body(fetched),
        collected_at,
    )  # mapper scope follows the requested scope
    records = provider_options.filter_records(
        provider, cfg, records
    )  # region / KEV / NVD / FX / Kp (client)
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_coinpaprika(run: Run) -> None:
    """CoinPaprika tickers: one gated, debited GET per selected coin (<= 5), spaced past the 1 call/second pilot cap.

    The pacing matters: a quota deferral restarts the whole run, so back-to-back calls inside one second would
    defer forever. Any call failing fails the run (a partial coin set would silently drop a series). The
    ``coin_ids`` order is the curated order, so equal selections send equal request sequences.
    """
    from modules.connectors.providers.crypto import map_coinpaprika
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json
    from modules.connectors.providers.world_data import PURE_ADAPTERS

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    records: list[IngestionRecord] = []
    for index, (coin, url) in enumerate(
        zip(provider_options.selected_coins(cfg), provider_options.coin_urls(cfg), strict=True)
    ):
        if index:
            await asyncio.sleep(provider_options.COINPAPRIKA_GAP_SECONDS)
        fetched = await _get_body(
            run, replace(PURE_ADAPTERS["coinpaprika"].request(), url=url), max_bytes=MAX_BODY_BYTES
        )
        records += await _payload_async(
            lambda body, coin=coin: map_coinpaprika(decode_json(body), collected_at, coin_id=coin),
            _require_body(fetched),
        )
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_vn_stock(run: Run) -> None:
    """VN stocks/indices: one gated, debited GET per non-empty group (stocks, indices), so at most two per run.

    The VNDirect finfo API serves stocks and indices from different endpoints. Any failing call fails the run
    (a partial symbol set would silently drop a series); calls are spaced one second apart to stay polite to an
    unofficial endpoint. Symbol order is the owner's, so equal selections send equal request sequences.
    """
    from modules.connectors.providers import vn_stock
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    records: list[IngestionRecord] = []
    requests = _payload(vn_stock.vn_stock_requests, provider_options.selected_vn_symbols(cfg))
    for index, (group, request) in enumerate(requests):
        if index:
            await asyncio.sleep(1.0)
        fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
        records += await _payload_async(
            lambda body, group=group: vn_stock.map_vn_stock(
                decode_json(body), collected_at, symbols=group
            ),
            _require_body(fetched),
        )
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_vietnamworks(run: Run) -> None:
    """VietnamWorks jobs: one gated, debited POST search per owner keyword (<= 5), spaced one second apart.

    Results are ranked by relevance, not time, so the provider is dedup-only (no cursor floor). City, level and
    salary filters are applied while mapping; per-keyword results are merged by job id. Any failing call fails the
    run so a partial keyword set never silently drops a search.
    """
    from modules.connectors.providers import jobs
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    batches: list[list[IngestionRecord]] = []
    for index, request in enumerate(
        _payload(jobs.vietnamworks_requests, provider_options.selected_job_keywords(cfg))
    ):
        if index:
            await asyncio.sleep(1.0)
        fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
        batches.append(
            await _payload_async(
                lambda body: jobs.map_vietnamworks(
                    decode_json(body),
                    collected_at,
                    cities=cfg.get("job_cities") or (),
                    levels=cfg.get("job_levels") or (),
                    min_salary_usd=cfg.get("job_min_salary_usd"),
                ),
                _require_body(fetched),
            )
        )
    records, cursor_after = await _select(
        run, lease, jobs.merge_keyword_records(batches), collected_at
    )
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_itviec(run: Run) -> None:
    """ITviec jobs: <= 3 gated search pages, then <= 10 gated posting pages, every request >= 2 s apart.

    Owner-approved static crawl (docs/connectors/free-sources.md): plain HTTP GET, JSON-LD only. Search pages yield
    vetted posting URLs; only URLs this source has not stored yet are fetched, rotated by a 6-hour slot so
    postings rejected by the salary/level filters (never stored, so never "seen") cannot starve newer ones. A
    posting that is gone (404/410) is skipped; any other failing request fails the run, as for VietnamWorks.
    """
    from modules.connectors.providers import jobs
    from modules.connectors.providers.macro import MAX_BODY_BYTES

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    source = run.attempt.source
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    searches = _payload(
        jobs.itviec_search_requests,
        provider_options.selected_job_keywords(cfg),
        cfg.get("job_cities") or (),
    )
    urls: dict[str, None] = {}
    first = True

    async def page(request: Any) -> str:
        """Fetch one listing page (spacing requests after the first) and return its decoded text."""
        nonlocal first
        if not first:
            await asyncio.sleep(jobs.ITVIEC_REQUEST_GAP_SECONDS)
        first = False
        return _require_body(await _get_body(run, request, max_bytes=MAX_BODY_BYTES)).decode(
            "utf-8", errors="replace"
        )

    for request in searches:
        urls.update(dict.fromkeys(jobs.parse_itviec_item_list(await page(request))))
    async with run.factory() as session:
        seen = await ingestion.seen_provider_ids(
            session,
            source_id=source.id,
            source_generation=source.generation,
            provider_ids=[f"itviec:{u.rsplit('/', 1)[1]}" for u in urls],
        )
        await session.rollback()
    fresh = [u for u in urls if f"itviec:{u.rsplit('/', 1)[1]}" not in seen]
    if fresh:
        shift = int(collected_at.timestamp() // 21600) % len(fresh)
        fresh = fresh[shift:] + fresh[:shift]
    pages: list[tuple[str, str]] = []
    for url in fresh[: jobs.ITVIEC_MAX_DETAIL_FETCHES]:
        try:
            pages.append((url, await page(jobs.itviec_detail_request(url))))
        except rest.ProviderHttpError as exc:
            if exc.status_code not in {404, 410}:
                raise
    records = await _payload_async(
        lambda: jobs.map_itviec(
            pages,
            collected_at,
            levels=cfg.get("job_levels") or (),
            min_salary_usd=cfg.get("job_min_salary_usd"),
        )
    )
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _keyed_secret(run: Run) -> str:
    """Owner key for this exact generation/revision, read at send time; typed ``credential_missing`` if unusable.

    The stored ``header_name`` must equal the provider's fixed placement (header name, ``query:<param>`` or
    ``path:<token>``), so a credential saved for another placement is never replayed here.
    """
    placement = KEYED_PROVIDERS[str(run.attempt.source.provider)]
    try:
        stored_placement, secret = await _rest_secret(run)
    except rest.ProviderHttpError as exc:
        raise CredentialMissing from exc
    if stored_placement != placement:
        raise CredentialMissing
    run.gate.bind_credential(
        quota.credential_fingerprint(
            secret,
            deployment_key=run.settings.connector_credential_encryption_key.get_secret_value(),
        )
    )
    return secret


async def _run_keyed(run: Run) -> None:
    """Keyed free-tier providers: key read first (no lease and no send without it), one gated send.

    Order matters: the credential and the request (which validates key shape and owner scope) are resolved
    before the lease so a missing key or bad scope fails closed without consuming quota or a lease.
    Incremental keyed providers resume from the leased cursor, map one response, select accepted records, and
    pass the selected records and cursor to ingestion together; the ingestion transaction accepts both or
    neither. Media snapshot providers use seen-key deduplication without a cursor floor so late results remain
    eligible. TMDB changes separately checkpoints its fixed window and next unread page, still with one send.
    """
    from modules.connectors.providers.macro import decode_json
    from modules.connectors.providers.media_movies_tv import (
        build_tmdb_request,
        tmdb_changes_cursor,
        tmdb_changes_window,
    )

    provider = str(run.attempt.source.provider)
    row = KEYED[provider]
    cfg = getattr(run.attempt.source, "configuration", None) or {}
    secret = await _keyed_secret(run)
    request = _payload(row.build, secret, cfg)
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    cursor = await _resume_cursor(run, lease) if provider in INCREMENTAL_PROVIDERS else None
    window = None
    if provider == "tmdb_movies" and cfg.get("tmdb_mode") == "changes":
        window = tmdb_changes_window(cursor, collected_at)
        request = build_tmdb_request(provider, secret, cfg, changes_window=window)
    fetched = await _get_body(run, request, max_bytes=KEYED_MAX_BYTES)
    body = _require_body(fetched)
    payload = body if row.binary else (await _payload_async(decode_json, body))
    records = await _payload_async(row.mapper, payload, collected_at, cfg)
    if provider in INCREMENTAL_PROVIDERS:
        records, cursor_after = await _select(run, lease, records, collected_at)
        if window is not None:
            backlog = bool(run.extras.get("selection", {}).get("backlog"))
            cursor_after = _payload(lambda: tmdb_changes_cursor(payload, window, backlog=backlog))
        await _accept_native(
            run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
        )
    else:
        await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_coingecko(run: Run) -> None:
    """CoinGecko Demo: the first keyed preset; kept as a named entry point for its executor wiring and tests."""
    await _run_keyed(run)


async def _fetch_sections(
    run: Run,
    provider: str,
    sections: list[tuple[str, str]],
    revision: str,
    collected_at: datetime,
) -> tuple[list[IngestionRecord], list[str]]:
    """GET every selected section unconditionally, merge by stable item id, and report failed slugs.

    Each GET is debited by the gate. Records of the same ``provider_id`` seen in several sections collapse to one
    (lowest ``version``, as ``map_feed`` does for in-feed duplicates); the section slug is never written to a
    record, so the hashed metadata equals a single-section fetch of the same article. A section failing with an
    HTTP/parse/scope error is skipped and reported; fence loss, timeouts and rate limits propagate. When every
    section fails the first error is re-raised, so the run fails exactly as a single feed would.
    """
    merged: dict[str, IngestionRecord] = {}
    failed: list[str] = []
    first_error: Exception | None = None
    for slug, url in sections:
        try:
            fetched = await _get_body(
                run,
                _news.feed_request(provider, None, revision, url),
                max_bytes=_news.MAX_FEED_BYTES,
            )
            result = await asyncio.to_thread(
                _payload,
                _news.map_feed_response,
                provider,
                200,
                _require_body(fetched),
                collected_at,
            )
        except (rest.ProviderHttpError, rest.CollectionIncomplete, ValueError) as exc:
            failed.append(slug)
            first_error = first_error or exc
            continue
        for record in result.records:
            old = merged.get(record.provider_id)
            if old is None or (record.version or "") < (old.version or ""):
                merged[record.provider_id] = record
    if not merged and first_error is not None:
        raise first_error
    return list(merged.values()), failed


async def _run_feed(run: Run) -> None:
    """P2 RSS/Atom presets: replay stored validators (same URL + revision), 304 keeps the last good records.

    A catalog preset with several selected sections fetches each one unconditionally (validators are single-URL),
    merges and dedups them, then runs the usual selection; failed section slugs are reported in the run stats.
    """
    from modules.connectors.providers import news

    a, provider = run.attempt, str(run.attempt.source.provider)
    collected_at, revision = datetime.now(UTC), str(a.connector_revision)
    state = await _state(run)
    sections = _payload(news.section_urls, provider, a.source.configuration)
    url = (
        sections[0][1] if sections else _payload(news.preset_url, provider, a.source.configuration)
    )
    stored = (
        news.FeedValidators(
            url,
            revision,
            state.etag,
            state.last_modified,
        )
        if state.validators_revision == a.connector_revision
        else None
    )
    lease = await _lease(run)
    if len(sections) > 1:
        found, failed = await _fetch_sections(run, provider, sections, revision, collected_at)
        update = None
    else:
        failed = []
        fetched = await _get_body(
            run, news.feed_request(provider, stored, revision, url), max_bytes=news.MAX_FEED_BYTES
        )
        # feedparser-style XML parse of up to MAX_FEED_BYTES is CPU-bound: keep it off the event loop.
        result = await asyncio.to_thread(
            _payload,
            news.map_feed_response,
            provider,
            200 if fetched.body is not None else 304,
            fetched.body or b"",
            collected_at,
        )
        if result.not_modified:
            await _accept_native(run, lease, (), "returned_snapshot", collected_at)
            return
        captured = news.capture_validators(
            provider,
            {"etag": fetched.etag or "", "last-modified": fetched.last_modified or ""},
            revision,
            url,
        )
        update = CollectionStateUpdate(
            update_validators=True,
            etag=captured.etag if captured else None,
            last_modified=captured.last_modified if captured else None,
        )
        found = list(result.records)
    found = provider_options.filter_records(
        provider, a.source.configuration or {}, found
    )  # e.g. advisory level
    records, cursor_after = await _select(run, lease, found, collected_at)
    if "selection" in run.extras:
        run.extras["selection"]["failed_sections"] = failed
    if failed:
        # A failed section may hold items older than the newest cursor candidate; keep the old cursor so the next
        # run re-reads them (dedup by record hash prevents duplicates of what this run stored).
        cursor_after = None
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, update, cursor_after=cursor_after
    )


async def _run_hn(run: Run) -> None:
    """Hacker News: one id-list send plus ``max_items`` (default 10, hard cap 30) sequential item sends, each debited."""
    from modules.connectors.providers import news

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    listing = provider_options.build_url("hn_top", cfg, cursor=None, now=collected_at)
    top = _require_body(
        await _get_body(
            run,
            news.hn_top_request(listing) if listing else news.hn_top_request(),
            max_bytes=64 * 1024,
        )
    )
    ids = _payload(news.parse_hn_top_ids, top, provider_options.hn_limit(cfg))
    bodies: dict[int, bytes | None] = {}
    for item_id in ids:
        try:
            bodies[item_id] = (
                await _get_body(run, news.hn_item_request(item_id), max_bytes=64 * 1024)
            ).body
        except rest.ProviderHttpError as exc:
            if exc.status_code != 404:
                raise
            bodies[item_id] = None  # removed item: skipped and counted by the mapper
    result = await _payload_async(
        news.map_hn_stories, ids, bodies, collected_at, provider_options.hn_limit(cfg)
    )
    run.extras["skipped"] = result.skipped
    records, cursor_after = await _select(run, lease, result.records, collected_at)
    await _accept_native(
        run, lease, records, "returned_snapshot", collected_at, cursor_after=cursor_after
    )


async def _run_gdelt(run: Run) -> None:
    """Experimental GDELT: hard 8 s timeout; on timeout nothing is accepted so the last good records stay."""
    from modules.connectors.providers.gdelt import GDELT_TIMEOUT_SECONDS, gdelt_request
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.news import map_news_body

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    cursor = await _resume_cursor(run, lease)
    request = gdelt_request()
    if (
        url := provider_options.build_url("gdelt_economy", cfg, cursor=cursor, now=collected_at)
    ) is not None:
        request = replace(request, url=url)
    fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES, timeout=GDELT_TIMEOUT_SECONDS)
    records = await _payload_async(
        map_news_body, "gdelt_economy", _require_body(fetched), collected_at
    )
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_sec_edgar(run: Run) -> None:
    """SEC EDGAR recent filings: one gated send with the owner-declared User-Agent; missing scope fails closed."""
    from modules.connectors.providers import filings
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg = run.attempt.source.configuration or {}
    request = _payload(filings.sec_edgar_request, cfg.get("sec_cik"), cfg.get("sec_contact_email"))
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
    payload = await _payload_async(decode_json, _require_body(fetched))
    records = await _payload_async(filings.map_sec_edgar, payload, collected_at, cfg["sec_cik"])
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_openalex(run: Run) -> None:
    """OpenAlex works search: one gated send to a server-built URL; no validators, first page only."""
    from modules.connectors.providers import scholarly

    collected_at = datetime.now(UTC)
    request = _payload(
        scholarly.openalex_request, (run.attempt.source.configuration or {}).get("scholarly_query")
    )
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=scholarly.OPENALEX_MAX_BODY)
    records = await _payload_async(scholarly.map_openalex, _require_body(fetched), collected_at)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_tvmaze(run: Run) -> None:
    """Collect one TVMaze schedule through the send gate with URL-bound validators.

    A new date or scope never replays another URL's validators. A 304 retains existing
    records; accepted records, validator state, and the dedup-only selection commit together.
    """
    from modules.connectors.providers.media_movies_tv import (
        collect_tvmaze_schedule,
        tvmaze_schedule_url,
    )

    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    web = cfg.get("tvmaze_mode") == "schedule_web"
    country = ("" if cfg.get("tvmaze_global") else cfg.get("media_country")) if web else (cfg.get("media_country") or "US")
    date = now.date().isoformat()
    url = tvmaze_schedule_url(country=country, date=date, web=web)
    fingerprint = sha256(url.encode("utf-8")).hexdigest()
    state = await _state(run)
    headers = _conditional(state, run.attempt.connector_revision) if state.continuation_state == fingerprint else None
    lease = await _lease(run)
    page = await collect_tvmaze_schedule(
        run.gate.fetch_bytes, country=country, date=date, web=web, config=cfg,
        collected_at=now, etag=(headers or {}).get("If-None-Match"),
        last_modified=(headers or {}).get("If-Modified-Since"),
    )
    run.extras["native_fp"] = fingerprint
    if page.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", now)
        return
    records, cursor_after = await _select(run, lease, page.records, now)
    # A selection cap leaves unseen records; replaying a 304 would strand that backlog.
    backlog = bool(run.extras.get("selection", {}).get("backlog"))
    update = CollectionStateUpdate(
        update_validators=True,
        etag=None if backlog else page.etag,
        last_modified=None if backlog else page.last_modified,
    )
    await _accept_native(run, lease, records, _coverage(records), now, update, cursor_after=cursor_after)


async def _run_livechart(run: Run) -> None:
    """Collect LiveChart episodes through SendGate and persist URL-scoped validators with accepted records."""
    from modules.connectors.providers.livechart import LIVECHART_URL, collect_livechart_feed

    now = datetime.now(UTC)
    cfg = run.attempt.source.configuration or {}
    fingerprint = sha256(LIVECHART_URL.encode("utf-8")).hexdigest()
    state = await _state(run)
    headers = _conditional(state, run.attempt.connector_revision) if state.continuation_state == fingerprint else None
    lease = await _lease(run)
    page = await collect_livechart_feed(
        run.gate.fetch_bytes,
        collected_at=now,
        config=cfg,
        etag=(headers or {}).get("If-None-Match"),
        last_modified=(headers or {}).get("If-Modified-Since"),
    )
    run.extras["native_fp"] = fingerprint
    if page.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", now)
        return
    records, cursor_after = await _select(run, lease, page.records, now)
    backlog = bool(run.extras.get("selection", {}).get("backlog"))
    update = CollectionStateUpdate(
        update_validators=True,
        etag=None if backlog else page.etag,
        last_modified=None if backlog else page.last_modified,
    )
    await _accept_native(run, lease, records, _coverage(records), now, update, cursor_after=cursor_after)


_ANILIST_PACER = None


async def _run_anilist(run: Run) -> None:
    """Collect a bounded full-sweep segment and atomically persist its accepted page checkpoint."""
    from modules.connectors.providers._media_common import MediaRatePacer
    from modules.connectors.providers.anilist import (
        MAX_PAGES_PER_RUN,
        MAX_RESULTS,
        anilist_query_fingerprint,
        collect_anilist,
        parse_anilist_cursor,
    )

    global _ANILIST_PACER
    if _ANILIST_PACER is None:
        _ANILIST_PACER = MediaRatePacer()
    now = datetime.now(UTC)
    cfg = run.attempt.source.configuration or {}
    provider = run.attempt.source.provider
    if provider is None:
        raise ValueError("provider_scope_invalid")
    lease = await _lease(run)
    fingerprint = anilist_query_fingerprint(provider, cfg)
    state = await _state(run)
    page_number, watermark = parse_anilist_cursor(
        lease.cursor_before,
        fingerprint_matches=state.continuation_state == fingerprint,
        collected_at=now,
    )
    mode = cfg.get("anilist_mode") or "updated"
    if mode != "updated":
        watermark = 0
    records_by_id: dict[str, Any] = {}
    cursor_after = f"{page_number}:{watermark}"
    for _ in range(MAX_PAGES_PER_RUN):
        await _ANILIST_PACER.pace("anilist", 2.1)
        result = await collect_anilist(
            run.gate.fetch_bytes, provider, config=cfg, collected_at=now, page=page_number,
            updated_after=watermark or None if mode == "updated" else None,
        )
        for record in result.records:
            records_by_id.setdefault(record.provider_id, record)
        cursor_after = f"{page_number + 1}:{watermark}"
        if not result.has_next_page or result.row_count < MAX_RESULTS:
            cursor_after = "1:0"
            break
        page_number += 1
    run.extras["native_fp"] = fingerprint
    await _accept_native(
        run, lease, list(records_by_id.values()), _coverage(list(records_by_id.values())), now,
        cursor_after=cursor_after,
    )


async def _run_openlibrary(run: Run) -> None:
    """Collect one bounded Open Library page and atomically advance recent-change pagination."""
    from modules.connectors.providers.openlibrary import collect_openlibrary

    now = datetime.now(UTC)
    cfg = run.attempt.source.configuration or {}
    lease = await _lease(run)
    fingerprint = provider_options.native_fingerprint("openlibrary_books", cfg)
    state = await _state(run)
    cursor = lease.cursor_before if state.continuation_state == fingerprint else None
    page = await collect_openlibrary(run.gate.fetch_bytes, config=cfg, collected_at=now, cursor=cursor)
    run.extras["native_fp"] = fingerprint
    await _accept_native(run, lease, page.records, _coverage(page.records), now, cursor_after=page.cursor)


async def _run_gutendex(run: Run) -> None:
    """Collect one Gutendex page and commit its continuation with its accepted records."""
    from modules.connectors.providers.gutendex import collect_gutendex

    now = datetime.now(UTC)
    cfg = run.attempt.source.configuration or {}
    lease = await _lease(run)
    fingerprint = provider_options.native_fingerprint("gutendex_books", cfg)
    state = await _state(run)
    cursor = lease.cursor_before if state.continuation_state == fingerprint else None
    page = await collect_gutendex(run.gate.fetch_bytes, config=cfg, collected_at=now, cursor=cursor)
    run.extras["native_fp"] = fingerprint
    await _accept_native(run, lease, page.records, _coverage(page.records), now, cursor_after=page.cursor)


async def _run_hardcover(run: Run) -> None:
    """Fetch at most two gated Hardcover pages and commit only if the whole walk and credential proof hold."""
    from modules.connectors.providers import hardcover

    attempt = run.attempt
    try:
        header, secret = await _rest_secret(run)
    except rest.ProviderHttpError as exc:
        raise CredentialMissing from exc
    if header != NATIVE_REST_PLACEMENTS[hardcover.PROVIDER] or not secret or attempt.credential_revision is None:
        raise CredentialMissing
    run.gate.bind_credential(
        quota.credential_fingerprint(
            secret,
            deployment_key=run.settings.connector_credential_encryption_key.get_secret_value(),
        )
    )
    cfg = attempt.source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = provider_options.native_fingerprint(hardcover.PROVIDER, cfg)
    state = await _state(run)
    after = hardcover.cursor_id(lease.cursor_before) if state.continuation_state == fingerprint else 0
    records: list[IngestionRecord] = []
    for _ in range(hardcover.MAX_PAGES):
        request = hardcover.build_request(secret, cfg, now, after)
        fetched = await _get_body(run, request, max_bytes=hardcover.MAX_BODY_BYTES)
        page, after = await _payload_async(hardcover.map_page, _require_body(fetched), cfg, now, after)
        records.extend(page)
        if after == 0:
            break
    # Keep the leased cursor and fingerprint intact until every fetched page can be accepted together.
    run.extras["native_fp"] = fingerprint
    await _accept_native(
        run, lease, records, _coverage(records), now, cursor_after=str(after),
        provider_credential_revision=attempt.credential_revision,
    )


def _media_scope_fingerprint(provider: str, config: dict[str, Any]) -> str:
    """Fingerprint a provider's effective query scope so saved page cursors and validators cannot cross scopes."""
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return f"media-v1:{sha256((provider + ':' + payload).encode('utf-8')).hexdigest()[:24]}"


async def _run_ann_news(run: Run) -> None:
    """Collect ANN Atom news or one paced Encyclopedia XML page and accept its page cursor atomically."""
    from modules.connectors.providers._media_common import MediaRatePacer
    from modules.connectors.providers.media_anime_manga_sources import (
        collect_ann_encyclopedia,
        collect_ann_news,
    )

    global _ANN_PACER
    if _ANN_PACER is None:
        _ANN_PACER = MediaRatePacer()
    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    mode = cfg.get("ann_mode") or "news"
    fingerprint = _media_scope_fingerprint("ann_news", cfg)
    cursor = lease.cursor_before if (await _state(run)).continuation_state == fingerprint else None
    if mode == "encyclopedia":
        raw_offset = cursor or "0"
        if not raw_offset.isdigit() or int(raw_offset) > 1_000_000:
            raw_offset = "0"
        await _ANN_PACER.pace("ann_encyclopedia", 1.0)
        page = await collect_ann_encyclopedia(
            run.gate.fetch_bytes,
            media_type=cfg.get("ann_media_type") or "anime",
            collected_at=now,
            config=cfg,
            offset=int(raw_offset),
        )
        records, cursor_after = list(page.records), page.cursor or "0"
    else:
        records = await collect_ann_news(run.gate.fetch_bytes, collected_at=now, config=cfg)
        cursor_after = None
    run.extras["native_fp"] = fingerprint
    await _accept_native(run, lease, records, _coverage(records), now, cursor_after=cursor_after)


_ANN_PACER: Any = None


async def _run_mangaupdates(run: Run) -> None:
    """Fetch one paced MangaUpdates release page and commit its cursor with accepted records."""
    from modules.connectors.providers._media_common import MediaRatePacer
    from modules.connectors.providers.media_anime_manga_sources import collect_mangaupdates_releases

    global _MANGAUPDATES_PACER
    if _MANGAUPDATES_PACER is None:
        _MANGAUPDATES_PACER = MediaRatePacer()
    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = _media_scope_fingerprint("mangaupdates_releases", cfg)
    state = await _state(run)
    raw_page = lease.cursor_before if state.continuation_state == fingerprint else None
    page_number = int(raw_page) if raw_page and raw_page.isdigit() and int(raw_page) <= 100_000 else 0
    await _MANGAUPDATES_PACER.pace("mangaupdates", 1.0)
    page = await collect_mangaupdates_releases(
        run.gate.fetch_bytes, collected_at=now, config=cfg, page=page_number
    )
    run.extras["native_fp"] = fingerprint
    await _accept_native(
        run, lease, page.records, _coverage(page.records), now, cursor_after=page.cursor or "0"
    )


async def _run_mangadex(run: Run) -> None:
    """Fetch one MangaDex manga/chapter window and atomically persist its continuation cursor."""
    from modules.connectors.providers._media_common import MediaRatePacer
    from modules.connectors.providers.media_anime_manga_sources import collect_mangadex_releases

    global _MANGADEX_PACER
    if _MANGADEX_PACER is None:
        _MANGADEX_PACER = MediaRatePacer()
    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = _media_scope_fingerprint("mangadex_releases", cfg)
    state = await _state(run)
    cursor = lease.cursor_before if state.continuation_state == fingerprint else None
    await _MANGADEX_PACER.pace("mangadex", 1.0)
    provider_cfg = dict(cfg)
    if "mangadex_content_ratings" in provider_cfg:
        provider_cfg["content_ratings"] = provider_cfg["mangadex_content_ratings"]
    page = await collect_mangadex_releases(
        run.gate.fetch_bytes, collected_at=now, config=provider_cfg, cursor=cursor
    )
    run.extras["native_fp"] = fingerprint
    await _accept_native(
        run, lease, page.records, _coverage(page.records), now, cursor_after=page.cursor or "0:0"
    )


_MANGAUPDATES_PACER: Any = None
_MANGADEX_PACER: Any = None


async def _run_apple_books(run: Run) -> None:
    """Collect one Apple Books chart and persist matching conditional validators with its accepted records."""
    from modules.connectors.providers.apple_podcasts import collect_apple_books

    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = _media_scope_fingerprint("apple_books_rss", cfg)
    state = await _state(run)
    conditional = _conditional(state, run.attempt.connector_revision) if state.continuation_state == fingerprint else None
    page = await collect_apple_books(
        run.gate.fetch_bytes,
        country=cfg.get("country") or "US",
        kind=cfg.get("kind") or "books",
        chart=cfg.get("chart") or "top-free",
        collected_at=now,
        config=cfg,
        etag=(conditional or {}).get("If-None-Match"),
        last_modified=(conditional or {}).get("If-Modified-Since"),
    )
    run.extras["native_fp"] = fingerprint
    if page.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", now)
        return
    update = CollectionStateUpdate(
        update_validators=True, etag=page.etag, last_modified=page.last_modified
    )
    await _accept_native(run, lease, page.records, _coverage(page.records), now, update)


async def _run_podcast_rss(run: Run) -> None:
    """Fetch one podcast feed; only a fixed feed URL may reuse or persist HTTP validators."""
    from modules.connectors.providers.apple_podcasts import collect_podcast_rss

    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = _media_scope_fingerprint("podcast_rss", cfg)
    state = await _state(run)
    direct_feed = cfg.get("feed_url") is not None
    conditional = (
        _conditional(state, run.attempt.connector_revision)
        if direct_feed and state.continuation_state == fingerprint
        else None
    )
    page = await collect_podcast_rss(
        run.gate.fetch_bytes,
        feed_url=cfg.get("feed_url"),
        itunes_id=str(cfg["itunes_podcast_id"]) if cfg.get("itunes_podcast_id") is not None else None,
        collected_at=now,
        config=cfg,
        etag=(conditional or {}).get("If-None-Match"),
        last_modified=(conditional or {}).get("If-Modified-Since"),
    )
    run.extras["native_fp"] = fingerprint
    if page.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", now)
        return
    update = CollectionStateUpdate(
        update_validators=True,
        etag=page.etag if direct_feed else None,
        last_modified=page.last_modified if direct_feed else None,
    )
    await _accept_native(run, lease, page.records, _coverage(page.records), now, update)


async def _run_wikidata_movies(run: Run) -> None:
    """Collect the bounded Wikidata movie query through the source's metered SendGate."""
    from modules.connectors.providers.media_movie_sources import collect_wikidata_movies

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    records = await collect_wikidata_movies(run.gate.fetch_bytes, config=cfg, collected_at=now)
    await _accept_native(run, lease, records, _coverage(records), now)


async def _run_itunes_movies(run: Run) -> None:
    """Collect a bounded iTunes movie search through the source's metered SendGate."""
    from modules.connectors.providers.media_movie_sources import collect_itunes_movies

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    records = await collect_itunes_movies(run.gate.fetch_bytes, config=cfg, collected_at=now)
    await _accept_native(run, lease, records, _coverage(records), now)


async def _run_letterboxd(run: Run) -> None:
    """Fetch one Letterboxd username RSS and commit URL-bound validators with records."""
    from modules.connectors.providers.media_movie_sources import collect_letterboxd_rss

    source = run.attempt.source
    cfg: dict[str, Any] = source.configuration or {}
    now = datetime.now(UTC)
    lease = await _lease(run)
    fingerprint = _media_scope_fingerprint("letterboxd_rss", cfg)
    state = await _state(run)
    conditional = _conditional(state, run.attempt.connector_revision) if state.continuation_state == fingerprint else None
    page = await collect_letterboxd_rss(
        run.gate.fetch_bytes,
        username=str(cfg.get("letterboxd_username") or cfg.get("username") or ""),
        config=cfg,
        collected_at=now,
        etag=(conditional or {}).get("If-None-Match"),
        last_modified=(conditional or {}).get("If-Modified-Since"),
    )
    run.extras["native_fp"] = fingerprint
    if page.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", now)
        return
    update = CollectionStateUpdate(
        update_validators=True, etag=page.etag, last_modified=page.last_modified
    )
    await _accept_native(run, lease, page.records, _coverage(page.records), now, update)


ADAPTERS.update(
    {
        **dict.fromkeys(PURE_PROVIDERS, _run_pure),
        "vietnamworks_jobs": _run_vietnamworks,
        "itviec_jobs": _run_itviec,
        "vn_stock": _run_vn_stock,  # two endpoints (stocks, indices); overrides the single-request runner
        **dict.fromkeys(KEYED, _run_keyed),
        "coingecko": _run_coingecko,
        **dict.fromkeys(
            _news.FEED_URLS, _run_feed
        ),  # every fixed-URL feed preset shares one adapter
        "coinpaprika": _run_coinpaprika,  # one call per selected coin
        "google_news": _run_feed,
        "openalex": _run_openalex,
        "tvmaze_schedule": _run_tvmaze,
        "livechart_episodes": _run_livechart,
        "ann_news": _run_ann_news,
        "mangaupdates_releases": _run_mangaupdates,
        "mangadex_releases": _run_mangadex,
        "anilist_anime": _run_anilist,
        "anilist_manga": _run_anilist,
        "openlibrary_books": _run_openlibrary,
        "gutendex_books": _run_gutendex,
        "hardcover_books": _run_hardcover,
        "apple_books_rss": _run_apple_books,
        "podcast_rss": _run_podcast_rss,
        "wikidata_movies": _run_wikidata_movies,
        "itunes_movies": _run_itunes_movies,
        "letterboxd_rss": _run_letterboxd,
        "sec_edgar": _run_sec_edgar,
        "hn_top": _run_hn,
        "gdelt_economy": _run_gdelt,
    }
)


# ---------------------------------------------------------------- GitHub / Telegram (proof-bearing)
