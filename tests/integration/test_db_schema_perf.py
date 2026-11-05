"""Real-PostgreSQL proof for revision 0014: stored tsvector, online indexes and concurrent HNSW build.

Runs only against the disposable Compose database (``INTEGRATION=1``); the orchestrator executes it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modules.ingestion._purge import outbox_purge_statement
from modules.search import indexing

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

_TEXTS = (
    "Alpha beta gamma delta", "alpha alpha alpha beta", "Quarterly revenue grew; gamma radiation unrelated",
    "beta", "completely different words here", "Alpha, beta; gamma-delta epsilon alpha", "",
)
_QUERIES = ("alpha", "alpha beta", "gamma OR epsilon", '"beta gamma"', "alpha -delta")


async def test_generated_column_scores_equal_the_inline_expression(committed_engine: AsyncEngine) -> None:
    """Ranking through a stored GENERATED column equals ranking through the former inline expression."""
    async with committed_engine.connect() as connection:
        await connection.execute(text(
            "CREATE TEMP TABLE tsv_fixture (id int, content text NOT NULL, "
            "content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED)"
        ))
        for index, content in enumerate(_TEXTS):
            await connection.execute(text("INSERT INTO tsv_fixture (id, content) VALUES (:i, :c)"), {"i": index, "c": content})
        for query in _QUERIES:
            rows = (await connection.execute(text(
                "SELECT id, ts_rank_cd(content_tsv, websearch_to_tsquery('simple', :q)) AS stored, "
                "ts_rank_cd(to_tsvector('simple', content), websearch_to_tsquery('simple', :q)) AS inline, "
                "content_tsv @@ websearch_to_tsquery('simple', :q) AS stored_match, "
                "to_tsvector('simple', content) @@ websearch_to_tsquery('simple', :q) AS inline_match "
                "FROM tsv_fixture ORDER BY id"
            ), {"q": query})).all()
            assert [(r.stored, r.stored_match) for r in rows] == [(r.inline, r.inline_match) for r in rows], query


async def test_migration_0014_schema_state(committed_engine: AsyncEngine) -> None:
    async with committed_engine.connect() as connection:
        generated = await connection.scalar(text(
            "SELECT attgenerated FROM pg_attribute WHERE attrelid = 'document_chunks'::regclass AND attname = 'content_tsv'"
        ))
        assert generated == "s"  # STORED
        names = {row[0] for row in (await connection.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))).all()}
        assert "ix_document_chunks_content_tsv_gin" in names
        assert not names & {"ix_document_chunks_simple_fts", "ix_document_chunks_content_tsv",
                            "ix_relationship_evidence_document", "ix_chat_response_runs_status"}
        assert {"ix_fk_entity_aliases_source_id", "ix_fk_observations_document_id",
                "ix_chat_response_runs_active_status", "ix_event_outbox_failed_created"} <= names
        invalid = (await connection.execute(text(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid"
        ))).all()
        assert invalid == []  # a failed CONCURRENTLY build would leave an invalid index behind
        options = await connection.scalar(text("SELECT reloptions::text FROM pg_class WHERE relname = 'event_outbox'"))
        assert "autovacuum_analyze_scale_factor=0.02" in options


async def test_hnsw_index_builds_concurrently_and_is_idempotent(committed_engine: AsyncEngine) -> None:
    generation_id = uuid4()
    name = indexing.vector_index_name(generation_id)
    try:
        for _ in range(2):  # second call is the IF NOT EXISTS retry path
            await indexing.build_vector_index_concurrently(committed_engine, generation_id, 3)
        async with committed_engine.connect() as connection:
            row = (await connection.execute(text(
                "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c "
                "ON c.oid = i.indexrelid WHERE c.relname = :n"), {"n": name})).one()
        assert row[0] is True and "hnsw" in row[1] and f"generation_id = '{generation_id}'" in row[1]
    finally:
        async with committed_engine.connect() as raw:
            connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


async def test_outbox_purge_statement_runs_against_the_real_schema(committed_engine: AsyncEngine) -> None:
    async with committed_engine.begin() as connection:
        result = await connection.execute(outbox_purge_statement(now=datetime(2000, 1, 1, tzinfo=UTC), limit=10))
        assert result.rowcount == 0  # nothing is older than the cutoff; the statement is valid SQL
