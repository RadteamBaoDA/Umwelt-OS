"""Collection receipt telemetry end to end on the disposable PostgreSQL.

A source is created through the owner API, a collection request is opened and admitted through the
connectors scheduler, and the batch is accepted through ``receive_batch`` with the admitted request ref
(the way the native worker does). The receipt row is read back raw; the runs route is the user-facing read.
"""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import core.auth.models  # noqa: F401  # registers the owner table for foreign keys
from core.workspaces.schemas import InternalJobScope
from modules.connectors import scheduler
from modules.connectors.collection_schemas import CollectionRequestRef
from modules.connectors.models import ConnectorProvisioning
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import CollectionStateUpdate, IngestionRecord, ReceiveBatch

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)
FLAG = False  # the disposable stack runs the default single-workspace rollout


async def _collect(
    engine: AsyncEngine,
    client: AsyncClient,
    stats: dict[str, Any] | None,
) -> tuple[UUID, UUID, UUID]:
    """Settle one fenced collection carrying ``stats``; return (source_id, run_id, request_id)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created = await client.post(
        "/api/v1/sources", json={"type": "rss", "name": f"stats-{uuid4().hex[:8]}"}
    )
    created.raise_for_status()
    source_id = UUID(created.json()["id"])
    async with engine.connect() as connection:
        workspace_id, generation = (
            await connection.execute(
                text("SELECT workspace_id, generation FROM sources WHERE id = :i"), {"i": source_id}
            )
        ).one()
        user_id, revision = (
            await connection.execute(
                text(
                    "SELECT user_id, revision FROM workspace_memberships WHERE workspace_id = :w AND role = 'owner'"
                ),
                {"w": workspace_id},
            )
        ).one()
    async with factory() as session:
        session.add(
            ConnectorProvisioning(
                source_id=source_id,
                source_generation=generation,
                desired_revision=3,
                applied_revision=3,
                desired_enabled=True,
                state="active",
                execution_backend="n8n",
                backend_revision=1,
                applied_backend_revision=1,
                workflow_id="wf-stats",
                workflow_name="stats",
                template_revision=1,
                applied_template_revision=1,
                desired_configuration={},
                n8n_service_url="http://fake",
            )
        )
        await session.commit()
    scope = InternalJobScope(
        workspace_id=workspace_id,
        actor_user_id=user_id,
        membership_revision=revision,
        source_id=source_id,
        source_generation=generation,
    )
    async with factory() as session:
        request = await scheduler.request_collection(
            session, scope, source_id, "manual", 3, multi_workspace_enabled=FLAG
        )
    async with factory() as session:
        admission = await scheduler.admit_collection_request(
            session, request.request_id, multi_workspace_enabled=FLAG
        )
    assert admission is not None
    now = datetime.now(UTC)
    payload = ReceiveBatch(
        source_id=source_id,
        source_generation=generation,
        connector_revision=3,
        batch_key=f"k-{uuid4().hex}",
        cursor_before=None,
        cursor_after="c1",
        records=[
            IngestionRecord(
                provider_id="r1", version="1", content="hello", observed_at=now, collected_at=now
            )
        ],
    )
    async with factory() as session:
        _batch, run = await ingestion.receive_batch(
            session,
            payload,
            None,
            scope=scope,
            multi_workspace_enabled=FLAG,
            request_ref=CollectionRequestRef(
                request_id=request.request_id, admission_token=admission.admission_token
            ),
            state_update=CollectionStateUpdate(stats=stats),
        )
    return source_id, run.id, request.request_id


async def _receipt_stats(engine: AsyncEngine, request_id: UUID) -> tuple[int, Any]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT collection_stats FROM ingestion_collection_receipts WHERE request_id = :r"
                ),
                {"r": request_id},
            )
        ).all()
    return len(rows), rows[0][0] if rows else None


async def _run_stats(client: AsyncClient, source_id: UUID, run_id: UUID) -> Any:
    response = await client.get(f"/api/v1/ingestion/sources/{source_id}/runs")
    response.raise_for_status()
    (item,) = [i for i in response.json()["items"] if i["run_id"] == str(run_id)]
    return item["collection_stats"]


@pytest.mark.asyncio
async def test_settled_stats_persist_on_receipt_and_surface_in_runs_api(
    committed_engine: AsyncEngine,
    ready_owner_client: AsyncClient,
) -> None:
    stats = {"new": 3, "backlog": 1, "failed_sections": ["a"]}
    source_id, run_id, request_id = await _collect(committed_engine, ready_owner_client, stats)
    assert await _receipt_stats(committed_engine, request_id) == (1, stats)
    assert await _run_stats(ready_owner_client, source_id, run_id) == stats


@pytest.mark.asyncio
async def test_oversized_stats_are_dropped_but_the_run_still_settles(
    committed_engine: AsyncEngine,
    ready_owner_client: AsyncClient,
) -> None:
    source_id, run_id, request_id = await _collect(
        committed_engine, ready_owner_client, {"failed_sections": ["x" * 40] * 60}
    )
    assert await _receipt_stats(committed_engine, request_id) == (1, None)
    assert await _run_stats(ready_owner_client, source_id, run_id) is None


@pytest.mark.asyncio
async def test_settlement_without_stats_reports_null(
    committed_engine: AsyncEngine,
    ready_owner_client: AsyncClient,
) -> None:
    source_id, run_id, request_id = await _collect(committed_engine, ready_owner_client, None)
    assert await _receipt_stats(committed_engine, request_id) == (1, None)
    assert await _run_stats(ready_owner_client, source_id, run_id) is None
