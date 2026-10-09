"""P12 release acceptance: Source purge deletes derived data, never resurrects it, and gates success.

Everything here runs against the disposable stack: sources and documents are created through the
real owner API, the real worker consumes the durable outbox, and results are read from PostgreSQL.
Only the copied-evidence rows the API cannot create (derived Memory, Memory candidate, Agent run
ledger) are inserted directly, exactly as their owning modules persist them.
"""

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import core.auth.models  # noqa: F401  # registers the owner table for foreign keys
from modules.agents.models import AgentRun, AgentToolCall
from modules.ingestion.dispatcher import WORKER_BY_EVENT
from modules.memory.models import Memory, MemoryCandidate

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

PURGE_TIMEOUT_SECONDS = 360
REPLAY_TIMEOUT_SECONDS = 360
# Every public table is scanned for leftover secret text, except these audit-only/bookkeeping
# tables that legitimately never carry content (none are expected to match; listed for clarity).
SKIPPED_SCAN_TABLES: set[str] = set()


async def _scalar(engine: AsyncEngine, sql: str, **parameters: Any) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), parameters)


async def _rows(engine: AsyncEngine, sql: str, **parameters: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), parameters)).mappings().all())


async def _create_source(client: AsyncClient, name: str) -> UUID:
    response = await client.post("/api/v1/sources", json={"type": "manual", "name": name})
    response.raise_for_status()
    return UUID(response.json()["id"])


async def _create_document(client: AsyncClient, source_id: UUID, title: str, content: str) -> UUID:
    response = await client.post("/api/v1/documents", json={
        "source_id": str(source_id), "title": title, "content": content,
        "external_id": f"p12-{uuid4().hex}",
    })
    response.raise_for_status()
    return UUID(response.json()["id"])


async def _scope(engine: AsyncEngine) -> dict[str, object]:
    """Owner workspace identity required by workspace-scoped rows since W1/W2."""
    row = (await _rows(engine, "SELECT id, owner_user_id FROM workspaces ORDER BY created_at LIMIT 1"))[0]
    return {"workspace_id": row["id"], "actor_user_id": row["owner_user_id"]}


async def _occurrences(engine: AsyncEngine, needle: str) -> dict[str, int]:
    """Count rows still holding ``needle`` per public table, in text/jsonb and in bytea columns."""
    tables = [row["tablename"] for row in await _rows(
        engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )]
    found: dict[str, int] = {}
    for table in tables:
        if table in SKIPPED_SCAN_TABLES:
            continue
        count = await _scalar(
            engine, f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE :needle',
            needle=f"%{needle}%",
        )
        if count:
            found[table] = count
    # bytea renders as hex in ``t::text``, so LangGraph checkpoint blobs and raw browser content
    # need a byte-level search of every bytea column.
    for column in await _rows(
        engine, "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'bytea'"
    ):
        table, name = column["table_name"], column["column_name"]
        count = await _scalar(
            engine, f"SELECT count(*) FROM \"{table}\" "
            f"WHERE position(convert_to(:needle, 'UTF8') in \"{name}\") > 0",
            needle=needle,
        )
        if count:
            found[column["table_name"]] = found.get(column["table_name"], 0) + count
    return found


async def _seed_copied_evidence(
    engine: AsyncEngine, source_id: UUID, control_source_id: UUID, document_ids: list[UUID],
    secret: str,
) -> dict[str, UUID]:
    """Insert derived copies of the Source's content in Memory and the Agent ledger."""
    ids = {
        "derived": uuid4(), "candidate": uuid4(), "manual": uuid4(), "control": uuid4(),
        "run": uuid4(),
    }
    async with engine.connect() as connection:
        versions = (await connection.execute(text(
            "SELECT d.id AS document_id, v.id AS version_id, c.id AS chunk_id "
            "FROM documents d JOIN document_versions v ON v.document_id = d.id "
            "JOIN document_chunks c ON c.document_version_id = v.id "
            "WHERE d.id = ANY(:ids) ORDER BY d.id"
        ), {"ids": document_ids})).mappings().all()
    assert versions, "document creation must have produced searchable chunks"
    first = versions[0]
    fence = {
        "document_id": str(first["document_id"]), "document_version_id": str(first["version_id"]),
        "source_id": str(source_id), "source_generation": 1, "chunk_id": str(first["chunk_id"]),
    }
    fences = {"source_generations": {str(source_id): 1}, "records": [fence]}
    # Exact immutable identity, as Memory persists it for Document-derived copies.
    identity = {
        "source_id": str(source_id), "document_id": str(first["document_id"]),
        "document_version_id": str(first["version_id"]), "chunk_id": str(first["chunk_id"]),
    }
    ws = await _scope(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(MemoryCandidate(
            **ws,
            id=ids["candidate"], content=f"candidate copy {secret}", memory_type="fact",
            provenance=identity, status="pending",
        ))
        await session.flush()
        session.add_all([
            Memory(
                **ws,
                id=ids["derived"], content=f"derived copy {secret}", is_manual=False,
                provenance=identity,
                candidate_id=ids["candidate"], reason=f"learned from {secret}",
            ),
            Memory(
                **ws,
                id=ids["manual"], content="manual fact the owner wrote independently",
                is_manual=True,
                provenance={**identity, "origin": "manual"},
            ),
            Memory(
                **ws,
                id=ids["control"], content="control memory from another source", is_manual=False,
                provenance={"source_id": str(control_source_id)},
            ),
        ])
        thread_id = str(uuid4())
        session.add(AgentRun(
            workspace_id=ws["workspace_id"], owner_id=ws["actor_user_id"],
            id=ids["run"], auth_session_hash="0" * 64, workflow_version="w1", prompt_version="p1",
            checkpoint_schema_version=1, checkpoint_thread_id=thread_id,
            prompt="Fictional question", allowed_tools=[], tool_contracts={},
            source_fences=fences, status="succeeded", answer=f"answer quoting {secret}",
            completed_at=datetime.now(UTC),
        ))
        await session.flush()
        session.add(AgentToolCall(
            run_id=ids["run"], ordinal=1, tool_name="search_documents",
            arguments={"query": secret}, input_source_fences=fences, input_provenance_version=1,
            status="succeeded", evidence_refs=[],
        ))
        # LangGraph saver state for the run quotes evidence: msgpack-like bytes plus jsonb.
        blob = f"¨messagesÙ@quoted evidence {secret}".encode()
        checkpoint_json = f'{{"channel_values": {{"messages": "evidence {secret}"}}}}'
        key = {"t": thread_id}
        await session.execute(text(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint) "
            "VALUES (:t, '', 'cp1', 'json', CAST(:cp AS jsonb))"
        ), {**key, "cp": checkpoint_json})
        await session.execute(text(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (:t, '', 'messages', '1', 'msgpack', :blob)"
        ), {**key, "blob": blob})
        await session.execute(text(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "idx, channel, type, blob) "
            "VALUES (:t, '', 'cp1', 'task1', 0, 'messages', 'msgpack', :blob)"
        ), {**key, "blob": blob})
        await session.commit()
    return ids


async def _wait_for_operation(
    engine: AsyncEngine, source_id: UUID, *, until: str = "succeeded",
) -> dict[str, Any]:
    deadline = time.monotonic() + PURGE_TIMEOUT_SECONDS
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        rows = await _rows(
            engine, "SELECT * FROM source_purge_operations WHERE source_id = :id", id=source_id
        )
        if rows:
            row = dict(rows[0])
            # A "failed" aggregate is transient while a child stage waits for its 30 s retry;
            # only a purge that never reaches success within the deadline fails the test.
            if row["status"] == until:
                return row
        await asyncio.sleep(2)
    pytest.fail(f"purge did not reach {until} in time: {row}")


async def test_source_purge_erases_derived_data_and_replays_never_resurrect_it(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client, engine = ready_owner_client, committed_engine
    secret = f"SECRET-{uuid4().hex}"
    source_id = await _create_source(client, f"purge target {uuid4().hex[:8]}")
    control_id = await _create_source(client, f"purge control {uuid4().hex[:8]}")
    document_ids = [
        await _create_document(client, source_id, f"Note {number}", f"Fictional content {number} {secret}")
        for number in range(3)
    ]
    control_document = await _create_document(
        client, control_id, "Control note", "Unrelated control content that must survive."
    )
    ids = await _seed_copied_evidence(engine, source_id, control_id, document_ids, secret)
    before = await _occurrences(engine, secret)
    assert {
        "document_versions", "document_chunks", "memories", "memory_candidates", "agent_runs",
        "checkpoints", "checkpoint_blobs", "checkpoint_writes",  # positive control incl. bytea
    } <= set(before), before

    queued = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
    assert queued.status_code == 202
    assert queued.json()["source_id"] == str(source_id)
    operation = await _wait_for_operation(engine, source_id)

    # Success is reported only with every stage complete.
    assert operation["documents_status"] == "deleted"
    assert operation["memory_status"] == "succeeded" and operation["memory_cache_pending"] is False
    assert operation["pending_owner_codes"] == [] and operation["error_code"] is None
    unfinished = await _scalar(
        engine,
        "SELECT count(*) FROM document_cleanup_operations WHERE source_id = :id AND "
        "(status <> 'succeeded' OR copied_status <> 'succeeded' OR memory_status <> 'succeeded' "
        "OR agent_status <> 'succeeded' OR chat_status <> 'succeeded' "
        "OR materialization_status <> 'succeeded' OR brief_status <> 'succeeded')",
        id=source_id,
    )
    assert unfinished == 0

    async def snapshot() -> dict[str, Any]:
        return {
            "documents": await _scalar(
                engine, "SELECT count(*) FROM documents WHERE source_id = :id", id=source_id),
            "versions": await _scalar(
                engine, "SELECT count(*) FROM document_versions WHERE document_id = ANY(:ids)",
                ids=document_ids),
            "memories": await _rows(
                engine, "SELECT id, content, status, provenance::text AS provenance, updated_at "
                "FROM memories ORDER BY id"),
            "candidates": await _rows(
                engine, "SELECT id, content, status, updated_at FROM memory_candidates ORDER BY id"),
            "counts": {
                table: await _scalar(engine, f'SELECT count(*) FROM "{table}"')
                for table in ("documents", "document_versions", "document_chunks",
                              "entity_extraction_work", "news_observations", "search_index_items")
            },
            "operation": dict(
                (await _rows(engine, "SELECT * FROM source_purge_operations WHERE source_id = :id",
                             id=source_id))[0]),
        }

    # Derived data is gone from every owner.
    assert await _occurrences(engine, secret) == {}, "secret content survived the purge"
    assert await _scalar(
        engine, "SELECT count(*) FROM documents WHERE source_id = :id", id=source_id) == 0
    derived = (await _rows(engine, "SELECT * FROM memories WHERE id = :id", id=ids["derived"]))[0]
    assert derived["content"] == "" and derived["status"] == "forgotten" and derived["provenance"] == {}
    candidate = (await _rows(
        engine, "SELECT * FROM memory_candidates WHERE id = :id", id=ids["candidate"]))[0]
    assert candidate["content"] == "" and candidate["status"] == "expired"
    manual = (await _rows(engine, "SELECT * FROM memories WHERE id = :id", id=ids["manual"]))[0]
    assert manual["content"] == "manual fact the owner wrote independently"
    assert str(source_id) not in str(manual["provenance"])
    run = (await _rows(engine, "SELECT * FROM agent_runs WHERE id = :id", id=ids["run"]))[0]
    assert run["evidence_revoked"] is True and run["answer"] is None

    # Independent evidence is retained.
    control_memory = (await _rows(engine, "SELECT * FROM memories WHERE id = :id", id=ids["control"]))[0]
    assert control_memory["content"] == "control memory from another source"
    assert await _scalar(
        engine, "SELECT count(*) FROM documents WHERE id = :id", id=control_document) == 1
    assert (await client.get(f"/api/v1/documents/{control_document}")).status_code == 200

    # Owner-visible reads no longer return the purged content.
    for document_id in document_ids:
        assert (await client.get(f"/api/v1/documents/{document_id}")).status_code == 404
    assert (await client.get(f"/api/v1/sources/{source_id}")).json()["status"] == "archived"
    # New ingestion into the purged Source is refused.
    refused = await client.post("/api/v1/documents", json={
        "source_id": str(source_id), "title": "Late arrival", "content": f"again {secret}",
        "external_id": f"late-{uuid4().hex}",
    })
    assert refused.status_code == 409
    # A repeated purge request is idempotent: it returns the existing receipt, no second operation.
    again = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
    assert again.status_code == 202 and again.json()["operation_id"] == str(operation["id"])

    settled = await snapshot()

    # Replay: re-deliver every dispatchable event that was already delivered for this Source (purge,
    # coverage, cleanup, and ready/news events of its deleted documents), then wait until the real
    # worker has consumed every one of them before judging resurrection.
    async with engine.begin() as connection:
        replay_started = await connection.scalar(text("SELECT clock_timestamp()"))
        replayed = (await connection.execute(text(
            "UPDATE event_outbox SET status = 'pending', next_attempt_at = now(), dispatched_at = NULL "
            "WHERE status = 'delivered' AND type = ANY(:types) AND (payload::text LIKE :source "
            "OR payload::text LIKE :operation OR payload::text LIKE ANY(:documents)) "
            "RETURNING id, type"
        ), {
            "types": list(WORKER_BY_EVENT),
            "source": f"%{source_id}%", "operation": f"%{operation['id']}%",
            "documents": [f"%{document_id}%" for document_id in document_ids],
        })).all()
    replayed_ids = [row.id for row in replayed]
    replayed_types = {row.type for row in replayed}
    assert {"source.purge.requested", "document.version.ready"} <= replayed_types, replayed_types
    deadline = time.monotonic() + REPLAY_TIMEOUT_SECONDS
    unconsumed: list[Any] = replayed_ids
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        unconsumed = await _rows(
            engine, "SELECT id, type, status FROM event_outbox WHERE id = ANY(:ids) AND NOT "
            "(status IN ('delivered', 'failed') AND dispatched_at > :started)",
            ids=replayed_ids, started=replay_started,
        )
        if not unconsumed:
            break
    else:
        pytest.fail(f"replayed events were not consumed in time: {unconsumed}")
    assert await _rows(
        engine, "SELECT id, type FROM event_outbox WHERE id = ANY(:ids) AND status = 'failed'",
        ids=replayed_ids,
    ) == [], "a replayed consumer failed"
    await asyncio.sleep(12)  # at least two further cron polls of every reconciler

    after = await snapshot()
    assert after == settled, "replayed consumers changed the settled purge state"
    assert await _occurrences(engine, secret) == {}
    assert await _scalar(
        engine, "SELECT count(*) FROM documents WHERE source_id = :id", id=source_id) == 0
    assert (await client.get(f"/api/v1/sources/{source_id}")).json()["status"] == "archived"
    impact = await client.get(f"/api/v1/sources/{source_id}/impact")
    assert impact.status_code == 200 and impact.json()["document_count"] == 0
    assert impact.json()["gadget_definition_count"] == 0 and impact.json()["conversation_count"] == 0


async def test_purge_reports_success_only_after_source_memory_coverage_completes(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client, engine = ready_owner_client, committed_engine
    secret = f"GATE-{uuid4().hex}"
    source_id = await _create_source(client, f"gate target {uuid4().hex[:8]}")
    control_id = await _create_source(client, f"gate control {uuid4().hex[:8]}")
    document_ids = [
        await _create_document(client, source_id, f"Gate {number}", f"Fictional gate {number} {secret}")
        for number in range(2)
    ]
    ids = await _seed_copied_evidence(engine, source_id, control_id, document_ids, secret)

    # Hold a row lock on the derived Memory copy: every Memory cleanup stage must wait for it,
    # while canonical Source deletion (which never touches Memory) proceeds.
    holder = await engine.connect()
    try:
        await holder.execute(text("BEGIN"))
        await holder.execute(
            text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": ids["derived"]})
        queued = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
        assert queued.status_code == 202

        deadline = time.monotonic() + PURGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            rows = await _rows(
                engine, "SELECT * FROM source_purge_operations WHERE source_id = :id", id=source_id)
            if rows and rows[0]["documents_status"] == "deleted":
                break
            await asyncio.sleep(2)
        else:
            pytest.fail("canonical Source deletion never completed")
        assert await _scalar(
            engine, "SELECT count(*) FROM documents WHERE source_id = :id", id=source_id) == 0

        # While the purge is pending, no owner-visible read may return the deleted content, even
        # though the derived Memory copy is not yet scrubbed in the database.
        for document_id in document_ids:
            assert (await client.get(f"/api/v1/documents/{document_id}")).status_code == 404
        for path, params in (
            ("/api/v1/memories", {"limit": 100}),
            ("/api/v1/memories", {"limit": 100, "q": secret}),
            ("/api/v1/memories/candidates/list", {"limit": 100}),
            (f"/api/v1/memories/{ids['derived']}", {}),
            ("/api/v1/search/global", {"q": secret}),
        ):
            response = await client.get(path, params=params)
            assert secret not in response.text, (path, params, response.text[:500])
        search = await client.get("/api/v1/search/global", params={"q": secret})
        assert search.status_code == 200 and search.json()["documents"] == []

        # Canonical data is gone, yet success must not be claimed while Memory coverage is open.
        # Keep the hold short: the worker has four job slots, so a long hold still backs up its queue.
        for _ in range(4):
            await asyncio.sleep(2)
            operation = (await _rows(
                engine, "SELECT * FROM source_purge_operations WHERE source_id = :id", id=source_id))[0]
            assert operation["status"] != "succeeded", operation
            assert operation["memory_status"] != "succeeded", operation
            assert "memory" in operation["pending_owner_codes"], operation
        # The derived Memory copy is still unscrubbed while its coverage is incomplete.
        pending_memory = (await _rows(engine, "SELECT * FROM memories WHERE id = :id", id=ids["derived"]))[0]
        assert secret in pending_memory["content"]
    finally:
        await holder.rollback()
        await holder.close()

    operation = await _wait_for_operation(engine, source_id)
    assert operation["memory_status"] == "succeeded" and operation["memory_cache_pending"] is False
    assert operation["documents_status"] == "deleted" and operation["pending_owner_codes"] == []
    assert await _occurrences(engine, secret) == {}


async def test_purge_is_not_reported_successful_while_a_historical_cleanup_receipt_has_failed(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client, engine = ready_owner_client, committed_engine
    secret = f"HIST-{uuid4().hex}"
    source_id = await _create_source(client, f"historical target {uuid4().hex[:8]}")
    document_ids = [
        await _create_document(client, source_id, f"Hist {number}", f"Fictional hist {number} {secret}")
        for number in range(2)
    ]
    # A derived Memory tied to the Source whose provenance the per-Document sweep cannot interpret
    # (an unsupported legacy key) is an unresolved, terminal per-Document failure; the Source-wide
    # sweep still erases it by exact Source identity.
    orphan = uuid4()
    ws = await _scope(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(Memory(
            **ws,
            id=orphan, content=f"copy with unreadable provenance {secret}", is_manual=False,
            provenance={"source_id": str(source_id), "legacy_import_note": "unsupported shape"},
        ))
        await session.commit()

    individual = await client.delete(f"/api/v1/documents/{document_ids[0]}")
    assert individual.status_code == 202
    deadline = time.monotonic() + PURGE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        failed = await _scalar(
            engine, "SELECT count(*) FROM document_cleanup_operations WHERE source_id = :id "
            "AND memory_status = 'failed' AND source_purge_operation_id IS NULL", id=source_id)
        if failed:
            break
        await asyncio.sleep(2)
    else:
        pytest.fail("the individual deletion never produced its unresolved historical receipt")

    queued = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
    assert queued.status_code == 202
    deadline = time.monotonic() + PURGE_TIMEOUT_SECONDS
    operation: dict[str, Any] = {}
    while time.monotonic() < deadline:
        rows = await _rows(
            engine, "SELECT * FROM source_purge_operations WHERE source_id = :id", id=source_id)
        operation = dict(rows[0]) if rows else {}
        if operation.get("documents_status") == "deleted" and operation.get("memory_status") == "succeeded":
            break
        await asyncio.sleep(2)
    else:
        pytest.fail(f"Source-local coverage never completed: {operation}")

    # Canonical data and Source-wide Memory coverage are complete, but the historical receipt is
    # not: the aggregate must keep reporting failure, never success.
    for _ in range(6):
        await asyncio.sleep(5)
        operation = (await _rows(
            engine, "SELECT * FROM source_purge_operations WHERE source_id = :id", id=source_id))[0]
        assert operation["status"] != "succeeded", operation
    assert operation["status"] == "failed" and operation["error_code"] == "document_cleanup_failed"
    assert await _scalar(
        engine, "SELECT count(*) FROM documents WHERE source_id = :id", id=source_id) == 0
    # The Source-wide sweep still erased the derived copy it could attribute to the Source.
    row = (await _rows(engine, "SELECT * FROM memories WHERE id = :id", id=orphan))[0]
    assert row["content"] == "" and row["status"] == "forgotten"
    assert await _occurrences(engine, secret) == {}


async def test_impact_counts_seeded_gadget_and_chat_citation(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Seed one gadget definition and one cited chat (worker key shape) and expect non-zero counts."""
    client, engine = ready_owner_client, committed_engine
    source_id = await _create_source(client, f"impact {uuid4().hex[:8]}")
    owner_id = await _scalar(engine, "SELECT id FROM owner LIMIT 1")
    conversation_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO gadget_definitions (id, owner_id, name, renderer, source_ids) "
            "VALUES (gen_random_uuid(), :owner, 'impact gadget', 'list', CAST(:ids AS jsonb))"
        ), {"owner": owner_id, "ids": f'["{source_id}"]'})
        await connection.execute(text(
            "INSERT INTO chat_conversations (id, title) VALUES (:id, 'impact chat')"
        ), {"id": conversation_id})
        await connection.execute(text(
            "INSERT INTO chat_messages (id, conversation_id, role, content, citations) "
            "VALUES (gen_random_uuid(), :id, 'assistant', 'x', CAST(:cites AS jsonb))"
        ), {"id": conversation_id, "cites": f'[{{"source_id": "{source_id}"}}]'})
    impact = (await client.get(f"/api/v1/sources/{source_id}/impact")).json()
    assert impact["gadget_definition_count"] == 1
    assert impact["conversation_count"] == 1
