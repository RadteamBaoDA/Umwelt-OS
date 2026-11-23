"""Seed one source per offline-fixture provider for the chat real-model eval.

Every provider with a fixture under ``tests/fixtures/providers`` is either seeded here through its
production mapper (fed the offline body) or listed in ``NOT_SEEDED`` with the reason. Providers the matrix
already seeds, and fixtures whose records would change an existing case's answer, stay out. Source keys
are ``fx_<provider>``; they never reuse a matrix key.

A provider whose normalize stage does not settle does not abort the seed: its source is archived so chat
cannot read its rows, its failure is appended to the returned report with the normalize stage error code and
the observation row error codes read from the database, and seeding continues. The matrix seed and the planted
gold keep failing loudly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.providers.gdelt import map_gdelt
from modules.connectors.providers.news import map_feed, map_hn_stories
from modules.connectors.providers.world_data import _market_records, map_pure_provider_body
from modules.connectors.public import ConnectorConfig
from modules.ingestion.schemas import IngestionRecord
from tests.integration.test_chat_collected_data_matrix import (
    COLLECTED,
    Matrix,
    _create_source,
    _ingest,
    _set,
)
from tests.unit.modules.connectors.p3_helpers import fixture

# provider -> fixture files that feed its mapper (one source per provider)
SEEDED: Mapping[str, tuple[str, ...]] = {
    "bbc_world": ("bbc_world_rss.xml",),
    "fed_press": ("fed_press_rss.xml",),
    "github_trending": ("github_trending_rss.xml",),
    "google_news": ("google_news_rss.xml",),
    "ecb": ("ecb_eurofxref_daily.xml",),
    "alternative_me": ("fear_greed.json",),
    "ioda": ("ioda_events.json",),
    "iss_position": ("iss_position.json",),
    "launch_library": ("launch_library.json",),
    "nasa_eonet": ("nasa_eonet_open.json",),
    "noaa_kp": ("noaa_kp.json",),
    "nws_alerts": ("nws_alerts.json",),
    "oecd_cli": ("oecd_cli.json",),
    "satnogs_observations": ("satnogs_observations.json",),
    "wikipedia_mostread": ("wikipedia_mostread.json",),
    "wikipedia_top_views": ("wikipedia_top_views.json",),
    "disease_sh": ("disease_sh.json",),
    "vn_stock": ("vn_stock_prices.json",),
    "gdelt_economy": ("gdelt_artlist.json",),
    "hn_top": (
        "hn_item_story.json",
        "hn_item_ask.json",
        "hn_item_deleted.json",
        "hn_item_dead.json",
    ),
    "alpha_vantage": ("alpha_vantage_ibm_daily.json",),
}

_RSS_PROVIDERS = frozenset({"bbc_world", "fed_press", "github_trending", "google_news"})
_AV_CONFIG = ConnectorConfig(
    market_symbols=("IBM",), market_currency="USD", market_exchange_timezone="America/New_York"
)
# The market scope fields (symbols, currency, timezone) must be stored on the source. Chat reads a market
# point only when its scope is verifiable; an alpha_vantage source without them fails the normalize stage
# ("missing its scope") for every row. Same dump the matrix stores for its market sources.
_AV_CONFIG_DUMP: dict[str, Any] = _AV_CONFIG.model_dump(mode="json", exclude_none=True)
_CONFIGURATION: Mapping[str, dict[str, Any]] = {"alpha_vantage": _AV_CONFIG_DUMP}
_MATRIX_SEEDS = "the matrix already seeds this provider"
_BTC_DISTRACTOR = (
    "carries a BTC USD price; a second BTC price would change the unscoped btc_* case answers"
)
_QUAKE_DISTRACTOR = (
    "other earthquake/hazard feed; would compete with the unscoped USGS strongest-quake answers"
)
_JOB_DISTRACTOR = "job postings with pay would compete with the Python Developer salary cases"

# fixture, provider, reason: not seeded on purpose (kept in the returned list for the report)
NOT_SEEDED: tuple[tuple[str, str, str], ...] = (
    ("binance_btcusdt.json", "binance", _MATRIX_SEEDS),
    ("cisa_kev.json", "cisa_kev", _MATRIX_SEEDS),
    ("emsc_quakes.json", "emsc_quakes", _MATRIX_SEEDS),
    ("frankfurter_usd_vnd.json", "frankfurter", _MATRIX_SEEDS),
    ("usgs_4_5_day.geojson", "usgs", _MATRIX_SEEDS),
    ("vietnamworks_jobs.json", "vietnamworks_jobs", _MATRIX_SEEDS),
    ("world_bank_vn_gdp.json", "world_bank", _MATRIX_SEEDS),
    ("vnexpress_atom.xml", "vnexpress_business", _MATRIX_SEEDS),
    ("coingecko_btc.json", "coingecko", _BTC_DISTRACTOR),
    ("coinpaprika_btc.json", "coinpaprika", _BTC_DISTRACTOR),
    (
        "open_er_api.json",
        "open_er_api",
        "quotes a second USD/VND rate that would change the frankfurter USD/VND answers",
    ),
    (
        "nvd_cves.json",
        "nvd",
        "CVE rows would change the unscoped cve_count answer over the KEV data",
    ),
    ("bmkg_quakes.json", "bmkg_quakes", _QUAKE_DISTRACTOR),
    ("jma_eqvol_atom.xml", "jma_eqvol", _QUAKE_DISTRACTOR),
    ("gdacs_events.json", "gdacs_events", _QUAKE_DISTRACTOR),
    ("itviec_search.html", "itviec_jobs", _JOB_DISTRACTOR),
    ("itviec_job_9001.html", "itviec_jobs", _JOB_DISTRACTOR),
    ("itviec_job_9002.html", "itviec_jobs", _JOB_DISTRACTOR),
    ("itviec_job_9003.html", "itviec_jobs", _JOB_DISTRACTOR),
    ("itviec_job_malformed.html", "itviec_jobs", _JOB_DISTRACTOR),
    ("wwr_jobs_rss.xml", "wwr_jobs", _JOB_DISTRACTOR),
    (
        "news_hostile_doctype.xml",
        "bbc_world",
        "negative fixture: the mapper must reject it (forbidden_xml), so it is never ingested",
    ),
    (
        "hn_topstories.json",
        "hn_top",
        "id index only; its item bodies are seeded under hn_top",
    ),
)


def normalize_failure_reason(stage_code: str | None, row_codes: Mapping[str, int]) -> str:
    """Render the report reason for a provider whose normalize stage did not succeed.

    Pure formatting, testable without a database. ``stage_code`` is the normalize stage's ``error_code``
    (None when unset); ``row_codes`` maps each observation row error code to its count. Codes are sorted
    so the same failure always renders the same reason.
    """
    stage = stage_code or "none"
    rows = ", ".join(f"{code} x{count}" for code, count in sorted(row_codes.items())) or "none"
    return f"normalize stage failed (error_code={stage}); row error_codes: {rows}"


async def _normalize_failure(
    engine: AsyncEngine, source_id: UUID
) -> tuple[str | None, dict[str, int]]:
    """Read the normalize stage error code of the source's newest run and its per-row error counts.

    Read-only. Runs are ordered newest first, so a retried source reports its latest failure. Row codes
    live on ``observation_normalizations`` because they are set per observation, not per stage.
    """
    async with engine.connect() as conn:
        stage_code: str | None = (
            await conn.execute(
                text(
                    "SELECT st.error_code FROM ingestion_stages st "
                    "JOIN ingestion_runs r ON r.id = st.run_id "
                    "WHERE r.source_id = :i AND st.stage_key = 'normalize' "
                    "ORDER BY r.created_at DESC LIMIT 1"
                ),
                {"i": source_id},
            )
        ).scalar_one_or_none()
        rows = (
            await conn.execute(
                text(
                    "SELECT error_code, count(*) FROM observation_normalizations "
                    "WHERE source_id = :i AND error_code IS NOT NULL GROUP BY error_code"
                ),
                {"i": source_id},
            )
        ).all()
    return stage_code, {str(code): cast(int, count) for code, count in rows}


def _provider_records(provider: str, bodies: Sequence[bytes]) -> list[IngestionRecord]:
    """Map one provider's offline bodies with its production mapper; no I/O.

    HN item bodies are keyed by their own ``id`` so ``map_hn_stories`` drops the deleted and dead ones.
    """
    if provider == "hn_top":
        items: dict[int, bytes] = {int(json.loads(b)["id"]): b for b in bodies}
        return list(map_hn_stories(tuple(items), items, COLLECTED).records)
    if provider in _RSS_PROVIDERS:
        return list(map_feed(provider, bodies[0], COLLECTED))
    if provider == "gdelt_economy":
        return list(map_gdelt(json.loads(bodies[0]), COLLECTED))
    if provider == "alpha_vantage":
        payload: Any = json.loads(bodies[0])
        # the mapper reads only the body for fixtures; unit tests pass None as the source too
        return list(
            _market_records(None, _AV_CONFIG, payload, COLLECTED)  # type: ignore[arg-type]
        )
    return list(map_pure_provider_body(provider, bodies[0], COLLECTED))


async def seed_all(client: AsyncClient, engine: AsyncEngine, m: Matrix) -> list[dict[str, str]]:
    """Create and ingest one source per entry of ``SEEDED``; return the ``NOT_SEEDED`` rows plus failures.

    Each source is created through the matrix helper (its key lands in ``m.sources``) with its stored
    configuration where the mapper's scope needs one, then ingested through the real normalization path.
    When a provider's normalize stage does not settle, ``_ingest`` raises ``pytest.fail``; that one outcome
    is caught here. The source is archived (chat excludes archived sources), its failure is appended with the
    database error codes, and the next provider is seeded. Mapper and source-creation errors are not caught.
    """
    failures: list[dict[str, str]] = []
    for provider, fixtures in SEEDED.items():
        key = f"fx_{provider}"
        await _create_source(
            client,
            engine,
            m,
            key,
            provider=provider,
            source_type=PROVIDER_SOURCE_TYPES[provider],
            configuration=_CONFIGURATION.get(provider),
        )
        records = _provider_records(provider, [fixture(f) for f in fixtures])
        try:
            await _ingest(engine, m, key, records)
        except (
            pytest.fail.Exception
        ):  # the matrix _ingest reports an unsettled normalize stage this way
            stage_code, row_codes = await _normalize_failure(engine, m.sources[key])
            await _set(engine, m, key, "status = 'archived'")
            failures.append(
                {
                    "provider": provider,
                    "fixture": ", ".join(fixtures),
                    "reason": normalize_failure_reason(stage_code, row_codes),
                }
            )
    return [
        {"provider": provider, "fixture": name, "reason": reason}
        for name, provider, reason in NOT_SEEDED
    ] + failures
