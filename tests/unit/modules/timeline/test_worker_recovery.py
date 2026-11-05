"""Characterization of ``recover_timeline_extraction_work``: scheduling, parking, cursors and requeue."""

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.timeline import worker

WS = uuid4()
SCOPE = SimpleNamespace(workspace_id=WS)


class _Factory:
    """Yield a fresh session mock per ``async with``; remember them all."""

    def __init__(self) -> None:
        self.sessions: list[MagicMock] = []
        self._current: MagicMock | None = None

    def __call__(self) -> "_Factory":
        session = MagicMock()
        session.commit, session.rollback = AsyncMock(), AsyncMock()
        session.scalar = AsyncMock(return_value=None)
        self.sessions.append(session)
        self._current = session
        return self

    async def __aenter__(self) -> MagicMock:
        assert self._current is not None
        return self._current

    async def __aexit__(self, *exc: object) -> None:
        return None


def _ref(**over: Any) -> SimpleNamespace:
    base = {
        "source_id": uuid4(),
        "document_id": uuid4(),
        "document_version_id": uuid4(),
        "source_generation": 3,
        "raw_uri": "raw",
        "mime_type": "text/plain",
        "local_only": False,
    }
    return SimpleNamespace(**{**base, **over})


async def _recover(
    ref: SimpleNamespace,
    *,
    source: SimpleNamespace | None,
    current: SimpleNamespace | None,
    locked: bool = True,
    next_cursor: str | None = "n",
    blocked: list[Any] | None = None,
    recoverable: list[Any] | None = None,
    requeue: bool = True,
) -> SimpleNamespace:
    factory = _Factory()
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"c1")
    redis.set, redis.delete, redis.enqueue_job = AsyncMock(), AsyncMock(), AsyncMock()
    ctx = {
        "session_factory": factory,
        "redis": redis,
        "settings": SimpleNamespace(multi_workspace_enabled=True),
    }
    probes = SimpleNamespace(
        schedule=AsyncMock(return_value=uuid4()),
        defer=AsyncMock(),
        requeue=AsyncMock(return_value=requeue),
        factory=factory,
        redis=redis,
    )
    patches = [
        patch.object(worker, "_recovery_workspace_ids", AsyncMock(return_value=[WS])),
        patch.object(worker, "_workspace_job_scope", AsyncMock(return_value=SCOPE)),
        patch.object(worker.settings_public, "module_is_enabled", AsyncMock(return_value=True)),
        patch.object(
            worker.documents,
            "list_ready_version_refs",
            AsyncMock(return_value=([ref], next_cursor)),
        ),
        patch.object(worker.workspaces, "read_access_fence", AsyncMock(return_value="fence")),
        patch.object(worker.sources, "lock_source", AsyncMock(return_value=source)),
        patch.object(worker.documents, "get_ready_version_ref", AsyncMock(return_value=current)),
        patch.object(
            worker.documents, "lock_document_for_extraction", AsyncMock(return_value=locked)
        ),
        patch.object(worker.timeline, "schedule_extraction_work", probes.schedule),
        patch.object(
            worker.timeline, "list_blocked_extraction_work", AsyncMock(return_value=blocked or [])
        ),
        patch.object(worker.timeline, "defer_blocked_extraction_recheck", probes.defer),
        patch.object(worker.timeline, "requeue_blocked_extraction_work", probes.requeue),
        patch.object(
            worker.timeline,
            "list_recoverable_extraction_work",
            AsyncMock(return_value=recoverable or []),
        ),
        patch.object(
            worker.settings_public,
            "get_ai_execution_config",
            AsyncMock(
                return_value=SimpleNamespace(
                    endpoint_destination_id="d",
                    defaults=SimpleNamespace(resolve=lambda _r: "mapping"),
                )
            ),
        ),
        patch.object(worker.settings_public, "derive_for_role", lambda cfg, _r: cfg),
        patch.object(worker, "_dependency_snapshot", AsyncMock(return_value=("fp2", True))),
        patch.object(worker, "_allowed", lambda *_a: True),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        probes.count = await worker.recover_timeline_extraction_work(ctx)
    return probes


def _source(ref: SimpleNamespace, **over: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "id": ref.source_id,
            "status": "active",
            "generation": 3,
            "local_only": False,
            "graph_enabled": True,
            **over,
        }
    )


@pytest.mark.asyncio
async def test_current_ref_is_scheduled_and_cursor_saved() -> None:
    """A verified ref is scheduled once; a next cursor is stored and recoverable work is enqueued."""
    ref = _ref()
    work_a = uuid4()
    probes = await _recover(ref, source=_source(ref), current=ref, recoverable=[work_a, work_a])
    probes.schedule.assert_awaited_once()
    probes.redis.set.assert_awaited_once_with(f"bbd:timeline-extraction:ready-cursor:{WS}", "n")
    assert probes.count == 1
    probes.redis.enqueue_job.assert_awaited_once()
    assert probes.redis.enqueue_job.await_args.kwargs["_job_id"] == f"timeline-extraction:{work_a}"
    assert all(s.commit.await_count >= 1 for s in probes.factory.sessions[:2])


@pytest.mark.asyncio
async def test_exhausted_cursor_is_deleted_and_inactive_source_is_skipped() -> None:
    """No next cursor deletes the stored one; an inactive source never schedules."""
    ref = _ref()
    probes = await _recover(
        ref, source=_source(ref, status="disabled"), current=ref, next_cursor=None
    )
    probes.schedule.assert_not_awaited()
    probes.redis.delete.assert_awaited_once()
    probes.redis.set.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["drift", "unlocked", "graph_off"])
async def test_drift_unlocked_and_graph_disabled_refs_do_not_schedule(case: str) -> None:
    """Generation drift, a failed document lock, or a graph-off source commit without scheduling."""
    ref = _ref()
    source = _source(ref, graph_enabled=case != "graph_off")
    current = _ref(
        document_id=ref.document_id,
        source_id=ref.source_id,
        source_generation=4 if case == "drift" else 3,
    )
    probes = await _recover(ref, source=source, current=current, locked=case != "unlocked")
    probes.schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_only_ref_is_parked_as_blocked() -> None:
    """A local-only version's work row is parked, with the lease cleared and retry pushed to max."""
    ref = _ref(local_only=True)
    work = SimpleNamespace(
        status="pending",
        error_code=None,
        next_attempt_at=None,
        lease_owner="x",
        lease_expires_at=datetime.now(UTC),
    )
    factory_holder: list[Any] = []
    original = _Factory.__call__

    def call(self: _Factory) -> _Factory:
        result = original(self)
        self._current.scalar = AsyncMock(return_value=work)  # type: ignore[union-attr]
        factory_holder.append(self)
        return result

    with patch.object(_Factory, "__call__", call):
        await _recover(
            ref,
            source=_source(ref),
            current=_ref(document_id=ref.document_id, source_id=ref.source_id, local_only=True),
        )
    assert (work.status, work.error_code) == ("blocked", "local_only_source")
    assert work.next_attempt_at == datetime.max.replace(tzinfo=UTC)
    assert work.lease_owner is None and work.lease_expires_at is None


@pytest.mark.asyncio
async def test_blocked_items_requeue_only_for_policy_codes_and_changed_fingerprint() -> None:
    """Policy-blocked items are requeued when permitted; other codes are ignored; failures defer."""
    ref = _ref()
    ready = _ref(document_id=uuid4(), source_id=ref.source_id)
    blocked_ok, blocked_other, blocked_nofp = uuid4(), uuid4(), uuid4()
    items = [
        (blocked_ok, ready.document_version_id, 3, "ai_policy_denied", "fp1"),
        (blocked_other, ready.document_version_id, 3, "graph_disabled", "fp1"),
        (blocked_nofp, ready.document_version_id, 3, "ai_policy_denied", None),
    ]
    current = _ref(document_id=ready.document_id, source_id=ref.source_id)
    probes = await _recover(
        ref, source=_source(ref, graph_enabled=False), current=current, blocked=items
    )
    # ``ready`` and ``current`` are the same mocked ref here, so the requeue path is taken for one item.
    probes.requeue.assert_awaited_once()
    assert probes.requeue.await_args.args[1:4] == (blocked_ok, "fp1", "fp2")
    probes.defer.assert_not_awaited()
    assert probes.count == 1  # the requeued ID is enqueued

    denied = await _recover(
        ref, source=_source(ref, graph_enabled=False), current=current, blocked=items, requeue=False
    )
    denied.defer.assert_awaited_once()
