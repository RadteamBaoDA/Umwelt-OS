"""C4 backend transition against the disposable PostgreSQL: real locks and rows, fake n8n only.

The saga functions run in-process under a W2 internal-job scope (the way the worker sweep runs them);
n8n is a fake whose stop call can be lost. Sources are created through the real owner API.
"""

import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import core.auth.models  # noqa: F401  # registers the owner table for foreign keys
from core.worker_cursors import STATE_KEY
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import InternalJobScope
from modules.connectors import activation, backends, provisioning, upgrade_templates, worker
from modules.connectors.models import ConnectorProvisioning, ConnectorSchedule
from modules.sources import public as sources

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)
FLAG = False  # the disposable stack runs the default single-workspace rollout


class FakeN8n:
    """Records stop calls; ``lose_stop`` makes the response vanish after the stop was sent."""

    def __init__(self, lose_stop: bool = False) -> None:
        self.lose_stop, self.calls = lose_stop, []

    async def set_active(self, workflow_id: str, active: bool) -> None:
        self.calls.append((workflow_id, active))
        if self.lose_stop:
            raise httpx.ReadTimeout("response lost")


def _settings() -> Any:
    return SimpleNamespace(
        multi_workspace_enabled=FLAG, n8n_api_key=SimpleNamespace(get_secret_value=lambda: "k"),
        n8n_service_url="http://fake")


class Env:
    def __init__(self, factory: async_sessionmaker, source_id: UUID, scope: InternalJobScope) -> None:
        self.factory, self.source_id, self.scope = factory, source_id, scope

    async def fence(self, session: Any) -> Any:
        fence = await read_access_fence(session, scope=self.scope, multi_workspace_enabled=FLAG)
        await session.rollback()
        return fence

    async def row(self) -> Any:
        async with self.factory() as session:
            row = await session.scalar(
                select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == self.source_id)
                .execution_options(populate_existing=True))
            assert row is not None
            snapshot = SimpleNamespace(**{c.key: getattr(row, c.key) for c in ConnectorProvisioning.__table__.columns})
            await session.rollback()
            return snapshot

    async def begin(self, target: str) -> None:
        revision = (await self.row()).desired_revision
        async with self.factory() as session:
            fence = await self.fence(session)
            await provisioning.begin_backend_transition_in_uow(
                session, self.source_id, revision, target, scope=self.scope, multi_workspace_enabled=FLAG,
                access_fence=fence)
            await session.commit()

    async def advance(self, api: Any) -> str:
        async with self.factory() as session:
            fence = await self.fence(session)
            return await provisioning.advance_backend_transition(
                session, self.source_id, api, scope=self.scope, multi_workspace_enabled=FLAG, access_fence=fence)

    async def admits(self, backend_revision: int | None = None) -> bool:
        """Collection fence plus backend readiness, exactly as the bearer routes and scheduler ask."""
        row = await self.row()
        async with self.factory() as session:
            source = await sources.get_connector_source(
                session, self.source_id, scope=self.scope, multi_workspace_enabled=FLAG)
            assert source is not None
            ok = await provisioning.require_collection_fence(
                session, source, source.generation, row.desired_revision, backend_revision=backend_revision,
                scope=self.scope, multi_workspace_enabled=FLAG)
            await session.rollback()
            return ok

    async def late_webhook(self) -> int | None:
        """HTTP status the n8n bearer gate answers to a webhook; None when it lets it through."""
        async with self.factory() as session:
            try:
                await provisioning.require_n8n_backend(session, self.source_id)
            except HTTPException as exc:
                return exc.status_code
            finally:
                await session.rollback()
        return None


@pytest.fixture
async def make_env(committed_engine: AsyncEngine, ready_owner_client: AsyncClient) -> AsyncIterator[Any]:
    made: list[UUID] = []
    factory = async_sessionmaker(committed_engine, expire_on_commit=False)

    async def make(**row: Any) -> Env:
        created = await ready_owner_client.post("/api/v1/sources", json={"type": "rss", "name": f"c4-{uuid4().hex[:8]}"})
        created.raise_for_status()
        source_id = UUID(created.json()["id"])
        made.append(source_id)
        async with committed_engine.connect() as connection:
            workspace_id, generation = (await connection.execute(
                text("SELECT workspace_id, generation FROM sources WHERE id = :i"), {"i": source_id})).one()
            user_id, revision = (await connection.execute(
                text("SELECT user_id, revision FROM workspace_memberships WHERE workspace_id = :w AND role = 'owner'"),
                {"w": workspace_id})).one()
        values: dict[str, Any] = {
            "source_id": source_id, "source_generation": generation, "desired_revision": 3, "applied_revision": 3,
            "desired_enabled": True, "state": "active", "execution_backend": "n8n", "backend_revision": 1,
            "applied_backend_revision": 1, "workflow_id": "wf-old", "workflow_name": "c4-old",
            "template_revision": 1, "applied_template_revision": 1, "desired_configuration": {}}
        values.update(row)
        async with factory() as session:
            session.add(ConnectorProvisioning(**values))
            await session.commit()
        scope = InternalJobScope(
            workspace_id=workspace_id, actor_user_id=user_id, membership_revision=revision,
            source_id=source_id, source_generation=generation)
        return Env(factory, source_id, scope)

    yield make
    async with committed_engine.begin() as connection:
        for source_id in made:  # stale rows must not leak into the next test's bulk upgrade
            await connection.execute(text("DELETE FROM connector_provisioning WHERE source_id = :i"), {"i": source_id})


@pytest.mark.asyncio
async def test_lost_stop_response_is_reconciliation_and_neither_backend_admits(make_env: Any) -> None:
    env = await make_env()
    assert await env.admits()  # healthy n8n source
    await env.begin("native")
    n8n = FakeN8n(lose_stop=True)
    assert await env.advance(n8n) == "reconciliation_required"
    row = await env.row()
    assert n8n.calls == [("wf-old", False)] and row.error_code == "deactivation_unconfirmed"
    assert row.execution_backend == "n8n" and row.transition_phase == "reconciliation_required"
    assert not backends.backend_admits(row) and not await env.admits()
    assert await env.late_webhook() == 409  # n8n side stays shut while the stop is unconfirmed
    async with env.factory() as session:  # native was never activated: no schedule exists or is enabled
        schedule = await session.get(ConnectorSchedule, env.source_id)
        assert schedule is None or not schedule.enabled
    assert await env.advance(FakeN8n()) == "reconciliation_required"  # a sweep never guesses past it


@pytest.mark.asyncio
async def test_late_webhook_from_old_token_only_template_is_rejected(make_env: Any) -> None:
    stale = await make_env(applied_template_revision=0, template_revision=0)
    assert not await stale.admits()  # the template is too old to admit
    healthy = await make_env()
    assert await healthy.admits() and await healthy.late_webhook() is None
    await healthy.begin("native")
    assert not await healthy.admits(backend_revision=1)  # an old workflow still carries its build-time revision
    assert await healthy.late_webhook() == 409
    assert await healthy.advance(FakeN8n()) in ("idle", "activating_new")
    row = await healthy.row()
    assert row.execution_backend == "native" and row.backend_revision == 2
    assert not await healthy.admits(backend_revision=1) and await healthy.late_webhook() == 409


@pytest.mark.asyncio
async def test_crash_between_draining_and_deactivating_old_is_resumed_by_the_sweep(
    make_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await make_env()
    await env.begin("native")  # the process dies here: durable phase is draining, nothing was sent
    assert (await env.row()).transition_phase == "draining" and not await env.admits()
    n8n = FakeN8n()
    monkeypatch.setattr(worker, "N8nApi", lambda *_a, **_k: n8n)
    advanced = await worker._advance_transitions(env.factory, _settings(), {STATE_KEY: {}})
    assert advanced >= 1 and n8n.calls == [("wf-old", False)]
    row = await env.row()
    assert row.transition_phase == "idle" and row.execution_backend == "native" and row.old_workflow_id is None
    assert backends.backend_admits(row) and row.applied_backend_revision == row.backend_revision == 2


@pytest.mark.asyncio
async def test_native_activation_without_a_workflow_admits(make_env: Any) -> None:
    env = await make_env(
        workflow_id=None, workflow_name=None, desired_enabled=False, state="saved_not_active",
        applied_revision=0, applied_backend_revision=0, applied_template_revision=0, template_revision=0)
    assert not await env.admits()
    async with env.factory() as session:
        fence = await env.fence(session)
        source_fence, row, _slots = await provisioning.lock_connector(
            session, env.source_id, scope=env.scope, multi_workspace_enabled=FLAG, expected_access_fence=fence)
        assert source_fence is not None and row is not None
        assert await activation.activate_native_in_uow(
            session, env.source_id, source_fence, row, scope=env.scope, multi_workspace_enabled=FLAG) == ""
        await session.commit()
    row = await env.row()
    assert row.execution_backend == "native" and row.workflow_id is None and backends.backend_admits(row)
    assert await env.admits() and await env.late_webhook() == 409
    async with env.factory() as session:
        schedule = await session.get(ConnectorSchedule, env.source_id)
        assert schedule is not None and schedule.enabled


@pytest.mark.asyncio
async def test_bulk_template_upgrade_stops_the_old_workflow_and_waits_for_the_owner(
    make_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = await make_env(applied_template_revision=0, template_revision=0)
    n8n = FakeN8n()
    monkeypatch.setattr(upgrade_templates, "N8nApi", lambda *_a, **_k: n8n)
    dry = await upgrade_templates.run(stale.factory, _settings(), limit=500)
    assert dry["dry_run"] and dry["would_upgrade"] >= 1 and n8n.calls == []
    assert (await stale.row()).transition_phase == "idle"
    done = await upgrade_templates.run(stale.factory, _settings(), apply=True, limit=500)
    assert done["upgraded"] >= 1 and done["reconciliation_required"] == 0
    row = await stale.row()
    assert n8n.calls == [("wf-old", False)] and row.execution_backend == "n8n"  # never native
    assert row.transition_phase == "activating_new" and row.error_code == "activation_required"
    assert not await stale.admits()  # waits for the owner's Activate, which finalizes the template
    again = await upgrade_templates.run(stale.factory, _settings(), apply=True, limit=500)
    assert again["examined"] == 0  # idempotent: nothing stale and idle remains
