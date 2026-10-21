"""Model-worker queue routing: queue_for, enqueue sites, worker registrations and claim lease length."""

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from apps.worker.main import HEAVY_SLOT_JOBS, ModelWorkerSettings, WorkerSettings
from core.config import Settings
from core.system import health as health_module
from core.worker_queues import MODEL_JOBS, MODEL_QUEUE, queue_for
from tests.unit.test_b1c_regressions import _factory, _recovery_session, _recovery_setup


def _names(functions: Any) -> set[str]:
    return {getattr(f, "name", getattr(f, "__name__", "")) for f in functions}


def test_queue_for_routes_only_model_jobs() -> None:
    for name in MODEL_JOBS:
        assert queue_for(name) == MODEL_QUEUE == "arq:model"
    assert queue_for("process_graph_operation") is None
    assert queue_for("index_pending_chunks") is None


def test_model_worker_owns_exactly_the_model_jobs_and_main_worker_none() -> None:
    assert _names(ModelWorkerSettings.functions) == set(MODEL_JOBS)
    assert not _names(WorkerSettings.functions) & set(MODEL_JOBS)
    assert ModelWorkerSettings.queue_name == MODEL_QUEUE
    assert ModelWorkerSettings.max_jobs == 4
    assert ModelWorkerSettings.job_timeout == Settings().model_worker_job_timeout_seconds == 900
    assert not _names(ModelWorkerSettings.functions) & HEAVY_SLOT_JOBS
    assert health_module.ARQ_MODEL_WORKER_HEALTH_KEY == ModelWorkerSettings.health_check_key


def test_translate_enqueue_in_route_uses_model_queue() -> None:
    from modules.translations import routes

    assert '_queue_name=queue_for("translate_content")' in inspect.getsource(routes)


async def test_translation_recovery_enqueues_on_model_queue() -> None:
    from modules.translations import worker

    tid = uuid4()
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [tid]))
    redis = MagicMock()
    redis.enqueue_job = AsyncMock()
    assert await worker.recover_translation_jobs({"session_factory": _factory(session), "redis": redis}) == 1
    assert redis.enqueue_job.await_args.kwargs["_queue_name"] == MODEL_QUEUE


@pytest.mark.parametrize(("module", "job"), [
    ("modules.timeline.worker", "process_timeline_extraction_work"),
    ("modules.knowledge.entities.worker", "process_entity_extraction_work"),
])
async def test_extraction_recovery_enqueues_on_model_queue(
    monkeypatch: pytest.MonkeyPatch, module: str, job: str,
) -> None:
    import importlib

    worker: Any = importlib.import_module(module)
    ws, _scope = _recovery_setup(monkeypatch, worker)
    work_id = uuid4()
    timeline = module.startswith("modules.timeline")
    public = worker.timeline if timeline else worker.entities
    monkeypatch.setattr(public, "list_blocked_extraction_work", AsyncMock(return_value=[]))
    monkeypatch.setattr(public, "list_recoverable_extraction_work", AsyncMock(return_value=[work_id]))
    monkeypatch.setattr(public, "terminalize_exhausted_extraction_work", AsyncMock(), raising=False)
    monkeypatch.setattr(worker.documents, "list_ready_version_refs", AsyncMock(return_value=([], None)))
    redis = MagicMock()
    for attr in ("get", "set", "delete", "enqueue_job"):
        setattr(redis, attr, AsyncMock(return_value=None))
    ctx = {"session_factory": _factory(_recovery_session(ws)), "redis": redis,
           "settings": MagicMock(multi_workspace_enabled=False)}
    recover = worker.recover_timeline_extraction_work if timeline else worker.recover_entity_extraction_work
    await recover(ctx)
    redis.enqueue_job.assert_awaited_once()
    assert redis.enqueue_job.await_args.args[0] == job
    assert redis.enqueue_job.await_args.kwargs["_queue_name"] == MODEL_QUEUE


async def test_claims_honour_lease_seconds_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.workspaces.schemas import InternalJobScope
    from modules.knowledge.entities import public as entities
    from modules.timeline import public as timeline

    now = datetime(2026, 1, 1, tzinfo=UTC)
    scope = InternalJobScope(workspace_id=uuid4(), actor_user_id=1, membership_revision=1)
    monkeypatch.setattr(entities, "_admit", AsyncMock())
    for module, kwargs in ((entities, {}), (timeline, {})):
        for seconds in (None, 195):
            work = SimpleNamespace(status="pending", lease_expires_at=None, attempt=0, next_attempt_at=now,
                                   lease_owner=None, error_code=None)
            session = MagicMock()
            session.scalar = AsyncMock(return_value=work)
            session.flush = AsyncMock()
            extra = {} if seconds is None else {"lease_seconds": seconds}
            claimed = await module.claim_extraction_work(
                session, uuid4(), "owner", now, scope=scope, multi_workspace_enabled=False, **extra, **kwargs)
            assert claimed is work
            assert (work.lease_expires_at - now).total_seconds() == (110 if seconds is None else seconds)
