"""Bulk entity owner contracts equal the singular contracts per id, against real Postgres."""

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core.workspaces.schemas import InternalJobScope
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships import public as relationships

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

FLAG = False  # the disposable stack runs the default single-workspace rollout


async def _scope_and_ids(engine: AsyncEngine) -> tuple[InternalJobScope, list[UUID]]:
    """Return the first workspace's owner scope and up to 20 of its entity ids plus one unknown id (read-only)."""
    async with engine.connect() as connection:
        workspace_id, user_id, revision = (
            await connection.execute(
                text(
                    "SELECT m.workspace_id, m.user_id, m.revision FROM workspace_memberships m "
                    "WHERE m.role = 'owner' ORDER BY m.workspace_id LIMIT 1"
                )
            )
        ).one()
        ids = [
            row[0]
            for row in await connection.execute(
                text("SELECT id FROM entities WHERE workspace_id = :w ORDER BY id LIMIT 20"),
                {"w": workspace_id},
            )
        ]
    scope = InternalJobScope(
        workspace_id=workspace_id, actor_user_id=user_id, membership_revision=revision
    )
    return scope, [*ids, uuid4()]  # the unknown id must yield None in both contracts


@pytest.mark.asyncio
async def test_list_entity_evidence_bulk_matches_singular(committed_engine: AsyncEngine) -> None:
    scope, ids = await _scope_and_ids(committed_engine)
    ids = [*ids, ids[0]]  # duplicates are allowed and stay aligned
    async with async_sessionmaker(committed_engine, expire_on_commit=False)() as session:
        bulk = await entities.list_entity_evidence_bulk(
            session, ids, 5, scope=scope, multi_workspace_enabled=FLAG
        )
        singular = [
            await entities.list_entity_evidence(
                session, i, 5, scope=scope, multi_workspace_enabled=FLAG
            )
            for i in ids
        ]
    assert bulk == singular


@pytest.mark.asyncio
async def test_get_neighbors_bulk_matches_singular(committed_engine: AsyncEngine) -> None:
    scope, ids = await _scope_and_ids(committed_engine)
    ids = [*ids, ids[0]]
    async with async_sessionmaker(committed_engine, expire_on_commit=False)() as session:
        bulk = await relationships.get_neighbors_bulk(
            session, ids, 5, scope=scope, multi_workspace_enabled=FLAG
        )
        singular = [
            await relationships.get_neighbors(
                session, i, 5, scope=scope, multi_workspace_enabled=FLAG
            )
            for i in ids
        ]
    assert bulk == singular
