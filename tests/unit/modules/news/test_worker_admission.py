"""N-F8/N-F12: news worker admission order and shared cursor state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.news import worker

WS = uuid4()
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=2, configuration_revision=3)
SCOPE = InternalJobScope(workspace_id=WS, actor_user_id=1, membership_revision=2)


def _session_factory() -> MagicMock:
    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
async def test_invalid_provenance_locks_admission_before_checkpoint_and_outbox() -> None:
    """With no provenance, original admission is locked before checkpoint and outbox locks."""
    order: list[str] = []
    delivery = SimpleNamespace(status="pending", type="news.document.ready", version=1, payload={})
    event = SimpleNamespace(status="pending", version=1, valid_payload=False)

    def rec(name: str, result: object = None) -> AsyncMock:
        async def _call(*_a: object, **_k: object) -> object:
            order.append(name)
            return result
        return AsyncMock(side_effect=_call)

    ctx = {"session_factory": _session_factory(), "settings": SimpleNamespace(multi_workspace_enabled=True)}
    with patch.object(worker.ingestion, "resolve_ingestion_event_scope", rec("scope", SCOPE)), \
         patch.object(worker.workspaces, "read_access_fence", rec("read", FENCE)), \
         patch.object(worker.workspaces, "lock_access_fence", rec("lock_admission", FENCE)), \
         patch.object(worker.settings_public, "module_is_enabled", rec("module", True)), \
         patch.object(worker.ingestion, "get_event_delivery", rec("delivery", delivery)), \
         patch.object(worker.ingestion, "resolve_ready_event_provenance", rec("provenance", None)), \
         patch.object(worker, "_ensure_recovery_checkpoint", rec("checkpoint")), \
         patch.object(worker.ingestion, "lock_news_document_ready_event", rec("outbox", event)), \
         patch.object(worker.ingestion, "fail_news_document_ready_event", rec("fail")), \
         patch.object(worker, "commit_with_replay", rec("commit")):
        await worker.process_news_document_ready(ctx, str(uuid4()))
    assert order.index("lock_admission") < order.index("delivery")
    assert order.index("lock_admission") < order.index("checkpoint") < order.index("outbox")


@pytest.mark.asyncio
async def test_cursor_survives_arq_style_ctx_copies_and_failed_redis_set() -> None:
    """Shared startup state keeps progress across per-job ctx copies even when Redis SET fails."""
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = RuntimeError("down")
    base = {"redis": redis, "w2_cursor_state": {}}
    first = uuid4()
    await worker._write_cursor({**base}, worker.RECOVERY_CURSOR_KEY, first)
    redis.get.return_value = str(uuid4()).encode()  # stale remote value
    assert await worker._read_cursor({**base}, worker.RECOVERY_CURSOR_KEY) == first


@pytest.mark.asyncio
async def test_cursor_rejects_unknown_key_and_bad_uuid() -> None:
    """Only fixed keys are accepted and malformed stored values read as no cursor."""
    redis = AsyncMock()
    redis.get.return_value = b"not-a-uuid"
    ctx = {"redis": redis, "w2_cursor_state": {}}
    with pytest.raises(KeyError):
        await worker._read_cursor(ctx, "other")
    assert await worker._read_cursor(ctx, worker.RECOVERY_INIT_CURSOR_KEY) is None


@pytest.mark.asyncio
async def test_recovery_locks_access_fence_before_checkpoint_read() -> None:
    """N-R3: the recovery loop takes the fence with expected= before touching the checkpoint."""
    order: list[str] = []
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [WS]))

    async def _scalar(*_a: object, **_k: object) -> None:
        order.append("checkpoint")

    session.scalar.side_effect = _scalar
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    lock = AsyncMock(side_effect=lambda *_a, **k: order.append("lock") or k["expected"])
    owner = SimpleNamespace(user_id=1, membership_revision=2)
    ctx = {"session_factory": MagicMock(return_value=cm), "redis": None, "w2_cursor_state": {},
           "settings": SimpleNamespace(multi_workspace_enabled=True)}
    with patch.object(worker.documents, "list_ready_document_workspace_ids", AsyncMock(return_value=[])), \
         patch.object(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
         patch.object(worker.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
         patch.object(worker.workspaces, "lock_access_fence", lock), \
         patch.object(worker.settings_public, "module_is_enabled", AsyncMock(return_value=True)):
        await worker.recover_news_work(ctx)
    assert lock.await_args.kwargs["expected"] == FENCE
    assert order == ["lock", "checkpoint"]


@pytest.mark.asyncio
async def test_cursor_is_forward_only_and_malformed_remote_keeps_local() -> None:
    """N-R4: an older overlapping job cannot overwrite a newer cursor; bad Redis data keeps local."""
    redis = AsyncMock()
    redis.get.return_value = None
    state: dict[str, str] = {}
    older, newer = {"redis": redis, "w2_cursor_state": state}, {"redis": redis, "w2_cursor_state": state}
    key = worker.RECOVERY_CURSOR_KEY
    await worker._read_cursor(older, key)
    await worker._read_cursor(newer, key)
    a, b = uuid4(), uuid4()
    await worker._write_cursor(newer, key, b)
    await worker._write_cursor(older, key, a)  # stale: must be skipped
    assert state[key] == str(b)
    await worker._write_cursor(newer, key, a)  # same job may keep advancing
    assert state[key] == str(a)
    redis.get.return_value = b"not-a-uuid"
    assert await worker._read_cursor({"redis": redis, "w2_cursor_state": state}, key) == a
    redis.set.side_effect = RuntimeError("down")
    await worker._write_cursor(newer, key, b)
    assert state[f"_unsynced:{key}"] == "1" and all(isinstance(v, str) for v in state.values())
