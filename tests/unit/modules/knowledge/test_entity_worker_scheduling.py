"""Characterization of ``process_document_ready`` and ``recover_entity_extraction_work`` branch outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.knowledge.entities import worker as w
from tests.unit.modules.knowledge._entity_worker_patch import patch_entity_worker

WS, DOC, SRC, VER = uuid4(), uuid4(), uuid4(), uuid4()


class _Factory:
    """Session factory returning one shared mock session so commit/rollback order is observable."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.commit = AsyncMock()
        self.session.rollback = AsyncMock()
        self.session.begin_nested = MagicMock(return_value=_Nested())

    def __call__(self) -> _Factory:
        return self

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Nested:
    """Async context manager standing in for ``session.begin_nested()``."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def _ready(**over: Any) -> SimpleNamespace:
    """Ready-version ref with consistent identity unless overridden."""
    base: dict[str, Any] = {
        "document_id": DOC,
        "source_id": SRC,
        "source_generation": 1,
        "raw_uri": "r",
        "mime_type": "text/plain",
        "local_only": False,
        "document_version_id": VER,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _source(**over: Any) -> SimpleNamespace:
    """Locked source fence."""
    base: dict[str, Any] = {
        "id": SRC,
        "status": "active",
        "generation": 1,
        "local_only": False,
        "graph_enabled": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _event(**over: Any) -> SimpleNamespace:
    """Delivery row for a v1 ``document.version.ready`` event."""
    base: dict[str, Any] = {
        "status": "pending",
        "type": "document.version.ready",
        "version": 1,
        "payload": {
            "document_version_id": str(VER),
            "document_id": str(DOC),
            "source_id": str(SRC),
            "source_generation": 1,
        },
    }
    base.update(over)
    return SimpleNamespace(**base)


def _ready_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Any,
    readies: list[Any],
    source: Any,
    lock_doc: bool = True,
) -> SimpleNamespace:
    """Patch collaborators of ``process_document_ready`` and return call spies."""
    factory = _Factory()
    work = SimpleNamespace(
        id=uuid4(),
        status="pending",
        error_code=None,
        next_attempt_at=None,
        dependency_fingerprint="x",
    )
    log: list[str] = []

    def spy(name: str, value: Any = None) -> AsyncMock:
        async def call(*_a: Any, **_k: Any) -> Any:
            log.append(name)
            return value

        return AsyncMock(side_effect=call)

    scope = SimpleNamespace(workspace_id=WS)
    monkeypatch.setattr(w.ingestion, "resolve_ingestion_event_scope", spy("scope", scope))
    monkeypatch.setattr(w.settings_public, "module_is_enabled", spy("enabled", True))
    monkeypatch.setattr(w.workspaces, "read_access_fence", spy("fence", "f"))
    monkeypatch.setattr(w.ingestion, "get_event_delivery", spy("event", event))
    monkeypatch.setattr(w.ingestion, "set_event_delivery", spy("set_event"))
    monkeypatch.setattr(w.ingestion, "mark_event_delivered", spy("delivered"))
    ready_iter = iter(readies)

    async def get_ready(*_a: Any, **_k: Any) -> Any:
        log.append("ready")
        return next(ready_iter)

    monkeypatch.setattr(w.documents, "get_ready_version_ref", get_ready)
    monkeypatch.setattr(w.sources, "lock_source", spy("lock_source", source))
    monkeypatch.setattr(w.documents, "lock_document_for_extraction", spy("lock_doc", lock_doc))
    monkeypatch.setattr(w.entities, "schedule_extraction_work", spy("schedule", work))
    import modules.connectors.public as connectors
    import modules.knowledge.temporal.public as temporal
    import modules.timeline.public as timeline

    monkeypatch.setattr(timeline, "schedule_extraction_work", spy("timeline", "tl"))
    monkeypatch.setattr(timeline, "block_local_only_extraction_work", spy("timeline_block"))
    monkeypatch.setattr(temporal, "schedule_version", spy("temporal"))
    monkeypatch.setattr(connectors, "map_github_version", spy("github"))
    redis = MagicMock()
    redis.enqueue_job = AsyncMock()
    ctx = {
        "session_factory": factory,
        "settings": SimpleNamespace(multi_workspace_enabled=False),
        "redis": redis,
    }
    return SimpleNamespace(ctx=ctx, log=log, work=work, redis=redis, session=factory.session)


async def test_ready_event_schedules_all_work_in_one_commit_then_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: lock order, scheduling order, a single commit, and both jobs enqueued on the model queue."""
    s = _ready_setup(
        monkeypatch, event=_event(), readies=[_ready(), _ready(), _ready()], source=_source()
    )
    await w.process_document_ready(s.ctx, str(uuid4()))
    assert s.log == [
        "scope",
        "enabled",
        "fence",
        "event",
        "ready",
        "lock_source",
        "ready",
        "lock_doc",
        "ready",
        "schedule",
        "timeline",
        "temporal",
        "github",
        "delivered",
    ]
    s.session.commit.assert_awaited_once()
    assert [c.args[0] for c in s.redis.enqueue_job.await_args_list] == [
        "process_entity_extraction_work",
        "process_timeline_extraction_work",
    ]
    assert (
        s.redis.enqueue_job.await_args_list[0].kwargs["_job_id"] == f"entity-extraction:{s.work.id}"
    )


async def test_ready_event_local_only_blocks_work_and_does_not_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only versions park both work rows (no fingerprint, ``datetime.max``) and never reach the model queue."""
    s = _ready_setup(
        monkeypatch,
        event=_event(),
        readies=[_ready(local_only=True)] * 3,
        source=_source(local_only=True),
    )
    await w.process_document_ready(s.ctx, str(uuid4()))
    assert (s.work.status, s.work.error_code, s.work.dependency_fingerprint) == (
        "blocked",
        "local_only_source",
        None,
    )
    assert s.work.next_attempt_at == datetime.max.replace(tzinfo=UTC)
    assert "timeline_block" in s.log
    s.redis.enqueue_job.assert_not_awaited()


async def test_ready_event_graph_disabled_maps_github_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph-disabled source schedules no model work but still runs the deterministic GitHub mapper."""
    s = _ready_setup(
        monkeypatch, event=_event(), readies=[_ready()] * 3, source=_source(graph_enabled=False)
    )
    await w.process_document_ready(s.ctx, str(uuid4()))
    assert s.log[-2:] == ["github", "delivered"]
    assert "schedule" not in s.log
    s.session.commit.assert_awaited_once()
    s.redis.enqueue_job.assert_not_awaited()


async def test_ready_event_unsupported_type_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown event type/version is marked failed and committed with no further reads."""
    s = _ready_setup(monkeypatch, event=_event(version=2), readies=[], source=None)
    await w.process_document_ready(s.ctx, str(uuid4()))
    assert s.log == ["scope", "enabled", "fence", "event", "set_event"]
    s.session.commit.assert_awaited_once()


@pytest.mark.parametrize("case", ["no_ready", "generation_drift", "doc_lock_failed", "late_drift"])
async def test_ready_event_stale_lineage_is_acknowledged(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every stale-lineage branch acknowledges the event, commits once and schedules nothing."""
    readies: list[Any] = [_ready()] * 3
    source = _source()
    lock_doc = True
    if case == "no_ready":
        readies = [None]
    elif case == "generation_drift":
        source = _source(generation=2)
        readies = [_ready(), _ready()]
    elif case == "doc_lock_failed":
        lock_doc = False
        readies = [_ready(), _ready()]
    else:
        readies = [_ready(), _ready(), _ready(source_generation=5)]
    s = _ready_setup(monkeypatch, event=_event(), readies=readies, source=source, lock_doc=lock_doc)
    await w.process_document_ready(s.ctx, str(uuid4()))
    assert s.log[-1] == "delivered"
    assert "schedule" not in s.log
    s.session.commit.assert_awaited_once()
    s.redis.enqueue_job.assert_not_awaited()


async def test_ready_event_delivered_or_missing_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivered or absent event returns without mutation."""
    for event in (None, _event(status="delivered")):
        s = _ready_setup(monkeypatch, event=event, readies=[], source=None)
        await w.process_document_ready(s.ctx, str(uuid4()))
        assert s.log == ["scope", "enabled", "fence", "event"]
        s.session.commit.assert_not_awaited()


def _recovery_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refs: list[Any],
    current: Any,
    source: Any,
    blocked: list[Any],
    work_ids: list[Any],
) -> SimpleNamespace:
    """Patch collaborators of ``recover_entity_extraction_work`` for one workspace."""
    factory = _Factory()
    scope = SimpleNamespace(workspace_id=WS)
    log: list[tuple[str, Any]] = []
    patch_entity_worker(monkeypatch, "_recovery_workspace_ids", AsyncMock(return_value=[WS]))
    patch_entity_worker(monkeypatch, "_workspace_job_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(w.settings_public, "module_is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        w.documents, "list_ready_version_refs", AsyncMock(return_value=(refs, "next"))
    )
    monkeypatch.setattr(w.workspaces, "read_access_fence", AsyncMock(return_value="f"))
    monkeypatch.setattr(w.sources, "lock_source", AsyncMock(return_value=source))
    monkeypatch.setattr(w.documents, "get_ready_version_ref", AsyncMock(return_value=current))
    work = SimpleNamespace(
        status="pending",
        lease_expires_at=None,
        error_code=None,
        next_attempt_at=None,
        lease_owner="x",
        dependency_fingerprint="y",
    )

    async def schedule(*_a: Any, **_k: Any) -> Any:
        log.append(("schedule", None))
        return work

    monkeypatch.setattr(w.entities, "schedule_extraction_work", schedule)
    monkeypatch.setattr(w.entities, "list_blocked_extraction_work", AsyncMock(return_value=blocked))
    monkeypatch.setattr(w.entities, "terminalize_exhausted_extraction_work", AsyncMock())
    monkeypatch.setattr(
        w.entities, "list_recoverable_extraction_work", AsyncMock(return_value=work_ids)
    )
    monkeypatch.setattr(w.entities, "defer_blocked_extraction_recheck", AsyncMock())
    monkeypatch.setattr(w.entities, "requeue_blocked_extraction_work", AsyncMock(return_value=True))
    import modules.connectors.public as connectors

    monkeypatch.setattr(
        connectors, "map_github_version", AsyncMock(side_effect=RuntimeError("boom"))
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"cur")
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    redis.enqueue_job = AsyncMock()
    ctx = {
        "session_factory": factory,
        "settings": SimpleNamespace(multi_workspace_enabled=False),
        "redis": redis,
    }
    return SimpleNamespace(ctx=ctx, redis=redis, work=work, log=log, session=factory.session)


async def test_recovery_local_only_ref_blocks_work_and_survives_mapper_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-only ready ref is parked, a failing GitHub mapper is isolated, the cursor advances and ids enqueue once."""
    wid = uuid4()
    ref = _ready()
    s = _recovery_setup(
        monkeypatch,
        refs=[ref],
        current=_ready(local_only=True),
        source=_source(),
        blocked=[],
        work_ids=[wid, wid],
    )
    assert await w.recover_entity_extraction_work(s.ctx) == 1
    assert (s.work.status, s.work.error_code, s.work.lease_owner) == (
        "blocked",
        "local_only_source",
        None,
    )
    assert s.work.dependency_fingerprint is None
    s.redis.set.assert_awaited_once_with(f"bbd:entity-extraction:ready-cursor:{WS}", "next")
    assert s.redis.enqueue_job.await_args.args == ("process_entity_extraction_work", str(wid))
    # claim page + ref + blocked listing sessions each commit exactly once.
    assert s.session.commit.await_count == 3


async def test_recovery_requeues_blocked_work_when_dependency_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy-blocked item whose fingerprint changed and now passes policy is requeued and enqueued."""
    blocked_id = uuid4()
    s = _recovery_setup(
        monkeypatch,
        refs=[],
        current=_ready(),
        source=_source(),
        blocked=[(blocked_id, VER, 1, "ai_policy_denied", "old")],
        work_ids=[],
    )
    config = SimpleNamespace(
        defaults=SimpleNamespace(resolve=lambda _r: SimpleNamespace(model="m")),
        endpoint_destination_id="d",
    )
    monkeypatch.setattr(
        w.settings_public, "get_ai_execution_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(w.settings_public, "derive_for_role", lambda cfg, _r: cfg)
    patch_entity_worker(monkeypatch, "_dependency_snapshot", AsyncMock(return_value=("new", True)))
    patch_entity_worker(monkeypatch, "_policy_allows_extraction", lambda *_a: True)
    s.redis.get = AsyncMock(return_value=None)
    assert await w.recover_entity_extraction_work(s.ctx) == 1
    w.entities.requeue_blocked_extraction_work.assert_awaited_once()
    assert w.entities.requeue_blocked_extraction_work.await_args.args[1:4] == (
        blocked_id,
        "old",
        "new",
    )
    w.entities.defer_blocked_extraction_recheck.assert_not_awaited()
    s.redis.set.assert_awaited_once_with(f"bbd:entity-extraction:ready-cursor:{WS}", "next")
    assert s.redis.enqueue_job.await_args.args == (
        "process_entity_extraction_work",
        str(blocked_id),
    )


async def test_recovery_ignores_non_dependency_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only policy/capability blocks that carry a fingerprint are candidates for dependency requeue."""
    s = _recovery_setup(
        monkeypatch,
        refs=[],
        current=_ready(),
        source=_source(),
        blocked=[
            (uuid4(), VER, 1, "local_only_source", "fp"),
            (uuid4(), VER, 1, "ai_policy_denied", None),
        ],
        work_ids=[],
    )
    assert await w.recover_entity_extraction_work(s.ctx) == 0
    w.entities.requeue_blocked_extraction_work.assert_not_awaited()
    w.entities.defer_blocked_extraction_recheck.assert_not_awaited()
