"""Seed one source per offline-fixture provider for the chat real-model eval.

Every provider with a fixture under ``tests/fixtures/providers`` is either seeded here through its
production mapper (fed the offline body) or listed in ``NOT_SEEDED`` with the reason. Providers the matrix
already seeds, and fixtures whose records would change an existing case's answer, stay out. Source keys
are ``fx_<provider>``; they never reuse a matrix key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from httpx import AsyncClient
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
    """Create and ingest one source per entry of ``SEEDED``; return the ``NOT_SEEDED`` report rows.

    Each source is created through the matrix helper (its key lands in ``m.sources``) and ingested
    through the real normalization path. A normalization failure raises from ``_ingest``.
    """
    for provider, fixtures in SEEDED.items():
        key = f"fx_{provider}"
        await _create_source(
            client,
            engine,
            m,
            key,
            provider=provider,
            source_type=PROVIDER_SOURCE_TYPES[provider],
        )
        await _ingest(engine, m, key, _provider_records(provider, [fixture(f) for f in fixtures]))
    return [
        {"provider": provider, "fixture": name, "reason": reason}
        for name, provider, reason in NOT_SEEDED
    ]
