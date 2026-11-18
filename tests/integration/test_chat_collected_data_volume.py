"""Opt-in volume benchmark of the chat ``query_collected_data`` tool on real PostgreSQL (Docker harness).

Plan "Buoc 8". Seeds ``CHAT_VOLUME_ROWS`` observations (default 2,000,000) over 20 connector sources
(some pairs share series so the cross-source dedup does real work, ~10% of points carry a superseded
revision) spread over one year, plus 50,000 documents/chunks of mixed accented Vietnamese text for the
unaccent search. Seeding is set-based SQL. Each of four representative calls runs through the real tool
(1 warm-up, then 5 measured runs); ``EXPLAIN (ANALYZE, BUFFERS)`` is taken from the statements the tool
really sent. Timings and plans are written to ``test-results/chat-volume/``.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import WorkspaceContext
from modules.chat import db_query
from modules.chat._db_scope import resolve_source_scopes
from modules.chat.db_query import execute_db_query_tool
from modules.knowledge.documents.schemas import ProviderRecordMetadata

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
    ),
]

ROWS = int(os.getenv("CHAT_VOLUME_ROWS", "2000000"))
DOC_ROWS = int(os.getenv("CHAT_VOLUME_DOCS", "50000"))
OUT_DIR = Path("test-results/chat-volume")
GIN_INDEX = "ix_document_chunks_content_unaccent_tsv"
RUNS = 5
CEILING_SECONDS = 2.0
SYMBOLS_PER_SOURCE = 5
# 20 observation sources; consecutive pairs (k, k+1) of one provider share their series keys.
OBS_PROVIDERS = ["binance"] * 8 + ["frankfurter"] * 6 + ["usgs"] * 3 + ["cisa_kev"] * 3

# One declared measurement field per provider, so the attributes view has something to read.
_VOLUME_FIELDS: dict[str, dict[str, str]] = {
    "binance": {"symbol": "VOL"},
    "frankfurter": {"base": "USD"},
    "usgs": {"place": "vol"},
    "cisa_kev": {"product": "vol"},
}


def _provider_record(provider: str) -> dict[str, Any]:
    """A contract-valid ``provider_record`` (validated by the real model), as normalization would persist it.

    The rows are inserted with raw SQL, so an invalid record would only fail later in workers that read it
    (the news ready consumer dead-lettered every such version and starved the other queues).
    """
    return ProviderRecordMetadata.model_validate(
        {
            "provider": provider,
            "identity": f"{provider}:volume",
            "timestamp_basis": "collection",
            "coverage": "returned_snapshot",
            "content_truncated": False,
            "world_data": {
                "provider": provider,
                "metric": "volume_fixture",
                "value": 1.0,
                "unit": "count",
                "quality": "provider_reported",
                "provider_fields": _VOLUME_FIELDS[provider],
            },
        }
    ).model_dump(mode="json", exclude_none=True)


DOC_PROVIDER = "vnexpress_business"
PHRASES = [
    "Giá vàng tăng mạnh",
    "Lãi suất ngân hàng giảm",
    "Thị trường chứng khoán biến động",
    "Tỷ giá USD VND ổn định",
    "Bão số 5 đổ bộ miền Trung",
    "Giá xăng dầu điều chỉnh",
    "Bất động sản phục hồi",
    "Xuất khẩu gạo tăng trưởng",
    "Lạm phát tháng 10 hạ nhiệt",
    "Doanh nghiệp công nghệ mở rộng",
    "Vàng SJC giảm sâu",
    "Ngân hàng nhà nước điều hành tỷ giá",
    "Giá cà phê lập kỷ lục",
    "Gia vang the gioi",
]


async def _create_sources(client: AsyncClient, engine: AsyncEngine) -> dict[str, Any]:
    """Create the 20 observation sources and 2 feed sources through the API; return ids and owner context."""
    tag = uuid4().hex[:6]
    specs = [(f"obs{k}", p, "api") for k, p in enumerate(OBS_PROVIDERS)]
    specs += [(f"doc{k}", DOC_PROVIDER, "rss") for k in range(2)]
    ids: dict[str, UUID] = {}
    for key, _provider, _stype in specs:
        # Built-in providers allow one API-created source per workspace (409), so create manual sources and
        # retype them below.
        created = await client.post(
            "/api/v1/sources", json={"type": "manual", "name": f"vol-{key}-{tag}"}
        )
        created.raise_for_status()
        ids[key] = UUID(created.json()["id"])
    async with engine.begin() as conn:
        for key, provider, stype in specs:
            await conn.execute(
                text(
                    "UPDATE sources SET type = :t, provider = :p, local_only = false WHERE id = :i"
                ),
                {"t": stype, "p": provider, "i": ids[key]},
            )
        # Retention cron must not purge the year-old seed rows mid-benchmark.
        await conn.execute(
            text(
                "UPDATE sources SET configuration = configuration || "
                "jsonb_build_object('retention_days', 3650) WHERE id = ANY(:ids)"
            ),
            {"ids": list(ids.values())},
        )
        ws, owner = (
            await conn.execute(
                text(
                    "SELECT w.id, w.owner_user_id FROM workspaces w JOIN sources s "
                    "ON s.workspace_id = w.id WHERE s.id = :i"
                ),
                {"i": ids["obs0"]},
            )
        ).one()
        revision = (
            await conn.execute(
                text(
                    "SELECT revision FROM workspace_memberships "
                    "WHERE workspace_id = :w AND role = 'owner'"
                ),
                {"w": ws},
            )
        ).scalar_one()
    scope = WorkspaceContext(
        user_id=int(owner), workspace_id=UUID(str(ws)), role="owner", membership_revision=revision
    )
    return {"ids": ids, "scope": scope, "tag": tag}


async def _discriminators(engine: AsyncEngine, seeded: dict[str, Any]) -> dict[UUID, str | None]:
    """Current scope discriminator per source, from the same resolver the tool uses."""
    ids: dict[str, UUID] = seeded["ids"]
    allowed = [(sid, None, key) for key, sid in ids.items()]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        _kept, scopes = await resolve_source_scopes(
            session, allowed, scope=seeded["scope"], multi_workspace_enabled=False
        )
    missing = [k for k, sid in ids.items() if scopes.get(sid) is None]
    assert not missing, f"sources without a scope discriminator would hide every row: {missing}"
    return scopes


async def _seed_observations(
    engine: AsyncEngine, seeded: dict[str, Any], scopes: dict[UUID, str | None]
) -> int:
    """Insert document/version/provenance/raw rows per source, then the observations with generate_series."""
    ids: dict[str, UUID] = seeded["ids"]
    ws = seeded["scope"].workspace_id
    per_source = ROWS // len(OBS_PROVIDERS)
    # 10% of points carry an extra superseded revision, hence the 1.1 factor.
    points = max(2, int(per_source / (SYMBOLS_PER_SOURCE * 1.1)))
    step_s = 365 * 86400 / points
    start = datetime.now(UTC) - timedelta(days=365)
    total = 0
    for k, provider in enumerate(OBS_PROVIDERS):
        src, disc = ids[f"obs{k}"], scopes[ids[f"obs{k}"]]
        batch, raw, raw1, doc, ver = (uuid4() for _ in range(5))
        prov = json.dumps(
            {
                "provider_scope_discriminator": disc,
                "provider_record": _provider_record(provider),
            }
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingestion_batches (id, source_id, batch_key, payload_hash) "
                    "VALUES (:b, :s, :bk, :bk)"
                ),
                {"b": batch, "s": src, "bk": uuid4().hex + uuid4().hex},
            )
            for raw_id in (
                raw,
                raw1,
            ):  # one raw row per revision (unique per source/external id/raw)
                await conn.execute(
                    text(
                        "INSERT INTO source_observations (id, source_id, batch_id, provider_id, "
                        "record_hash, payload, observed_at) "
                        "VALUES (:r, :s, :b, 'vol', :h, '{}'::jsonb, now())"
                    ),
                    {"r": raw_id, "s": src, "b": batch, "h": uuid4().hex + uuid4().hex},
                )
            await conn.execute(
                text(
                    "INSERT INTO documents (workspace_id, id, source_id, external_id, title, "
                    "current_version, content_hash, observed_at) "
                    "VALUES (:w, :d, :s, :e, 'volume series', 1, :h, now())"
                ),
                {"w": ws, "d": doc, "s": src, "e": f"vol-{uuid4().hex}", "h": uuid4().hex},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_versions (id, document_id, version_number, content, "
                    "content_hash) VALUES (:v, :d, 1, 'volume series', :h)"
                ),
                {"v": ver, "d": doc, "h": uuid4().hex},
            )
            await conn.execute(
                text(
                    "INSERT INTO normalized_version_provenance (id, document_id, document_version_id, "
                    "provider_id, accepted_record_hash, normalization_version, source_generation, "
                    "observed_at, selection_observed_at, title, provenance) VALUES "
                    "(:i, :d, :v, 'vol', :h, 1, 1, now(), now(), 'volume series', CAST(:p AS jsonb))"
                ),
                {"i": uuid4(), "d": doc, "v": ver, "h": uuid4().hex, "p": prov},
            )
            sql = f"""
                INSERT INTO observations (id, workspace_id, source_id, external_id, provider,
                    provider_scope_discriminator, revision, metric, symbol, region, observed_at,
                    collected_at, accepted_at, value, unit, currency, quality, is_current,
                    content_hash, document_id, document_version_id, ingestion_observation_id,
                    source_generation)
                SELECT gen_random_uuid(), :w, :s, sy.sym || '-' || g.i, :prov, :disc, r.rev, 'price',
                    sy.sym, sy.region, ts.t, ts.t, ts.t + (r.rev + CAST(:par AS integer)) * interval '1 second',
                    100 + 20 * random(), 'USD', 'USD', 'ok', r.rev = 2, md5(random()::text),
                    :doc, :ver, CASE r.rev WHEN 1 THEN CAST(:raw1 AS uuid) ELSE CAST(:raw AS uuid) END, 1
                FROM (SELECT 'S' || CAST(CAST(:pair AS integer) AS text) || CAST(j AS text) AS sym,
                             CASE j % 3 WHEN 0 THEN 'VN' WHEN 1 THEN 'US' END AS region
                      FROM generate_series(0, {SYMBOLS_PER_SOURCE - 1}) j) sy
                CROSS JOIN generate_series(0, {points - 1}) g(i)
                CROSS JOIN (VALUES (1), (2)) r(rev)
                CROSS JOIN LATERAL (SELECT CAST(:start AS timestamptz)
                    + g.i * {step_s} * interval '1 second'
                    + (g.i % 2) * CAST(:par AS integer) * interval '1 minute' AS t) ts
                WHERE r.rev = 2 OR g.i % 10 = 0
            """  # noqa: S608  # integer constants only
            result = await conn.execute(
                text(sql),
                {
                    "w": ws,
                    "s": src,
                    "prov": provider,
                    "disc": disc,
                    "pair": k // 2,
                    "par": k % 2,
                    "doc": doc,
                    "ver": ver,
                    "raw": raw,
                    "raw1": raw1,
                    "start": start,
                },
            )
            total += result.rowcount
    return total


async def _seed_documents(
    engine: AsyncEngine, seeded: dict[str, Any], scopes: dict[UUID, str | None]
) -> None:
    """Insert ``DOC_ROWS`` documents + versions + one chunk + provenance each, with accented Vietnamese text."""
    ids: dict[str, UUID] = seeded["ids"]
    ws = seeded["scope"].workspace_id
    phrases = "ARRAY[" + ",".join("'" + p.replace("'", "''") + "'" for p in PHRASES) + "]"
    n = len(PHRASES)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                CREATE TEMP TABLE vol_docs ON COMMIT DROP AS
                SELECT i, md5('doc' || i)::uuid AS did, md5('ver' || i)::uuid AS vid,
                    CASE i % 2 WHEN 0 THEN CAST(:a AS uuid) ELSE CAST(:b AS uuid) END AS sid,
                    CASE i % 2 WHEN 0 THEN :da ELSE :db END AS disc,
                    ({phrases})[1 + i % {n}] || ' #' || i AS title,
                    ({phrases})[1 + i % {n}] || '. ' || ({phrases})[1 + (i + 3) % {n}]
                        || '. ' || ({phrases})[1 + (i + 7) % {n}] || '. Nguồn tin kinh tế trong ngày.'
                        AS body,
                    now() - random() * interval '365 days' AS ts
                FROM generate_series(1, {DOC_ROWS}) i
                """  # noqa: S608  # constants only
            ),
            {
                "a": ids["doc0"],
                "b": ids["doc1"],
                "da": scopes[ids["doc0"]],
                "db": scopes[ids["doc1"]],
            },
        )
        stmts = [
            "INSERT INTO documents (workspace_id, id, source_id, external_id, title, current_version, "
            "content_hash, observed_at) SELECT CAST(:w AS uuid), did, sid, 'vol-' || i, title, 1, "
            "md5(title), ts FROM vol_docs",
            "INSERT INTO document_versions (id, document_id, version_number, content, content_hash) "
            "SELECT vid, did, 1, body, md5(body) FROM vol_docs",
            "INSERT INTO document_chunks (id, document_version_id, chunk_index, content, "
            "content_hash, token_count) SELECT md5('chunk' || i)::uuid, vid, 0, body, md5(body), 40 "
            "FROM vol_docs",
            "INSERT INTO normalized_version_provenance (id, document_id, document_version_id, "
            "provider_id, accepted_record_hash, normalization_version, source_generation, observed_at, "
            "selection_observed_at, title, provenance) SELECT md5('prov' || i)::uuid, did, vid, 'vol', "
            "md5('rec' || i), 1, 1, ts, ts, title, "
            "jsonb_build_object('provider_scope_discriminator', disc) FROM vol_docs",
        ]
        for stmt in stmts:
            await conn.execute(text(stmt), {"w": ws} if ":w" in stmt else {})


async def _analyze(engine: AsyncEngine) -> None:
    """Refresh planner statistics on every seeded table."""
    async with engine.begin() as conn:
        for table in (
            "observations",
            "documents",
            "document_versions",
            "document_chunks",
            "normalized_version_provenance",
            "source_observations",
        ):
            await conn.execute(text(f"ANALYZE {table}"))  # constant table names


async def _timed_call(
    engine: AsyncEngine, seeded: dict[str, Any], args: dict[str, Any], *, captured: list[Any] | None
) -> tuple[float, dict[str, Any]]:
    """Run the real tool once as the owner pinned to the seeded sources; return (seconds, parsed result).

    With ``captured`` given, every statement sent through ``engine`` is appended as ``(sql, params)``.
    """
    scope: WorkspaceContext = seeded["scope"]
    source_scope = [str(sid) for sid in seeded["ids"].values()]
    sync_engine = engine.sync_engine

    def listener(_c: Any, _cur: Any, statement: str, parameters: Any, *_a: Any) -> None:
        """Record one executed statement."""
        assert captured is not None
        captured.append((statement, parameters))

    if captured is not None:
        event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            fence = await workspaces.read_access_fence(
                session, scope=scope, multi_workspace_enabled=False
            )
            started = time.perf_counter()
            raw = await execute_db_query_tool(
                session,
                args,
                scope=scope,
                multi_workspace_enabled=False,
                expected_access_fence=fence,
                retrieval_context={"source_scope": source_scope},
                allow_local_only=False,
                evidence_list=[],
                timezone="Asia/Ho_Chi_Minh",
            )
            elapsed = time.perf_counter() - started
    finally:
        if captured is not None:
            event.remove(sync_engine, "before_cursor_execute", listener)
    return elapsed, json.loads(raw)


async def _explain_captured(engine: AsyncEngine, captured: list[Any]) -> list[str]:
    """EXPLAIN (ANALYZE, BUFFERS) each distinct observation/document SELECT the tool sent; return plan text blocks."""
    blocks: list[str] = []
    seen: set[str] = set()
    async with engine.connect() as conn:
        for statement, params in captured:
            upper = " ".join(statement.split()).upper()  # SQLAlchemy emits newlines before FROM
            relevant = (" FROM OBSERVATIONS" in upper or " FROM DOCUMENTS" in upper) and (
                upper.startswith("SELECT")
            )
            if not relevant or statement in seen:
                continue
            seen.add(statement)
            plan = (
                (
                    await conn.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS) " + statement, tuple(params or ())
                    )
                )
                .scalars()
                .all()
            )
            blocks.append(f"-- statement {len(blocks) + 1}\n" + "\n".join(plan))
        await conn.rollback()
    return blocks


async def _explain_sql_mode(
    engine: AsyncEngine, seeded: dict[str, Any], scopes: dict[UUID, str | None], sql: str
) -> str:
    """EXPLAIN the bounded SQL-mode statement on the reader connection under a real, committed fence."""
    ws = seeded["scope"].workspace_id
    ids = list(seeded["ids"].values())
    clean = db_query.validate_readonly_sql(sql)
    bounded = db_query._bounded_sql(clean).text  # same wrapper the tool executes
    async with db_query._reader_engine().connect() as conn:
        pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async with engine.begin() as app:
            await app.execute(
                db_query._UPSERT_FENCE,
                {"pid": pid, "ws": ws, "src": ids, "ttl": 60},
            )
            await app.execute(db_query._DELETE_FENCE_SCOPE, {"pid": pid})
            await app.execute(
                db_query._INSERT_FENCE_SCOPE,
                [{"pid": pid, "sid": s, "disc": d} for s, d in scopes.items()],
            )
        try:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = await conn.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + bounded), {"max_rows": 26}
            )
            return "\n".join(rows.scalars().all())
        finally:
            await conn.rollback()
            async with engine.begin() as app:
                await app.execute(db_query._DELETE_FENCE_SCOPE, {"pid": pid})
                await app.execute(db_query._DELETE_FENCE, {"pid": pid})


def _percentile(samples: list[float], q: float) -> float:
    """Nearest-rank percentile (with 5 samples p95 is the maximum)."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _write_report(name: str, timings: dict[str, Any], plans: dict[str, str]) -> None:
    """Write ``timings.json`` and one ``plan-<query>.txt`` per query under ``OUT_DIR``."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"timings-{name}.json").write_text(
        json.dumps(timings, indent=2, default=str), encoding="utf-8"
    )
    for key, plan in plans.items():
        (OUT_DIR / f"plan-{key}.txt").write_text(plan, encoding="utf-8")


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


async def test_collected_data_tool_at_volume(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine, reader: None
) -> None:
    """Four representative calls stay under the ceiling at volume and the text search uses the 0025 GIN index."""
    seeded = await _create_sources(ready_owner_client, committed_engine)
    try:
        scopes = await _discriminators(committed_engine, seeded)
        t0 = time.perf_counter()
        observations = await _seed_observations(committed_engine, seeded, scopes)
        await _seed_documents(committed_engine, seeded, scopes)
        await _analyze(committed_engine)
        seed_seconds = time.perf_counter() - t0

        since = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
        sql_text = (
            "SELECT date_trunc('day', observed_at) AS day, avg(value) AS avg_value "
            "FROM scoped_observations WHERE provider = 'binance' AND symbol = 'S00' "
            "AND observed_at >= now() - interval '90 days' GROUP BY 1 ORDER BY 1"
        )
        queries: dict[str, dict[str, Any]] = {
            "latest_one_symbol": {
                "providers": ["binance"],
                "symbols": ["S00"],
                "target": "observations",
            },
            "aggregate_avg_90d": {
                "target": "observations",
                "aggregation": "avg",
                "date_from": since,
            },
            "sql_daily_avg": {"mode": "sql", "sql_query": sql_text},
            "text_gia_vang": {
                "providers": [DOC_PROVIDER],
                "target": "documents",
                "text_query": "gia vang",
            },
        }
        timings: dict[str, Any] = {
            "observations_seeded": observations,
            "documents_seeded": DOC_ROWS,
            "seed_seconds": round(seed_seconds, 1),
            "runs_per_query": RUNS,
        }
        plans: dict[str, str] = {}
        results: dict[str, dict[str, Any]] = {}
        for name, args in queries.items():
            await _timed_call(
                committed_engine, seeded, args, captured=None
            )  # warm-up, not measured
            samples = []
            for _ in range(RUNS):
                elapsed, results[name] = await _timed_call(
                    committed_engine, seeded, args, captured=None
                )
                samples.append(elapsed)
            timings[name] = {
                "p50_s": round(statistics.median(samples), 4),
                "p95_s": round(_percentile(samples, 0.95), 4),
                "samples_s": [round(s, 4) for s in samples],
                "error": results[name].get("error"),
                "result_head": json.dumps(results[name], ensure_ascii=False, default=str)[:300],
            }
            if args.get("mode") == "sql":
                plans[name] = await _explain_sql_mode(committed_engine, seeded, scopes, sql_text)
            else:
                captured: list[Any] = []
                await _timed_call(committed_engine, seeded, args, captured=captured)
                plans[name] = "\n\n".join(await _explain_captured(committed_engine, captured))
        _write_report(seeded["tag"], timings, plans)

        for name in queries:
            assert not timings[name]["error"], f"{name}: {timings[name]['error']}"
            assert timings[name]["p95_s"] < CEILING_SECONDS, f"{name}: {timings[name]}"
        assert results["latest_one_symbol"]["observations"]
        assert results["text_gia_vang"]["documents"]
        assert f"Bitmap Index Scan on {GIN_INDEX}" in plans["text_gia_vang"], plans["text_gia_vang"]
    finally:
        # Archive the synthetic sources: their ~50k documents would otherwise keep the shared workers busy
        # (news/graph recovery) and starve later integration tests. A status flip avoids a multi-million-row
        # delete; workers skip inactive sources.
        async with committed_engine.begin() as conn:
            await conn.execute(
                text("UPDATE sources SET status = 'archived' WHERE id = ANY(:ids)"),
                {"ids": list(seeded["ids"].values())},
            )
