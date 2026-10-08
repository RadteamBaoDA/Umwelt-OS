"""Recipe J contracts for durable browser jobs: original-epoch admission and insert stamping."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.agents.public import BrowserRunAuthorization
from modules.connectors.public import AgentBrowserScope
from modules.tools import browser_public
from modules.tools.browser_public import (
    BrowserReadArgs,
    BrowserReadBudget,
    execute_browser_read,
    submit_browser_read_in_uow,
)

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)
FENCE = AccessFence(WS, 7, 3, 5)
FLAG = False


def _job(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid4(), "operation_id": uuid4(), "run_id": uuid4(), "tool_slot": 1, "source_id": uuid4(),
        "status": "queued", "actual_pages": 0, "actual_bytes": 0, "result_hash": None, "error_code": None,
        "expires_at": datetime.now(UTC) + timedelta(hours=1), "claim_generation": 1,
        "auth_session_hash": "h", "profile_id": "p", "workspace_id": WS, "owner_id": 7,
        "membership_revision": 3, "configuration_revision": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _factory(job: SimpleNamespace) -> Any:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=job)

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return factory


async def _run(job: SimpleNamespace, lock_result: AccessFence | None) -> tuple[Any, AsyncMock, AsyncMock]:
    lock_source = AsyncMock()
    lock_fence = AsyncMock(return_value=lock_result)
    with (
        patch("core.config.Settings", MagicMock()),
        patch("modules.sources.public.lock_source", lock_source),
        patch("modules.tools.browser_public.workspaces.lock_access_fence", lock_fence),
        patch("httpx.AsyncClient", side_effect=AssertionError("no browser HTTP")),
    ):
        result = await execute_browser_read(
            _factory(job), job.id, 1, deadline=1e12, multi_workspace_enabled=FLAG,
        )
    return result, lock_source, lock_fence


@pytest.mark.asyncio
async def test_null_epoch_job_is_quarantined_without_lock_or_http() -> None:
    job = _job(membership_revision=None, configuration_revision=None)
    result, lock_source, lock_fence = await _run(job, FENCE)
    assert job.status == "failed" and job.error_code == "authority_revoked"
    assert result.job.error_code == "authority_revoked"
    lock_source.assert_not_called()
    lock_fence.assert_not_called()


@pytest.mark.asyncio
async def test_changed_fence_aborts_before_source_lock() -> None:
    job = _job()
    result, lock_source, lock_fence = await _run(job, AccessFence(WS, 7, 4, 5))
    assert job.status == "failed" and result.job.error_code == "authority_revoked"
    assert lock_fence.await_args.kwargs["expected"] == FENCE
    lock_source.assert_not_called()


@pytest.mark.asyncio
async def test_submit_insert_stamps_workspace_owner_and_original_epoch() -> None:
    source_id = uuid4()
    authorization = BrowserRunAuthorization(
        7, uuid4(), 1, "arg-hash", "session-hash", uuid4(), "specialist", "rev-hash",
        frozenset({source_id}), 1, 1, 3, 1_000_000, 30,
    )
    grant = AgentBrowserScope(
        workspace_id=WS, source_id=source_id, source_generation=1, connector_revision=1, grant_revision=1,
        scope_hash="s", origin="https://example.com", path_prefix="/", local_only=False, enabled=True,
    )
    budget = BrowserReadBudget(
        authorization=authorization, operation_id=uuid4(), max_bytes=100_000, max_active_seconds=30,
        service_token_hash="a" * 64, expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = MagicMock(side_effect=lambda row: setattr(row, "actual_pages", 0) or setattr(row, "actual_bytes", 0))
    await submit_browser_read_in_uow(
        session, authorization.run_id, 1, "session-hash", grant,
        BrowserReadArgs(source_id=source_id, max_pages=1), budget,
        scope=OWNER, multi_workspace_enabled=FLAG, access_fence=FENCE,
    )
    row = session.add.call_args.args[0]
    assert (row.workspace_id, row.owner_id) == (WS, 7)
    assert (row.membership_revision, row.configuration_revision) == (3, 5)
    text = str(session.scalar.call_args.args[0].compile())
    assert "browser_read_jobs.workspace_id" in text and "browser_read_jobs.owner_id" in text
    assert browser_public._actor(OWNER) == 7


def _submit_inputs(grant_workspace: Any = WS) -> tuple[Any, ...]:
    source_id = uuid4()
    authorization = BrowserRunAuthorization(
        7, uuid4(), 1, "arg-hash", "session-hash", uuid4(), "specialist", "rev-hash",
        frozenset({source_id}), 1, 1, 3, 1_000_000, 30,
    )
    grant = AgentBrowserScope(
        workspace_id=grant_workspace, source_id=source_id, source_generation=1, connector_revision=1,
        grant_revision=1, scope_hash="s", origin="https://example.com", path_prefix="/", local_only=False,
        enabled=True,
    )
    budget = BrowserReadBudget(
        authorization=authorization, operation_id=uuid4(), max_bytes=100_000, max_active_seconds=30,
        service_token_hash="a" * 64, expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return authorization, grant, budget, BrowserReadArgs(source_id=source_id, max_pages=1)


@pytest.mark.asyncio
async def test_submit_rejects_grant_from_another_workspace_before_any_query() -> None:
    authorization, grant, budget, args = _submit_inputs(grant_workspace=uuid4())
    session = AsyncMock()
    with pytest.raises(PermissionError):
        await submit_browser_read_in_uow(
            session, authorization.run_id, 1, "session-hash", grant, args, budget,
            scope=OWNER, multi_workspace_enabled=FLAG, access_fence=FENCE,
        )
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_job_authority_maps_admission_409_to_none_and_reraises_other_errors() -> None:
    from fastapi import HTTPException

    job = _job()
    for status in (401, 403, 404, 409):
        with patch("modules.tools.browser_public.workspaces.lock_access_fence", AsyncMock(side_effect=HTTPException(status))):
            assert await browser_public._job_authority(AsyncMock(), job, multi_workspace_enabled=FLAG) is None
    with patch("modules.tools.browser_public.workspaces.lock_access_fence", AsyncMock(side_effect=HTTPException(500))), \
            pytest.raises(HTTPException):
        await browser_public._job_authority(AsyncMock(), job, multi_workspace_enabled=FLAG)
    with patch("modules.tools.browser_public.workspaces.lock_access_fence", AsyncMock(return_value=FENCE)):
        assert await browser_public._job_authority(AsyncMock(), job, multi_workspace_enabled=FLAG) == (
            SimpleNamespace_scope(), FENCE,
        )


def SimpleNamespace_scope() -> Any:  # helper reads like a value in the assertion above
    from core.workspaces.schemas import InternalJobScope

    return InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=3)


@pytest.mark.asyncio
async def test_current_authority_null_epoch_is_denied_without_source_lock() -> None:
    from modules.tools import browser_control

    job = _job(membership_revision=None, configuration_revision=None)
    lock_source = AsyncMock()
    with patch("modules.sources.public.lock_source", lock_source):
        result = await browser_control._current_authority(AsyncMock(), job, multi_workspace_enabled=FLAG)  # type: ignore[arg-type]
    assert result == (False, None, None, None)
    lock_source.assert_not_called()


def _control_request(job: SimpleNamespace) -> tuple[Any, Any, str]:
    from core.config import Settings

    token = "ab" * 32
    job.service_token_hash = __import__("hashlib").sha256(token.encode("ascii")).hexdigest()
    settings = MagicMock(spec=Settings)
    settings.browser_shared_token = SecretStr("svc")
    settings.multi_workspace_enabled = FLAG
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=job)

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=factory)))
    return request, session, token


def _register_event(job: SimpleNamespace) -> Any:
    from modules.tools.browser_control import BrowserControlEvent

    return BrowserControlEvent(
        event="register", operation_id=job.operation_id, claim_generation=1, service_instance_id="svc-1",
        request_ordinal=0,
    )


def _control_job() -> SimpleNamespace:
    return _job(
        cancel_requested=False, service_instance_id=None, service_token_hash="", request_ordinal=0,
        arguments_hash="a", conversation_id=uuid4(), profile_revision_hash="r", authorized_source_ids=[],
        max_active_seconds=30, scope_hash="s", source_generation=1, connector_revision=1, grant_revision=1,
        max_pages=1, max_bytes=1000,
    )


@pytest.mark.asyncio
async def test_control_register_with_revoked_authority_marks_job_and_returns_403() -> None:
    from fastapi import HTTPException

    from modules.tools import browser_control

    job = _control_job()
    request, session, token = _control_request(job)
    with patch("modules.settings.public.register_request_activity", AsyncMock()), \
            patch.object(browser_control, "_browser_network_verified", return_value=True), \
            patch.object(browser_control, "_current_authority", AsyncMock(return_value=(False, None, None, None))), \
            pytest.raises(HTTPException) as exc:
        await browser_control.browser_control_event(
            job.id, _register_event(job), request, authorization="Bearer svc", job_token=token,
        )
    assert exc.value.status_code == 403
    assert (job.status, job.error_code) == ("failed", "authority_revoked")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_control_register_commits_with_replay_under_the_original_fence() -> None:
    from modules.tools import browser_control

    job = _control_job()
    request, session, token = _control_request(job)
    scope = SimpleNamespace_scope()
    replay = AsyncMock()
    with patch("modules.settings.public.register_request_activity", AsyncMock()), \
            patch.object(browser_control, "_browser_network_verified", return_value=True), \
            patch.object(browser_control, "_current_authority", AsyncMock(return_value=(True, MagicMock(), scope, FENCE))), \
            patch.object(browser_control, "register_remote_heavy_in_uow", AsyncMock()), \
            patch.object(browser_control, "commit_with_replay", replay):
        out = await browser_control.browser_control_event(
            job.id, _register_event(job), request, authorization="Bearer svc", job_token=token,
        )
    assert out == {"allowed": True} and job.status == "running"
    assert replay.await_args.kwargs["access_fence"] == FENCE and replay.await_args.kwargs["scope"] == scope
    session.commit.assert_not_awaited()
