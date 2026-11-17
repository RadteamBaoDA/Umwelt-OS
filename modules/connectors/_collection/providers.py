"""Free/pure/keyed provider adapters (world data, jobs, feeds, HN, GDELT, SEC, OpenAlex).

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from modules.connectors import provider_options, quota
from modules.connectors._collection.base import (
    ADAPTERS,
    KEYED_PROVIDERS,
    PURE_MAX_BYTES,
    PURE_PROVIDERS,
    CredentialMissing,
    Run,
    _accept_native,
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
    """
    from modules.connectors.providers.macro import decode_json

    provider = str(run.attempt.source.provider)
    row = KEYED[provider]
    cfg = getattr(run.attempt.source, "configuration", None) or {}
    request = _payload(row.build, await _keyed_secret(run), cfg)
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=KEYED_MAX_BYTES)
    body = _require_body(fetched)
    payload = body if row.binary else (await _payload_async(decode_json, body))
    records = await _payload_async(row.mapper, payload, collected_at, cfg)
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
        "sec_edgar": _run_sec_edgar,
        "hn_top": _run_hn,
        "gdelt_economy": _run_gdelt,
    }
)


# ---------------------------------------------------------------- GitHub / Telegram (proof-bearing)
