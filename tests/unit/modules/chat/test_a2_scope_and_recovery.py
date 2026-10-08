"""A2: scoped chat worker admission (S1/D6), account+workspace SSE admission, export scoping, Q1 V-proofs."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import InternalJobScope
from modules.chat import public as chat_public
from modules.chat import routes, worker
from modules.chat.models import Message
from tests.unit.modules.chat.test_p14_generation_worker import _Gen

WS = uuid4()
SCOPE = InternalJobScope(workspace_id=WS, actor_user_id=1, membership_revision=1)


class _Cm:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_exc: object) -> None:
        return None


# ------------------------------------------------------------------ admission


async def test_admit_job_rejects_foreign_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = SimpleNamespace(user_id=2, membership_revision=1)
    monkeypatch.setattr(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner))
    with pytest.raises(worker.PrivacyFenceChanged):
        await worker._admit_job(lambda: _Cm(MagicMock()), WS, 1)  # type: ignore[arg-type]


async def test_admit_job_denied_by_access_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = SimpleNamespace(user_id=1, membership_revision=3)
    monkeypatch.setattr(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner))
    monkeypatch.setattr(worker.workspaces, "authorize_internal_job",
                        AsyncMock(side_effect=HTTPException(status_code=404)))
    with pytest.raises(worker.PrivacyFenceChanged):
        await worker._admit_job(lambda: _Cm(MagicMock()), WS, 1)  # type: ignore[arg-type]


async def test_admit_job_builds_scope_from_run_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = SimpleNamespace(user_id=1, membership_revision=3)
    fence = object()
    monkeypatch.setattr(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner))
    monkeypatch.setattr(worker.workspaces, "authorize_internal_job", AsyncMock(return_value=fence))
    session = MagicMock()
    session.rollback = AsyncMock()
    scope, got = await worker._admit_job(lambda: _Cm(session), WS, 1)  # type: ignore[arg-type]
    assert scope == InternalJobScope(workspace_id=WS, actor_user_id=1, membership_revision=3) and got is fence
    session.rollback.assert_awaited_once()  # no lock survives into model I/O


async def test_lock_live_response_fails_when_access_fence_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.workspaces, "lock_access_fence",
                        AsyncMock(side_effect=HTTPException(status_code=409)))
    with pytest.raises(worker.PrivacyFenceChanged):
        await worker._lock_live_response(
            MagicMock(), uuid4(), uuid4(), {}, scope=SCOPE, access_fence=object(),  # type: ignore[arg-type]
        )


async def test_lock_live_response_requires_admitted_job() -> None:
    with pytest.raises(worker.PrivacyFenceChanged):
        await worker._lock_live_response(MagicMock(), uuid4(), uuid4(), {}, scope=None, access_fence=None)


async def test_privacy_fence_without_scope_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    with pytest.raises(worker.PrivacyFenceChanged):
        await worker._require_privacy_fence(MagicMock(), {}, scope=None)


async def test_admission_denied_redacts_and_never_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])
    monkeypatch.setattr(worker, "_admit_job", AsyncMock(side_effect=worker.PrivacyFenceChanged("denied")))
    redact = AsyncMock()
    monkeypatch.setattr(worker, "_mark_privacy_cancelled", redact)
    built: list[Any] = []
    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: built.append(kw))
    await gen.run()
    redact.assert_awaited_once()
    assert built == [] and not any(isinstance(m, Message) for m in gen.added)


async def test_gateway_and_config_use_the_job_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])
    monkeypatch.setattr(worker, "_admit_job", AsyncMock(return_value=(SCOPE, object())))
    seen: dict[str, Any] = {}

    def build(**kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(stream=gen.stream)

    monkeypatch.setattr(worker, "ModelGateway", build)
    await gen.run()
    assert seen["scope"] is SCOPE
    config_call = worker.settings_public.get_ai_execution_config
    assert config_call.await_args.kwargs["scope"] is SCOPE  # type: ignore[attr-defined]


# ----------------------------------------------------- SSE / export scoping


async def test_auth_row_requires_active_account_and_default_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.auth.public as auth_public

    h = "a" * 64
    session = MagicMock()
    session.scalar = AsyncMock(return_value=7)
    monkeypatch.setattr(auth_public, "revalidate_account_session", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=object()))
    assert await routes._auth_row_current(session, h) == 7
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(side_effect=HTTPException(status_code=404)))
    assert await routes._auth_row_current(session, h) is None  # default workspace gone
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=object()))
    monkeypatch.setattr(auth_public, "revalidate_account_session", AsyncMock(return_value=False))
    assert await routes._auth_row_current(session, h) is None  # account disabled
    session.scalar = AsyncMock(return_value=None)
    assert await routes._auth_row_current(session, h) is None  # session expired
    assert await routes._auth_row_current(session, None) is None


async def test_run_scope_rejects_run_of_another_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = SimpleNamespace(workspace_id=WS)
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=ctx))
    assert await routes._run_scope(MagicMock(), SimpleNamespace(actor_user_id=1, workspace_id=WS)) is ctx  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as exc:
        await routes._run_scope(MagicMock(), SimpleNamespace(actor_user_id=1, workspace_id=uuid4()))  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_chat_export_scope_filters_by_workspace() -> None:
    now = datetime.now(UTC)
    clauses = chat_public._chat_export_scope(now, now, WS)
    compiled = clauses[0].compile()
    assert "chat_conversations.workspace_id" in str(compiled) and WS in compiled.params.values()


# --------------------------------------------------------- Q1 V-proofs


async def test_duplicate_dispatch_second_claim_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])
    gen.session.execute = AsyncMock(return_value=SimpleNamespace(fetchone=lambda: None))  # lost the claim
    built: list[Any] = []
    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: built.append(kw))
    await gen.run()
    assert built == [] and gen.added == []
    gen.failed.assert_not_awaited()


async def test_stale_late_completion_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery failed the run while the old worker still streamed: its final publication must not land."""
    gen = _Gen(monkeypatch, ["a", "b"])
    calls = {"n": 0}

    async def lock(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 3:  # claim-time read and first flush succeed; later publications find the run terminal
            raise worker.ResponseNoLongerActive("terminal")
        return True, MagicMock()

    monkeypatch.setattr(worker, "_lock_live_response", lock)
    await gen.run()
    assert not any(isinstance(m, Message) for m in gen.added)
    assert not any(getattr(e, "event_type", "") == "message.done" for e in gen.added)
    gen.failed.assert_not_awaited()


async def test_redis_loss_does_not_cancel_or_fail_generation() -> None:
    redis = SimpleNamespace(exists=AsyncMock(side_effect=ConnectionError("down")))
    assert await worker.is_run_cancelled(uuid4(), redis) is False  # type: ignore[arg-type]


async def test_recover_continues_when_redis_enqueue_fails() -> None:
    first, second = uuid4(), uuid4()
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[[first, second], []])
    session.execute = AsyncMock(return_value=SimpleNamespace(all=list))
    redis = SimpleNamespace(enqueue_job=AsyncMock(side_effect=[ConnectionError("down"), None]))
    result = await worker.recover_chat_runs({"session_factory": lambda: _Cm(session), "redis": redis})
    assert result == {"requeued": 2, "failed": 0}
    assert redis.enqueue_job.await_count == 2  # one lost push does not block the next; next poll retries


# ------------------------------------------- requester must own the run (cross-account)


def _events_request() -> Any:
    return SimpleNamespace(cookies={}, app=SimpleNamespace(state=SimpleNamespace(
        session_factory=lambda: _Cm(MagicMock(scalar=AsyncMock(return_value=SimpleNamespace(
            id=uuid4(), actor_user_id=1, workspace_id=WS, conversation_id=uuid4()))))),
    ))


async def test_other_accounts_session_cannot_stream_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "_session_is_current", AsyncMock(return_value=2))  # requester is account 2
    monkeypatch.setattr(routes, "_run_scope", AsyncMock(return_value=SCOPE))  # old code stopped here
    with pytest.raises(HTTPException) as exc:
        await routes.get_response_events(uuid4(), _events_request(), MagicMock(close=AsyncMock()))  # type: ignore[arg-type]
    assert exc.value.status_code == 404


async def test_other_accounts_session_cannot_cancel_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock(scalar=AsyncMock(return_value=SimpleNamespace(
        id=uuid4(), actor_user_id=1, workspace_id=WS, conversation_id=uuid4())))
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=SimpleNamespace(workspace_id=WS)))
    with pytest.raises(HTTPException) as exc:
        await routes.cancel_response(uuid4(), MagicMock(), session, SimpleNamespace(owner_id=2))  # type: ignore[arg-type]
    assert exc.value.status_code == 404


@pytest.mark.parametrize("requester,expected", [(1, False), (2, True), (None, True)])
async def test_poll_ends_stream_when_requester_is_not_run_actor(
    monkeypatch: pytest.MonkeyPatch, requester: int | None, expected: bool,
) -> None:
    from modules.chat.models import Conversation, ResponseRun, StreamEvent

    fence = {"v": 1}
    run = SimpleNamespace(status="streaming", conversation_id=uuid4(), actor_user_id=1, workspace_id=WS,
                          retrieval_context={"_chat_privacy_fence": fence})
    rows = {ResponseRun: run, StreamEvent: None, Conversation: SimpleNamespace(ephemeral=False, expires_at=None)}
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=lambda stmt: rows[stmt.column_descriptions[0]["entity"]])
    monkeypatch.setattr(routes, "_run_scope", AsyncMock(return_value=SCOPE))
    monkeypatch.setattr(routes, "read_export_privacy", AsyncMock(return_value=object()))
    monkeypatch.setattr(routes, "_privacy_fence", lambda _p: fence)
    monkeypatch.setattr(routes, "_auth_row_current", AsyncMock(return_value=requester))
    assert await routes._poll_needs_lock(session, uuid4(), 0, "h") is expected
