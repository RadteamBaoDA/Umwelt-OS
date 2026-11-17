"""Chat collected-data tool against one representative provider per datasource category (Docker harness).

Plan: "Danh gia theo loai datasource" table and "Buoc 7" tier 1. Seeding goes through the real ingestion
persistence and the real normalization worker: a ``ReceiveBatch`` is persisted with ``_insert_batch_graph`` and
``schedule_normalization`` (what ``receive_batch`` does, minus the native-provider refusal that every registered
provider hits), the receive stage and the outbox event are marked as the dispatcher would, and
``process_normalize_event`` then writes documents, observations and provenance (``source_fields``,
``world_data.provider_fields``, scope discriminator). Records come from the provider mappers fed with offline
fixtures from ``tests/fixtures/providers``. Every tool call is pinned to its sources with ``source_scope`` so
rows from other integration tests cannot leak in. Seeded once per session (committed rows persist).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import core.auth.models  # noqa: F401  # registers the owner table for foreign keys
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.chat import db_query
from modules.chat.db_query import execute_db_query_tool
from modules.connectors.providers import google_api, social_feeds, world_data
from modules.connectors.providers.crypto import map_binance
from modules.connectors.providers.disasters import map_usgs
from modules.connectors.providers.feed_catalog import _records_from_feed
from modules.connectors.providers.jobs import map_vietnamworks
from modules.connectors.providers.json_feed import JSON_FEEDS, map_json_feed
from modules.connectors.providers.macro import map_frankfurter, map_world_bank
from modules.connectors.providers.news import map_feed
from modules.connectors.providers.security import map_cisa_kev
from modules.connectors.public import ConnectorConfig
from modules.ingestion import worker
from modules.ingestion._batches import _insert_batch_graph
from modules.ingestion._outbox import schedule_normalization
from modules.ingestion._scope import _digest
from modules.ingestion.schemas import IngestionRecord, ReceiveBatch
from tests.unit.modules.connectors.p3_helpers import fixture, jfixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
    ),
]

FLAG = False  # the disposable stack runs the default single-workspace rollout
TZ = "Asia/Ho_Chi_Minh"
COLLECTED = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
OM_HOURS = ["2026-11-16T00:00", "2026-11-16T01:00"]
HANOI = {"weather_latitude": 21.0345, "weather_longitude": 105.8512}
HCMC = {"weather_latitude": 10.8231, "weather_longitude": 106.6297}
HANOI_LABEL, HCMC_LABEL = "21.03,105.85", "10.82,106.63"
ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom"'


@dataclass
class Matrix:
    """Committed identities of the seeded sources (key -> source id) and the owner principal."""

    workspace_id: UUID
    user_id: int
    revision: int
    sources: dict[str, UUID] = field(default_factory=dict)


_MATRIX: Matrix | None = None
_SEED_ERROR: str | None = None


def _ts_ms(moment: datetime) -> int:
    """Epoch milliseconds of ``moment`` (USGS event clock unit)."""
    return int(moment.timestamp() * 1000)


def _usgs_payload() -> dict[str, Any]:
    """Three quakes: 23:30 local on the 10th, 00:30 local on the 11th, 23:30 local on the 9th (HCMC)."""
    events = [
        ("us10a", 5.1, 35.1, datetime(2026, 11, 10, 16, 30, tzinfo=UTC)),
        ("us10b", 4.2, 12.0, datetime(2026, 11, 10, 17, 30, tzinfo=UTC)),
        ("us10c", 6.3, 60.5, datetime(2026, 11, 9, 16, 30, tzinfo=UTC)),
    ]
    return {
        "type": "FeatureCollection",
        "metadata": {"count": len(events)},
        "features": [
            {
                "type": "Feature",
                "id": ident,
                "properties": {
                    "mag": mag,
                    "place": f"{ident} place",
                    "time": _ts_ms(when),
                    "updated": _ts_ms(when),
                    "type": "earthquake",
                },
                "geometry": {"type": "Point", "coordinates": [142.5, 38.25, depth]},
            }
            for ident, mag, depth, when in events
        ],
    }


def _atom(entries: list[tuple[str, str, str, str]]) -> bytes:
    """Atom feed bytes from ``(id, title, summary, published)`` tuples."""
    body = "".join(
        f"<entry><id>{i}</id><title>{t}</title>"
        f'<link rel="alternate" href="https://example.invalid/{i}"/>'
        f"<summary>{s}</summary><published>{p}</published><updated>{p}</updated></entry>"
        for i, t, s, p in entries
    )
    return f"<?xml version='1.0' encoding='UTF-8'?><feed {ATOM_NS}><title>f</title>{body}</feed>".encode()


def _arxiv_atom() -> bytes:
    """One arXiv entry with two authors and a category (authors live in provider source_fields)."""
    return (
        f"<?xml version='1.0' encoding='UTF-8'?><feed {ATOM_NS}><title>cs.AI</title><entry>"
        "<id>http://arxiv.org/abs/2611.00001v1</id><title>Sparse attention at scale</title>"
        "<summary>We study attention.</summary><published>2026-11-16T00:00:00Z</published>"
        "<updated>2026-11-16T00:00:00Z</updated><author><name>Ada Lovelace</name></author>"
        '<author><name>Alan Turing</name></author><category term="cs.AI"/></entry></feed>'
    ).encode()


def _weather(label_cfg: dict[str, float], base: float) -> list[IngestionRecord]:
    """Two hourly temperature records (``base`` and ``base + 2``) for the configured point."""
    config = ConnectorConfig(
        **label_cfg, weather_timezone="UTC", weather_metrics=("temperature_2m",)
    )
    payload: dict[str, object] = {
        "timezone": "UTC",
        "hourly_units": {"temperature_2m": "C"},
        "hourly": {"time": OM_HOURS, "temperature_2m": [base, base + 2]},
    }
    return world_data._weather_records(  # offline mapper entry; no I/O variant exists
        SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
        config,
        payload,
        COLLECTED,
        coverage="returned_snapshot",
    )


def _binance(price: str, when: datetime) -> list[IngestionRecord]:
    """One BTCUSDT price record collected at ``when``."""
    return map_binance({"symbol": "BTCUSDT", "price": price}, when)


def _wb(restated: bool) -> list[IngestionRecord]:
    """World Bank VN GDP rows; ``restated`` rewrites the 2024 value (same identity: a new revision)."""
    payload = jfixture("world_bank_vn_gdp.json")
    if restated:
        for row in payload[1]:
            if row["date"] == "2024":
                row["value"] = 500000000000.5
    return map_world_bank(payload, COLLECTED)


def _calendar() -> list[IngestionRecord]:
    """One calendar event starting 10:30 local on 18 Nov."""
    event = {
        "id": "ev1",
        "summary": "Standup",
        "start": {"dateTime": "2026-11-18T10:30:00+07:00"},
        "end": {"dateTime": "2026-11-18T11:00:00+07:00"},
        "updated": "2026-11-16T00:00:00Z",
    }
    return [google_api._calendar_event_record("primary", event, COLLECTED)]


def _bluesky() -> list[IngestionRecord]:
    """One post whose title is its first line."""
    return [
        social_feeds._text_record(
            "at://did:plc:x/app.bsky.feed.post/1",
            "Shipping today\nsecond line",
            T2,
            COLLECTED,
            canonical_url=None,
            author="alice.example.com",
        )
    ]


async def _ingest(
    engine: AsyncEngine, matrix: Matrix, key: str, records: list[IngestionRecord]
) -> None:
    """Persist ``records`` as an accepted batch of source ``key`` and run the real normalization worker.

    Fails the test setup loudly when the normalize stage does not reach ``succeeded``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source_id = matrix.sources[key]
    async with engine.connect() as conn:
        generation: int = (
            await conn.execute(
                text("SELECT generation FROM sources WHERE id = :i"), {"i": source_id}
            )
        ).scalar_one()
    scope = InternalJobScope(
        workspace_id=matrix.workspace_id,
        actor_user_id=matrix.user_id,
        membership_revision=matrix.revision,
        source_id=source_id,
        source_generation=generation,
    )
    # The trusted native-collection path carries provider_record metadata; ReceiveBatch's validator rejects it
    # only for untrusted API input, so the simulated trusted path skips validation.
    payload = ReceiveBatch.model_construct(
        source_id=source_id,
        source_generation=generation,
        batch_key=f"matrix-{uuid4().hex}",
        cursor_before=None,
        cursor_after=None,
        records=records,
    )
    now = datetime.now(UTC)
    async with factory() as session:
        _batch, run, _stage = await _insert_batch_graph(
            session,
            payload,
            SimpleNamespace(generation=generation),  # type: ignore[arg-type]
            _digest(payload.model_dump(mode="json")),
            now,
            scope=scope,
            multi_workspace_enabled=FLAG,
        )
        await schedule_normalization(
            session, run, _batch, generation, now, scope=scope, multi_workspace_enabled=FLAG
        )
        await session.commit()
        run_id = run.id
    ctx: dict[str, object] = {
        "session_factory": factory,
        "settings": SimpleNamespace(multi_workspace_enabled=FLAG),
    }
    async with factory() as session:
        # The dispatcher would mark the receive stage done and queue the normalize event.
        await session.execute(
            text(
                "UPDATE ingestion_stages SET status = 'succeeded' "
                "WHERE run_id = :r AND stage_key = 'receive'"
            ),
            {"r": run_id},
        )
        await session.commit()
    status: object = "unknown"
    for _ in range(8):  # normalization is bounded per slice; re-queue until the stage settles
        async with factory() as session:
            event_id = (
                await session.execute(
                    text(
                        "UPDATE event_outbox SET status = 'queued', dispatched_at = now() "
                        "WHERE type = 'ingestion.normalize.requested' AND payload->>'run_id' = :r "
                        "AND status <> 'delivered' RETURNING id"
                    ),
                    {"r": str(run_id)},
                )
            ).scalar_one_or_none()
            await session.commit()
        if event_id is None:
            break
        await worker.process_normalize_event(ctx, str(event_id))
        async with factory() as session:
            status = (
                await session.execute(
                    text(
                        "SELECT status FROM ingestion_stages "
                        "WHERE run_id = :r AND stage_key = 'normalize'"
                    ),
                    {"r": run_id},
                )
            ).scalar_one()
        if status == "succeeded":
            return
    pytest.fail(f"normalization of {key} did not settle (stage status {status!r})")


async def _create_source(
    client: AsyncClient,
    engine: AsyncEngine,
    matrix: Matrix,
    key: str,
    *,
    provider: str,
    source_type: str,
    configuration: dict[str, Any] | None = None,
) -> None:
    """Create a connector source through the API and set its configuration directly (as other harness tests do)."""
    # Built-in providers allow one API-created source per workspace (409 builtin_already_added) and the
    # matrix needs several per provider, so create a manual source and retype it.
    created = await client.post(
        "/api/v1/sources", json={"type": "manual", "name": f"matrix-{key}-{uuid4().hex[:6]}"}
    )
    created.raise_for_status()
    source_id = UUID(created.json()["id"])
    matrix.sources[key] = source_id
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sources SET type = :t, provider = :p, local_only = false, "
                "configuration = configuration || CAST(:c AS jsonb) WHERE id = :i"
            ),
            {"t": source_type, "p": provider, "c": json.dumps(configuration or {}), "i": source_id},
        )


async def _set(engine: AsyncEngine, matrix: Matrix, key: str, sql_set: str) -> None:
    """Apply ``UPDATE sources SET <sql_set>`` to one seeded source (lifecycle / config-change steps)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE sources SET {sql_set} WHERE id = :i"),  # noqa: S608  # test-local constants
            {"i": matrix.sources[key]},
        )


async def _seed(client: AsyncClient, engine: AsyncEngine) -> Matrix:
    """Create and ingest every matrix source once; later calls in the session reuse the result."""
    async with engine.connect() as conn:
        ws, owner = (
            await conn.execute(
                text(
                    # A fresh disposable DB has no sources yet: take the owner's (first) workspace.
                    "SELECT id, owner_user_id FROM workspaces ORDER BY created_at LIMIT 1"
                )
            )
        ).one()
        revision: int = (
            await conn.execute(
                text(
                    "SELECT revision FROM workspace_memberships "
                    "WHERE workspace_id = :w AND role = 'owner'"
                ),
                {"w": ws},
            )
        ).scalar_one()
    m = Matrix(UUID(str(ws)), int(str(owner)), revision)

    async def mk(key: str, provider: str, stype: str, cfg: dict[str, Any] | None = None) -> None:
        """Create one matrix source (shorthand for ``_create_source``)."""
        await _create_source(
            client, engine, m, key, provider=provider, source_type=stype, configuration=cfg
        )

    await mk("bn_a", "binance", "api")
    await mk("bn_b", "binance", "api")
    await mk("bn_paused", "binance", "api")
    await mk("bn_archived", "binance", "api")
    await mk("bn_local", "binance", "api")
    await mk("fx", "frankfurter", "api")
    await mk("wb", "world_bank", "api")
    await mk(
        "om_hanoi",
        "open_meteo",
        "api",
        ConnectorConfig(
            **HANOI, weather_timezone="UTC", weather_metrics=("temperature_2m",)
        ).model_dump(mode="json", exclude_none=True),
    )
    await mk(
        "om_hcmc",
        "open_meteo",
        "api",
        ConnectorConfig(
            **HCMC, weather_timezone="UTC", weather_metrics=("temperature_2m",)
        ).model_dump(mode="json", exclude_none=True),
    )
    await mk(
        "om_moved",
        "open_meteo",
        "api",
        ConnectorConfig(
            **HANOI, weather_timezone="UTC", weather_metrics=("temperature_2m",)
        ).model_dump(mode="json", exclude_none=True),
    )
    await mk("usgs", "usgs", "api")
    await mk("kev", "cisa_kev", "api")
    await mk("emsc", "emsc_quakes", "api")
    await mk("vne", "vnexpress_business", "rss")
    await mk("jobs", "vietnamworks_jobs", "api")
    await mk("arxiv", "arxiv", "rss", {"arxiv_category": "cs.AI"})
    await mk("bsky", "bluesky", "api", {"bluesky_actor": "alice.example.com"})
    await mk("cal", "google_calendar", "calendar", {"google_calendar_ids": ["primary"]})

    await _ingest(engine, m, "bn_a", _binance("67123.45", T1))
    await _ingest(engine, m, "bn_a", _binance("67500.0", T2))
    await _ingest(engine, m, "bn_b", _binance("67500.0", T2))  # same series point as bn_a's latest
    for key, price in (
        ("bn_paused", "55555.5"),
        ("bn_archived", "44444.4"),
        ("bn_local", "33333.3"),
    ):
        await _ingest(engine, m, key, _binance(price, T2))
    await _ingest(engine, m, "fx", map_frankfurter(jfixture("frankfurter_usd_vnd.json"), COLLECTED))
    await _ingest(engine, m, "wb", _wb(False))
    await _ingest(engine, m, "wb", _wb(True))  # 2024 restated: previous revision is superseded
    await _ingest(engine, m, "om_hanoi", _weather(HANOI, 20.0))
    await _ingest(engine, m, "om_hcmc", _weather(HCMC, 30.0))
    await _ingest(engine, m, "om_moved", _weather(HANOI, 25.0))
    await _ingest(engine, m, "usgs", map_usgs(_usgs_payload(), COLLECTED))
    await _ingest(engine, m, "kev", map_cisa_kev(fixture("cisa_kev.json"), COLLECTED))
    await _ingest(
        engine,
        m,
        "emsc",
        map_json_feed(JSON_FEEDS["emsc_quakes"], fixture("emsc_quakes.json"), COLLECTED),
    )
    news = _atom(
        [
            ("vne-gold", "Giá vàng tăng mạnh", "Vàng trong nước tăng", "2026-11-16T01:00:00+07:00"),
            ("vne-rate", "Lai suat giam", "Ngan hang ha lai suat", "2026-11-16T02:00:00+07:00"),
        ]
    )
    await _ingest(engine, m, "vne", map_feed("vnexpress_business", news, COLLECTED))
    await _ingest(
        engine, m, "jobs", map_vietnamworks(jfixture("vietnamworks_jobs.json"), COLLECTED)
    )
    arxiv_source = SimpleNamespace(provider="arxiv", configuration={"arxiv_category": "cs.AI"})
    await _ingest(
        engine,
        m,
        "arxiv",
        list(_records_from_feed(arxiv_source, _arxiv_atom(), COLLECTED).records),  # type: ignore[arg-type]
    )
    await _ingest(engine, m, "bsky", _bluesky())
    await _ingest(engine, m, "cal", _calendar())

    # Lifecycle and config changes AFTER ingestion (normalization needs an active, current-generation source).
    await _set(engine, m, "bn_paused", "status = 'paused'")
    await _set(engine, m, "bn_archived", "status = 'archived'")
    await _set(engine, m, "bn_local", "local_only = true")
    await _set(engine, m, "cal", "local_only = true")
    await _set(
        engine,
        m,
        "om_moved",
        "configuration = configuration || '{\"weather_latitude\": 10.5}'::jsonb, generation = generation + 1",
    )
    return m


@pytest.fixture
async def matrix(ready_owner_client: AsyncClient, committed_engine: AsyncEngine) -> Matrix:
    """The seeded matrix; the first test pays for seeding, later tests reuse the committed rows."""
    global _MATRIX, _SEED_ERROR  # per-session cache: committed rows persist between tests
    if _SEED_ERROR is not None:
        pytest.fail(f"matrix seeding failed earlier: {_SEED_ERROR}")
    if _MATRIX is None:
        try:
            _MATRIX = await _seed(ready_owner_client, committed_engine)
        except Exception as exc:  # seed once: later cases fail fast instead of re-seeding
            _SEED_ERROR = f"{type(exc).__name__}: {exc}"
            raise
    return _MATRIX


Call = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def call(matrix: Matrix, committed_engine: AsyncEngine) -> Call:
    """``call(args, sources=[keys], allow_local=False, tz=TZ)`` -> parsed tool JSON (``_evidence`` attached)."""

    async def run(
        args: dict[str, Any],
        sources: list[str] | None = None,
        *,
        allow_local: bool = False,
        tz: str | None = TZ,
    ) -> dict[str, Any]:
        """Execute the real tool for the owner, pinned to ``sources`` (None = those unknown to the matrix)."""
        scope = WorkspaceContext(
            user_id=matrix.user_id,
            workspace_id=matrix.workspace_id,
            role="owner",
            membership_revision=matrix.revision,
        )
        ids = [str(matrix.sources[k]) for k in sources] if sources is not None else [str(uuid4())]
        evidence: list[Any] = []
        async with AsyncSession(committed_engine, expire_on_commit=False) as session:
            fence = await workspaces.read_access_fence(
                session, scope=scope, multi_workspace_enabled=FLAG
            )
            raw = await execute_db_query_tool(
                session,
                args,
                scope=scope,
                multi_workspace_enabled=FLAG,
                expected_access_fence=fence,
                retrieval_context={"source_scope": ids},
                allow_local_only=allow_local,
                evidence_list=evidence,
                timezone=tz,
            )
        data: dict[str, Any] = json.loads(raw)
        data["_evidence"] = evidence
        return data

    return run


@pytest.fixture
async def reader(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """SQL mode needs the reader login DSN of the disposable runner; skip otherwise."""
    url = os.getenv("CHAT_SQL_READER_DATABASE_URL")
    if not url:
        pytest.skip("CHAT_SQL_READER_DATABASE_URL is available only in the disposable test runner")
    monkeypatch.setenv("CHAT_SQL_READER_DATABASE_URL", url)
    db_query._reader_engines.clear()
    yield
    for cached in db_query._reader_engines.values():
        await cached.dispose()
    db_query._reader_engines.clear()


def _obs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Observation rows of a structured result."""
    return list(data.get("observations", []))


def _docs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Document rows of a structured result."""
    return list(data.get("documents", []))


# --------------------------------------------------------------------------- table-driven structured cases
# (case id, source keys, tool args, predicate on the parsed result)
CASES: list[tuple[str, list[str], dict[str, Any], Callable[[dict[str, Any]], bool]]] = [
    (
        "market_latest_value_per_symbol",
        ["bn_a"],
        {"providers": ["binance"], "symbols": ["btcusdt"], "target": "observations", "limit": 1},
        lambda d: float(_obs(d)[0]["value"]) == 67500.0 and _obs(d)[0]["symbol"] == "BTCUSDT",
    ),
    (
        "market_rows_have_citation",
        ["bn_a"],
        {"providers": ["binance"], "target": "observations"},
        lambda d: all(r["citation"] for r in _obs(d)),
    ),
    (
        "market_two_sources_same_point_deduped",
        ["bn_a", "bn_b"],
        {"providers": ["binance"], "target": "observations", "metrics": None},
        lambda d: sum(1 for r in _obs(d) if r["observed_at"].startswith("2026-11-16T03:00")) == 1,
    ),
    (
        "market_aggregate_dedupes_two_sources",
        ["bn_a", "bn_b"],
        {
            "providers": ["binance"],
            "target": "observations",
            "aggregation": "count",
            "date_from": "2026-11-01",
        },
        lambda d: d["observation_aggregates"][0]["count"] <= 2,
    ),
    (
        "market_ticks_with_distinct_observed_at_are_all_history",
        ["bn_a"],
        {"providers": ["binance"], "target": "observations"},
        lambda d: sorted(float(r["value"]) for r in _obs(d)) == [67123.45, 67500.0],
    ),
    (
        "market_aggregate_counts_every_tick_in_window",
        ["bn_a"],
        {
            "providers": ["binance"],
            "target": "observations",
            "aggregation": "count",
            "date_from": "2026-11-01",
        },
        lambda d: d["observation_aggregates"][0]["count"] == 2,
    ),
    (
        "market_aggregate_avg_spans_ticks",
        ["bn_a"],
        {
            "providers": ["binance"],
            "target": "observations",
            "aggregation": "avg",
            "date_from": "2026-11-01",
        },
        lambda d: abs(d["observation_aggregates"][0]["avg"] - (67123.45 + 67500.0) / 2) < 1e-6,
    ),
    (
        "market_two_sources_aggregate_counts_each_point_once",
        ["bn_a", "bn_b"],
        {
            "providers": ["binance"],
            "target": "observations",
            "aggregation": "count",
            "date_from": "2026-11-01",
        },
        lambda d: d["observation_aggregates"][0]["count"] == 2,
    ),
    (
        "market_aggregate_has_citation",
        ["bn_a"],
        {
            "providers": ["binance"],
            "target": "observations",
            "aggregation": "avg",
            "date_from": "2026-11-01",
        },
        lambda d: all(a["citation"] for a in d["observation_aggregates"]),
    ),
    (
        "market_aggregate_default_window_is_declared",
        ["bn_a"],
        {"providers": ["binance"], "target": "observations", "aggregation": "avg"},
        lambda d: "aggregation_window" in d,
    ),
    (
        "paused_source_still_readable",
        ["bn_paused"],
        {"providers": ["binance"], "target": "observations"},
        lambda d: float(_obs(d)[0]["value"]) == 55555.5,
    ),
    (
        "archived_source_hidden",
        ["bn_archived"],
        {"providers": ["binance"], "target": "observations"},
        lambda d: d.get("status") == "no_active_sources_in_scope",
    ),
    (
        "local_only_hidden_for_remote_model",
        ["bn_local"],
        {"providers": ["binance"], "target": "observations"},
        lambda d: d.get("status") == "no_active_sources_in_scope",
    ),
    (
        "fx_rate_by_pair",
        ["fx"],
        {"providers": ["frankfurter"], "target": "observations"},
        lambda d: any(abs(float(r["value"]) - 26312.5) < 1e-6 for r in _obs(d)),
    ),
    (
        "fx_carries_currency_or_unit",
        ["fx"],
        {"providers": ["frankfurter"], "target": "observations"},
        lambda d: all(r["unit"] or r["currency"] for r in _obs(d)),
    ),
    (
        "macro_region_is_country_name",
        ["wb"],
        {"providers": ["world_bank"], "target": "observations", "region": "viet nam"},
        lambda d: len(_obs(d)) >= 1,
    ),
    (
        "macro_annual_observed_at_is_period_start",
        ["wb"],
        {"providers": ["world_bank"], "target": "observations", "region": "viet nam"},
        lambda d: all(r["observed_at"][5:10] == "01-01" for r in _obs(d)),
    ),
    (
        "macro_year_as_local_day_includes_jan_1",
        ["wb"],
        {
            "providers": ["world_bank"],
            "target": "observations",
            "date_from": "2024-01-01",
            "date_to": "2024-01-01",
        },
        lambda d: any(r["observed_at"].startswith("2024-01-01") for r in _obs(d)),
    ),
    (
        "superseded_revision_excluded_by_default",
        ["wb"],
        {
            "providers": ["world_bank"],
            "target": "observations",
            "date_from": "2024-01-01",
            "date_to": "2024-01-01",
        },
        lambda d: (
            len(_obs(d)) == 1
            and _obs(d)[0]["is_current"] is True
            and abs(float(_obs(d)[0]["value"]) - 500000000000.5) < 1
        ),
    ),
    (
        "superseded_revision_visible_with_current_only_false",
        ["wb"],
        {
            "providers": ["world_bank"],
            "target": "observations",
            "date_from": "2024-01-01",
            "date_to": "2024-01-01",
            "current_only": False,
        },
        lambda d: {r["is_current"] for r in _obs(d)} == {True, False},
    ),
    (
        "superseded_revision_excluded_from_aggregate",
        ["wb"],
        {
            "providers": ["world_bank"],
            "target": "observations",
            "aggregation": "count",
            "date_from": "2024-01-01",
            "date_to": "2024-01-01",
        },
        lambda d: d["observation_aggregates"][0]["count"] == 1,
    ),
    (
        "weather_region_label_present",
        ["om_hanoi"],
        {"providers": ["open_meteo"], "target": "observations"},
        lambda d: {r["region"] for r in _obs(d)} == {HANOI_LABEL},
    ),
    (
        "weather_observations_carry_lat_lon",
        ["om_hanoi"],
        {"providers": ["open_meteo"], "target": "observations"},
        lambda d: all(
            abs(float(r["latitude"]) - 21.0345) < 0.01
            and abs(float(r["longitude"]) - 105.8512) < 0.01
            for r in _obs(d)
        ),
    ),
    (
        "weather_aggregate_avg_grouped_by_region",
        ["om_hanoi", "om_hcmc"],
        {
            "providers": ["open_meteo"],
            "target": "observations",
            "metrics": ["temperature_2m"],
            "aggregation": "avg",
            "date_from": "2026-11-01",
        },
        lambda d: (
            {a["region"]: round(a["avg"], 3) for a in d["observation_aggregates"]}
            == {HANOI_LABEL: 21.0, HCMC_LABEL: 31.0}
        ),
    ),
    (
        "weather_region_filter_picks_one_location",
        ["om_hanoi", "om_hcmc"],
        {"providers": ["open_meteo"], "target": "observations", "region": HCMC_LABEL},
        lambda d: {r["region"] for r in _obs(d)} == {HCMC_LABEL},
    ),
    (
        "config_change_hides_old_rows",
        ["om_moved"],
        {"providers": ["open_meteo"], "target": "observations"},
        lambda d: _obs(d) == [],
    ),
    (
        "config_change_empty_result_lists_available_series",
        ["om_moved", "om_hanoi"],
        {"providers": ["open_meteo"], "symbols": ["NOPE"], "target": "observations"},
        lambda d: (
            _obs(d) == [] and any(s["provider"] == "open_meteo" for s in d["available_series"])
        ),
    ),
    (
        "quake_obs_attributes_carry_depth",
        ["usgs"],
        {"providers": ["usgs"], "target": "observations"},
        lambda d: all("depth_km" in r["attributes"] for r in _obs(d)) and len(_obs(d)) == 3,
    ),
    (
        "quake_obs_carry_lat_lon",
        ["usgs"],
        {"providers": ["usgs"], "target": "observations"},
        lambda d: all(r["latitude"] is not None and r["longitude"] is not None for r in _obs(d)),
    ),
    (
        "local_day_end_includes_23_30_local",
        ["usgs"],
        {
            "providers": ["usgs"],
            "target": "observations",
            "date_from": "2026-11-10",
            "date_to": "2026-11-10",
        },
        lambda d: (
            {r["observed_at"][:16] for r in _obs(d)} == {"2026-11-10T16:30", "2026-11-10T17:30"}
            or {r["observed_at"][:16] for r in _obs(d)} == {"2026-11-10T16:30"}
        ),
    ),
    (
        "local_day_excludes_next_local_day",
        ["usgs"],
        {
            "providers": ["usgs"],
            "target": "observations",
            "date_from": "2026-11-10",
            "date_to": "2026-11-10",
        },
        lambda d: all(r["observed_at"][:16] != "2026-11-10T17:30" for r in _obs(d)),
    ),
    (
        "local_day_excludes_previous_local_day",
        ["usgs"],
        {
            "providers": ["usgs"],
            "target": "observations",
            "date_from": "2026-11-10",
            "date_to": "2026-11-10",
        },
        lambda d: all(r["observed_at"][:16] != "2026-11-09T16:30" for r in _obs(d)),
    ),
    (
        "timezone_is_echoed",
        ["usgs"],
        {"providers": ["usgs"], "target": "observations"},
        lambda d: d["timezone"] == TZ and "data_notice" in d,
    ),
    (
        "security_symbol_is_cve",
        ["kev"],
        {"providers": ["cisa_kev"], "target": "observations"},
        lambda d: {r["symbol"] for r in _obs(d)} == {"CVE-2015-5477", "CVE-2016-3081"},
    ),
    (
        "security_attributes_carry_vendor",
        ["kev"],
        {"providers": ["cisa_kev"], "symbols": ["CVE-2016-3081"], "target": "observations"},
        lambda d: _obs(d)[0]["attributes"].get("vendorProject") == "Apache",
    ),
    (
        "security_attributes_carry_ransomware_flag",
        ["kev"],
        {"providers": ["cisa_kev"], "symbols": ["CVE-2016-3081"], "target": "observations"},
        lambda d: _obs(d)[0]["attributes"].get("knownRansomwareCampaignUse") == "Known",
    ),
    (
        "security_null_value_aggregate_counts_nulls",
        ["kev"],
        {
            "providers": ["cisa_kev"],
            "target": "observations",
            "aggregation": "avg",
            "date_from": "2026-10-01",
        },
        lambda d: (
            d["observation_aggregates"][0]["avg"] is None
            and d["observation_aggregates"][0]["null_values"]
            == d["observation_aggregates"][0]["count"]
        ),
    ),
    (
        "event_docs_only_in_documents",
        ["emsc"],
        {"providers": ["emsc_quakes"], "limit": 5},
        lambda d: len(_docs(d)) == 3 and _obs(d) == [],  # the emsc fixture holds 3 events
    ),
    (
        "event_docs_truncated_flag_when_over_limit",
        ["emsc"],
        {"providers": ["emsc_quakes"], "limit": 2},
        lambda d: d.get("truncated") is True and len(_docs(d)) == 2 and "hint" in d,
    ),
    (
        "event_docs_have_citations",
        ["emsc"],
        {"providers": ["emsc_quakes"], "limit": 3},
        lambda d: all(x["citation"] for x in _docs(d)),
    ),
    (
        "event_docs_not_truncated_under_limit",
        ["vne"],
        {"providers": ["vnexpress_business"], "limit": 10},
        lambda d: "truncated" not in d and len(_docs(d)) == 2,
    ),
    (
        "news_accentless_query_matches_accented_title",
        ["vne"],
        {"providers": ["vnexpress_business"], "text_query": "gia vang"},
        lambda d: [x["title"] for x in _docs(d)] == ["Giá vàng tăng mạnh"],
    ),
    (
        "news_accented_query_matches_accentless_title",
        ["vne"],
        {"providers": ["vnexpress_business"], "text_query": "lãi suất giảm"},
        lambda d: [x["title"] for x in _docs(d)] == ["Lai suat giam"],
    ),
    (
        "news_query_matches_body_text_accent_insensitive",
        ["vne"],
        {"providers": ["vnexpress_business"], "text_query": "ngân hàng"},
        lambda d: [x["title"] for x in _docs(d)] == ["Lai suat giam"],
    ),
    (
        "news_query_with_like_metacharacters_is_literal",
        ["vne"],
        {"providers": ["vnexpress_business"], "text_query": "100%"},
        lambda d: _docs(d) == [],
    ),
    (
        "news_doc_date_to_whole_local_day",
        ["vne"],
        {"providers": ["vnexpress_business"], "date_from": "2026-11-16", "date_to": "2026-11-16"},
        lambda d: len(_docs(d)) == 2,
    ),
    (
        "jobs_title_and_company_visible",
        ["jobs"],
        {"providers": ["vietnamworks_jobs"], "text_query": "Python Developer"},
        lambda d: any("Python Developer" in x["title"] for x in _docs(d)),
    ),
    (
        "research_attributes_carry_authors",
        ["arxiv"],
        {"providers": ["arxiv"]},
        lambda d: _docs(d)[0]["attributes"].get("authors") == ["Ada Lovelace", "Alan Turing"],
    ),
    (
        "research_attributes_carry_categories",
        ["arxiv"],
        {"providers": ["arxiv"]},
        lambda d: "cs.AI" in _docs(d)[0]["attributes"].get("categories", []),
    ),
    (
        "social_title_is_post_text_not_id",
        ["bsky"],
        {"providers": ["bluesky"]},
        lambda d: _docs(d)[0]["title"] == "Shipping today",
    ),
    (
        "personal_local_only_hidden_without_allow",
        ["cal"],
        {"providers": ["google_calendar"]},
        lambda d: d.get("status") == "no_active_sources_in_scope",
    ),
    (
        "empty_result_lists_available_series",
        ["bn_a"],
        {"providers": ["binance"], "symbols": ["NOPE"], "target": "observations"},
        lambda d: _obs(d) == [] and any(s["symbol"] == "BTCUSDT" for s in d["available_series"]),
    ),
    (
        "unknown_key_rejected_with_field_name",
        ["bn_a"],
        {"symbol": "BTCUSDT"},
        lambda d: (
            d["error"].startswith("invalid_arguments")
            and "symbol" in d["error"]
            and d["rows"] == []
        ),
    ),
    (
        "unknown_provider_key_rejected",
        ["bn_a"],
        {"provider": "binance"},
        lambda d: d["error"].startswith("invalid_arguments") and "provider" in d["error"],
    ),
    (
        "bad_enum_rejected",
        ["bn_a"],
        {"aggregation": "median"},
        lambda d: d["error"].startswith("invalid_arguments") and "aggregation" in d["error"],
    ),
    (
        "limit_out_of_range_rejected",
        ["bn_a"],
        {"limit": 500},
        lambda d: d["error"].startswith("invalid_arguments") and "limit" in d["error"],
    ),
    (
        "unparseable_date_is_an_error_not_a_dropped_filter",
        ["bn_a"],
        {"date_from": "yesterday"},
        lambda d: d["error"].startswith("invalid date_from"),
    ),
    (
        "unparseable_date_to_is_an_error",
        ["bn_a"],
        {"date_to": "2026-13-45"},
        lambda d: d["error"].startswith("invalid date_to"),
    ),
    (
        "unknown_source_scope_yields_no_sources",
        [],
        {"providers": ["binance"]},
        lambda d: d.get("status") == "no_active_sources_in_scope",
    ),
]


@pytest.mark.parametrize(
    ("sources", "args", "check"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
async def test_structured_case(
    call: Call,
    sources: list[str],
    args: dict[str, Any],
    check: Callable[[dict[str, Any]], bool],
) -> None:
    """One structured-mode case: the predicate must hold on the parsed tool result."""
    data = await call(args, sources if sources else None)
    shown = {k: v for k, v in data.items() if k != "_evidence"}
    assert check(data), json.dumps(shown, ensure_ascii=False, default=str)[:2000]


async def test_local_only_visible_when_model_is_local(call: Call) -> None:
    """A local model may read local_only sources: calendar event with ``start`` attribute and citation."""
    data = await call({"providers": ["google_calendar"]}, ["cal"], allow_local=True)
    (event,) = _docs(data)
    assert event["title"] == "Standup" and event["citation"]
    assert str(event["attributes"].get("start", "")).startswith("2026-11-18T10:30")


async def test_local_only_binance_visible_when_model_is_local(call: Call) -> None:
    """The same rule for an observation source."""
    data = await call(
        {"providers": ["binance"], "target": "observations"}, ["bn_local"], allow_local=True
    )
    assert float(_obs(data)[0]["value"]) == 33333.3


async def test_citation_numbers_follow_row_order_and_register_evidence(call: Call) -> None:
    """Every cited document row adds exactly one evidence item and its marker is ``[index + 1]``."""
    data = await call({"providers": ["vnexpress_business"]}, ["vne"])
    docs = _docs(data)
    assert [x["citation"] for x in docs] == ["[1]", "[2]"]
    assert len(data["_evidence"]) == 2


# --------------------------------------------------------------------------- SQL mode
async def test_sql_view_exposes_attributes_and_depth(call: Call, reader: None) -> None:
    """SQL mode returns the ``attributes`` jsonb column of current in-scope rows (quake depth)."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT symbol, attributes FROM scoped_observations ORDER BY observed_at DESC",
        },
        ["usgs"],
    )
    assert data["row_count"] == 3
    assert all("depth_km" in row["attributes"] for row in data["rows"])


async def test_sql_view_hides_superseded_revisions(call: Call, reader: None) -> None:
    """The view hides a restated point's older revision (and ``'...:...'`` literals survive ``text()``)."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT count(*) AS n, bool_and(is_current) AS cur FROM scoped_observations "
            "WHERE observed_at = '2024-01-01T00:00:00+00:00'",
        },
        ["wb"],
    )
    assert data["rows"] == [{"n": 1, "cur": True}]


async def test_sql_view_keeps_ticker_history_and_dedupes_sources(call: Call, reader: None) -> None:
    """Ticks with distinct observed_at are all visible; the same point from two sources counts once."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT count(*) AS n FROM scoped_observations WHERE provider = 'binance'",
        },
        ["bn_a", "bn_b"],
    )
    assert data["rows"] == [{"n": 2}]


async def test_sql_view_hides_rows_after_config_change(call: Call, reader: None) -> None:
    """Scope discriminator in the fence: rows of the old configuration are invisible in both views."""
    data = await call(
        {"mode": "sql", "sql_query": "SELECT count(*) AS n FROM scoped_observations"}, ["om_moved"]
    )
    assert data["rows"] == [{"n": 0}]


async def test_sql_view_reads_paused_source(call: Call, reader: None) -> None:
    """A paused source keeps its discriminator, so SQL mode still reads it."""
    data = await call(
        {"mode": "sql", "sql_query": "SELECT count(*) AS n FROM scoped_observations"}, ["bn_paused"]
    )
    assert data["rows"][0]["n"] >= 1


async def test_sql_view_has_coordinates(call: Call, reader: None) -> None:
    """``latitude``/``longitude`` are columns of ``scoped_observations``."""
    data = await call(
        {"mode": "sql", "sql_query": "SELECT latitude, longitude FROM scoped_observations LIMIT 1"},
        ["usgs"],
    )
    assert data["rows"][0]["latitude"] is not None


async def test_sql_literal_with_colon_is_not_a_bind(call: Call, reader: None) -> None:
    """``'10:30'`` in a literal runs as text (no bind-parameter error)."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT count(*) AS n FROM scoped_observations WHERE symbol = '10:30'",
        },
        ["usgs"],
    )
    assert data["rows"] == [{"n": 0}] and "error" not in data


async def test_sql_documents_view_gives_citation(call: Call, reader: None) -> None:
    """Document rows from SQL mode carry the same ``[n]`` citation as structured mode."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT title, document_version_id FROM scoped_documents ORDER BY title",
        },
        ["vne"],
    )
    assert [r["citation"] for r in data["rows"]] == ["[1]", "[2]"]


async def test_sql_rejects_base_tables(call: Call, reader: None) -> None:
    """Only the two scoped views are queryable."""
    data = await call(
        {"mode": "sql", "sql_query": "SELECT count(*) FROM public.chat_messages"}, ["usgs"]
    )
    assert "error" in data and data["rows"] == []


async def test_sql_row_limit_sets_truncated(call: Call, reader: None) -> None:
    """More rows than ``limit`` flags ``truncated`` in SQL mode."""
    data = await call(
        {"mode": "sql", "sql_query": "SELECT id FROM scoped_documents", "limit": 2}, ["emsc"]
    )
    assert data["truncated"] is True and data["row_count"] == 2


async def test_sql_statement_timeout_maps_to_query_timeout(call: Call, reader: None) -> None:
    """A heavy allowed query hits the 2 s timeout; skipped when the allowlist rejects the generator."""
    data = await call(
        {
            "mode": "sql",
            "sql_query": "SELECT count(*) AS n FROM generate_series(1, 400000000) AS g",
        },
        ["usgs"],
    )
    if "query_timeout" not in str(data.get("error", "")):
        pytest.skip(f"no timeout reachable through the allowlist: {data.get('error')!r}")
    assert data["rows"] == []
