"""Opt-in volume benchmark of the media dashboard projection (``/documents/dashboard-projections``) on real PostgreSQL.

Seeds ``MEDIA_VOLUME_DOCS`` (default 100,000) ready media documents with exactly one provenance row each across
5 manual sources retyped as ``tvmaze_schedule`` (set-based SQL), then times the real endpoint (1 warm-up + 5 runs)
for: default sort with genre + country filters; ``release_date_asc`` with the default window; ``release_date_asc``
with ``min_rating`` + genre; and two keyset pages. ``EXPLAIN (ANALYZE, BUFFERS)`` of the statements the projection
really sends for the ``release_date_asc`` query is written next to the timings under ``test-results/media-volume/``
and printed. Run with ``MEDIA_VOLUME_BENCH=1`` and ``INTEGRATION=1``; the default integration suite skips it.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import ProviderRecordMetadata

pytestmark = [
    pytest.mark.integration,
    # Opt-in: it seeds ~100k documents into the shared stack, whose workers then spend the rest of the suite on
    # them. Run it on purpose with MEDIA_VOLUME_BENCH=1.
    pytest.mark.skipif(
        os.getenv("INTEGRATION") != "1" or os.getenv("MEDIA_VOLUME_BENCH") != "1",
        reason="requires INTEGRATION=1 and MEDIA_VOLUME_BENCH=1 (seeds ~100k documents)",
    ),
]

DOCS = int(os.getenv("MEDIA_VOLUME_DOCS", "100000"))
SOURCES = 5
RUNS = 5
PAGE = 50
CEILING_SECONDS = 1.5
OUT_DIR = Path("test-results/media-volume")
PROJECTIONS = "/api/v1/documents/dashboard-projections"


async def _create_sources(
    client: AsyncClient, engine: AsyncEngine
) -> tuple[list[UUID], WorkspaceContext, int]:
    """Create manual sources via the API, retype them ``tvmaze_schedule``; return ids, owner scope, generation."""
    tag = uuid4().hex[:6]
    ids: list[UUID] = []
    for k in range(SOURCES):
        created = await client.post(
            "/api/v1/sources", json={"type": "manual", "name": f"mvol-{k}-{tag}"}
        )
        created.raise_for_status()
        ids.append(UUID(created.json()["id"]))
    async with engine.begin() as conn:
        # Manual type skips the live provider-scope check; provider only names the media field contract.
        await conn.execute(
            text(
                "UPDATE sources SET provider = 'tvmaze_schedule', configuration = configuration || "
                "jsonb_build_object('retention_days', 3650) WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT w.id, w.owner_user_id, s.generation FROM workspaces w "
                    "JOIN sources s ON s.workspace_id = w.id WHERE s.id = :i"
                ),
                {"i": ids[0]},
            )
        ).one()
        ws, owner, generation = row[0], row[1], row[2]
        revision: int = (
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
    return ids, scope, int(generation)


async def _seed(
    engine: AsyncEngine, ids: list[UUID], scope: WorkspaceContext, generation: int
) -> None:
    """Insert ``DOCS`` documents + version + chunk + one provenance row each, with contract-valid media fields.

    Release dates are uniform over +-365 days (so the 37-day default window holds ~5%), created_at over the
    last 365 days; 4 genre pairs, 3 countries, 2 formats, ratings 40-100.
    """
    base = ProviderRecordMetadata(
        provider="tvmaze_schedule",
        identity="base",
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=False,
        source_fields={},
    ).model_dump(mode="json")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TEMP TABLE mvol ON COMMIT DROP AS
                SELECT i, md5('d' || i)::uuid AS did, md5('v' || i)::uuid AS vid,
                    (CAST(:ids AS uuid[]))[1 + i % :n] AS sid,
                    now() - random() * interval '365 days' AS created,
                    now() + (random() * 730 - 365) * interval '1 day' AS rel,
                    (ARRAY['action','drama','comedy','fantasy'])[1 + i % 4] AS g1,
                    (ARRAY['drama','comedy','fantasy','action'])[1 + i % 4] AS g2,
                    (ARRAY['US','JP','GB'])[1 + i % 3] AS country,
                    round((40 + random() * 60)::numeric, 1) AS rating
                FROM generate_series(1, :docs) i
                """
            ),
            {"ids": ids, "n": SOURCES, "docs": DOCS},
        )
        stmts = [
            "INSERT INTO documents (workspace_id, id, source_id, external_id, title, current_version, "
            "content_hash, extraction_status, observed_at, created_at) SELECT :w, did, sid, 'mv-' || i, "
            "'Media volume ' || i, 1, md5('h' || i), 'ready', created, created FROM mvol",
            "INSERT INTO document_versions (id, document_id, version_number, content, content_hash) "
            "SELECT vid, did, 1, 'Media volume ' || i, md5('c' || i) FROM mvol",
            "INSERT INTO document_chunks (id, document_version_id, chunk_index, content, content_hash, "
            "token_count) SELECT md5('k' || i)::uuid, vid, 0, 'Media volume ' || i, md5('k' || i), 3 "
            "FROM mvol",
            "INSERT INTO normalized_version_provenance (id, document_id, document_version_id, "
            "provider_id, accepted_record_hash, normalization_version, source_generation, observed_at, "
            "selection_observed_at, title, provenance) SELECT md5('p' || i)::uuid, did, vid, "
            "'tvmaze_schedule', md5('r' || i) || md5('s' || i), 1, :gen, created, created, "
            "'Media volume ' || i, jsonb_build_object('provider_record', CAST(:base AS jsonb) || "
            "jsonb_build_object('identity', 'mv-' || i, 'source_fields', jsonb_build_object("
            "'title', 'Media volume ' || i, 'media_type', 'anime', 'genres', jsonb_build_array(g1, g2), 'country', country, "
            "'language', CASE WHEN country = 'JP' THEN 'ja' ELSE 'en' END, "
            "'format', CASE WHEN i % 2 = 0 THEN 'tv' ELSE 'movie' END, 'content_rating', 'tv_14', "
            "'rating', rating, 'release_date', to_char(rel AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24\\:MI\\:SS\"Z\"'), 'image_url', 'https://example.com/mv/' || i || '.jpg'))) "
            "FROM mvol",
        ]
        params = {"w": scope.workspace_id, "gen": generation, "base": json.dumps(base)}
        for stmt in stmts:
            await conn.execute(text(stmt), {k: v for k, v in params.items() if f":{k}" in stmt})
        for table in (
            "documents",
            "document_versions",
            "document_chunks",
            "normalized_version_provenance",
        ):
            await conn.execute(text(f"ANALYZE {table}"))  # constant table names


def _percentile(samples: list[float], q: float) -> float:
    """Nearest-rank percentile (with 5 samples p95 is the maximum)."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


async def _timed(
    client: AsyncClient, ids: list[UUID], params: list[tuple[str, Any]]
) -> tuple[float, dict[str, Any]]:
    """One real endpoint call; returns (seconds, body). Fails on a non-200 answer."""
    query: list[tuple[str, Any]] = [("source_ids", str(i)) for i in ids]
    query += [("limit", PAGE), *params]
    started = time.perf_counter()
    response = await client.get(PROJECTIONS, params=query)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    return elapsed, dict(response.json())


def _write_report(timings: dict[str, Any], plan: str) -> None:
    """Write ``timings.json`` and ``plan-release_date_asc.txt`` under ``OUT_DIR``."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    (OUT_DIR / "plan-release_date_asc.txt").write_text(plan, encoding="utf-8")


async def _explain(engine: AsyncEngine, ids: list[UUID], scope: WorkspaceContext) -> str:
    """EXPLAIN (ANALYZE, BUFFERS) every SELECT the ``release_date_asc`` projection sends (same in-process code path)."""
    captured: list[tuple[str, Any]] = []

    def listener(_c: Any, _cur: Any, statement: str, parameters: Any, *_a: Any) -> None:
        """Record one executed statement."""
        captured.append((statement, parameters))

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await documents.list_gadget_document_projections(
                session,
                source_ids=tuple(ids),
                limit=PAGE,
                sort="release_date_asc",
                scope=scope,
                multi_workspace_enabled=False,
            )
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)
    blocks: list[str] = []
    async with engine.connect() as conn:
        for statement, args in captured:
            upper = " ".join(statement.split()).upper()
            if not upper.startswith("SELECT") or " FROM DOCUMENTS" not in upper:
                continue
            plan = await conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS) " + statement, tuple(args or ())
            )
            blocks.append(f"-- statement {len(blocks) + 1}\n" + "\n".join(plan.scalars().all()))
        await conn.rollback()
    return "\n\n".join(blocks)


async def test_media_projection_at_volume(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine
) -> None:
    """Every projection query stays under ``CEILING_SECONDS`` per page at ~100k documents; plans are logged."""
    ids, scope, generation = await _create_sources(ready_owner_client, committed_engine)
    try:
        t0 = time.perf_counter()
        await _seed(committed_engine, ids, scope, generation)
        seed_seconds = round(time.perf_counter() - t0, 1)

        asc: list[tuple[str, Any]] = [("sort", "release_date_asc")]
        queries: dict[str, list[tuple[str, Any]]] = {
            "default_genre_country": [("genres", "drama"), ("countries", "US")],
            "release_asc_default_window": asc,
            "release_asc_rating_genre": [*asc, ("min_rating", 80), ("genres", "drama")],
        }
        timings: dict[str, Any] = {"docs_seeded": DOCS, "seed_seconds": seed_seconds}
        for name, params in queries.items():
            await _timed(ready_owner_client, ids, params)  # warm-up, not measured
            samples = []
            for _ in range(RUNS):
                elapsed, body = await _timed(ready_owner_client, ids, params)
                samples.append(elapsed)
            timings[name] = {
                "items": len(body["items"]),
                "p50_s": round(statistics.median(samples), 4),
                "p95_s": round(_percentile(samples, 0.95), 4),
                "samples_s": [round(s, 4) for s in samples],
            }
            assert body["items"], f"{name}: empty page, the seed does not exercise the query"
        # Two keyset pages of release_date_asc: the cursor must continue, not restart.
        page_seconds: list[float] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(2):
            extra: list[tuple[str, Any]] = [("cursor", cursor)] if cursor else []
            elapsed, body = await _timed(ready_owner_client, ids, [*asc, *extra])
            page_seconds.append(round(elapsed, 4))
            page_ids = {item["document_id"] for item in body["items"]}
            assert page_ids and not page_ids & seen, "keyset page repeated or empty"
            seen |= page_ids
            cursor = body["next_cursor"]
            assert cursor, "release window holds more than two pages"
        timings["release_asc_two_pages_s"] = page_seconds

        plan = await _explain(committed_engine, ids, scope)
        _write_report(timings, plan)
        print("MEDIA_VOLUME_TIMINGS " + json.dumps(timings))
        print("MEDIA_VOLUME_PLAN\n" + plan)

        for name in queries:
            assert timings[name]["p95_s"] < CEILING_SECONDS, f"{name}: {timings[name]}"
        assert max(page_seconds) < CEILING_SECONDS, page_seconds
    finally:
        # Archive the synthetic sources so ~100k documents do not keep shared workers busy for later tests.
        async with committed_engine.begin() as conn:
            await conn.execute(
                text("UPDATE sources SET status = 'archived' WHERE id = ANY(:ids)"), {"ids": ids}
            )
