"""P14-T3: arq-only chat dispatch, coalesced deltas, per-flush fence, recovery, agent dispatch."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from arq.connections import ArqRedis

from core.config import Settings
from modules.chat import routes, worker
from modules.chat.models import Message, StreamEvent
from modules.chat.schemas import AnswerContext
from tests.unit.test_b1c_regressions import _config, _evidence, _factory

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _request(redis: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


async def test_dispatch_enqueues_on_chat_queue_and_never_creates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("asyncio.create_task must not run chat generation")

    monkeypatch.setattr(asyncio, "create_task", boom)
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    run_id = uuid4()
    await routes._dispatch_response_run(_request(redis), run_id)
    redis.enqueue_job.assert_awaited_once_with(
        "process_chat_response", str(run_id), _job_id=f"chat-response:{run_id}", _queue_name="arq:chat",
    )


async def test_dispatch_enqueue_failure_is_logged_not_raised() -> None:
    redis = SimpleNamespace(enqueue_job=AsyncMock(side_effect=ConnectionError("down")))
    await routes._dispatch_response_run(_request(redis), uuid4())


def test_api_redis_is_arq_client() -> None:
    from apps.api.main import create_app

    app = create_app(Settings(csrf_signing_secret="s"))
    assert isinstance(app.state.redis, ArqRedis) and hasattr(app.state.redis, "enqueue_job")


async def test_agent_dispatch_enqueues_on_arq_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.agents import routes as agent_routes
    from modules.agents.schemas import ProfileRunStart

    run_id = uuid4()
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        redis=redis, settings=object(), tool_registry=object())))
    monkeypatch.setattr(agent_routes.settings_public, "get_ai_execution_config", AsyncMock())
    monkeypatch.setattr(agent_routes.public, "create_profile_run_in_uow",
                        AsyncMock(return_value=SimpleNamespace(id=run_id)))
    session = SimpleNamespace(commit=AsyncMock())
    owner = SimpleNamespace(owner_id=1, token_hash="h")
    payload = ProfileRunStart.model_construct(prompt="hi")
    result = await agent_routes.start_run("researcher", payload, request, session, owner)  # type: ignore[arg-type]
    assert result.id == run_id
    redis.enqueue_job.assert_awaited_once_with(
        "process_agent_run", str(run_id), 1, _job_id=f"agent-run:{run_id}:1",
    )


# ------------------------------------------------------------------ coalescing


class _Gen:
    """Runs run_response_generation over a scripted token stream with a fake session."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tokens: list[str]) -> None:
        self.added: list[Any] = []
        self.fence = AsyncMock(return_value=(True, []))
        self.privacy_cancel = AsyncMock(return_value=0)
        self.failed = AsyncMock()
        self.tokens = tokens
        context = AnswerContext(
            query="q", evidence=[_evidence(content="Umwelt docs.")], has_sufficient_evidence=True,
        )
        scalars = iter([
            SimpleNamespace(content="q?", revision_of_message_id=None, created_at=NOW, role="user"),
            None, SimpleNamespace(expires_at=None),
        ])
        self.session = MagicMock()
        self.session.execute = AsyncMock(return_value=SimpleNamespace(
            fetchone=lambda: (uuid4(), uuid4(), {}, False)))
        self.session.scalar = AsyncMock(side_effect=lambda *a, **k: next(scalars, None))
        self.session.scalars = AsyncMock(return_value=SimpleNamespace(all=list))
        self.session.add = self.added.append
        for name in ("commit", "flush", "rollback", "close", "begin"):
            setattr(self.session, name, AsyncMock())
        seq = iter(range(100, 10_000))
        monkeypatch.setattr(worker, "_lock_live_response", AsyncMock(return_value=(True, MagicMock())))
        monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(return_value=False))
        monkeypatch.setattr(worker, "revalidate_context_fence", self.fence)
        monkeypatch.setattr(worker, "_privacy_cancel_locked", self.privacy_cancel)
        monkeypatch.setattr(worker, "_next_event_seq", AsyncMock(side_effect=lambda *a, **k: next(seq)))
        monkeypatch.setattr(worker, "build_context", AsyncMock(return_value=context))
        monkeypatch.setattr(worker.settings_public, "get_ai_execution_config",
                            AsyncMock(return_value=_config(aliases={})))
        monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=self.stream))
        monkeypatch.setattr(worker, "_mark_failed", self.failed)

    async def stream(self, **_kwargs: Any) -> AsyncIterator[str]:
        for token in self.tokens:
            if token == "__SLEEP__":
                await asyncio.sleep(0.15)
                continue
            yield "data: " + json.dumps({"choices": [{"delta": {"content": token}}]})
        yield "data: [DONE]"

    async def run(self) -> None:
        self.factory = _factory(self.session)
        await worker.run_response_generation(
            uuid4(), self.factory, SimpleNamespace(ai_allowed_endpoint_cidrs=()),  # type: ignore[arg-type]
            MagicMock(),
        )

    @property
    def deltas(self) -> list[str]:
        return [e.data["text"] for e in self.added if isinstance(e, StreamEvent) and e.event_type == "message.delta"]


async def test_200_single_char_tokens_coalesce(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"] * 200)
    await gen.run()
    gen.failed.assert_not_awaited()
    assert len(gen.deltas) <= 2  # ceil(200/256) + 1 is the plan bound; 200 fast tokens fit one 100 ms window
    assert "".join(gen.deltas) == "a" * 200


async def test_size_threshold_flushes_at_256_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "STREAM_FLUSH_SECONDS", 3600.0)
    gen = _Gen(monkeypatch, ["x"] + ["y" * 100] * 6)
    await gen.run()
    # first token flushes at once (last_flush=-inf); 100/200/300 chars flushes at 300; the rest is the final flush
    assert gen.deltas == ["x", "y" * 300, "y" * 300]


async def test_time_threshold_flushes_and_final_flush_publishes_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "STREAM_FLUSH_CHARS", 10_000)
    gen = _Gen(monkeypatch, ["a", "b", "__SLEEP__", "c", "d"])
    await gen.run()
    assert gen.deltas == ["a", "bc", "d"]  # a: first flush; c arrives after 150 ms; d is the final flush
    done = next(e for e in gen.added if isinstance(e, StreamEvent) and e.event_type == "message.done")
    assert done.data["status"] == "completed"
    assert any(isinstance(m, Message) for m in gen.added)


async def test_fence_revalidated_for_every_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "STREAM_FLUSH_SECONDS", 3600.0)
    gen = _Gen(monkeypatch, ["x"] + ["y" * 100] * 6)
    await gen.run()
    locked = [c for c in gen.fence.await_args_list if c.kwargs.get("lock_evidence")]
    assert len(locked) == len(gen.deltas) + 1  # one per flush plus the completion fence


async def test_fence_failure_between_flushes_drops_buffer_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "STREAM_FLUSH_SECONDS", 3600.0)
    gen = _Gen(monkeypatch, ["x"] + ["SECRET" * 20] * 6)
    results = iter([(True, []), (False, ["consent"])])  # flush 1 ok, flush 2 fails
    gen.fence.side_effect = lambda *a, **k: next(results, (False, ["consent"]))
    await gen.run()
    assert gen.deltas == ["x"]
    gen.privacy_cancel.assert_awaited_once()
    assert not any("SECRET" in str(e.data) for e in gen.added if isinstance(e, StreamEvent))
    assert not any(isinstance(m, Message) for m in gen.added)


# -------------------------------------------------------------------- recovery


async def test_recover_requeues_pending_and_fails_abandoned_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    pending_id, stale_id = uuid4(), uuid4()
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[[pending_id], []])
    session.execute = AsyncMock(return_value=SimpleNamespace(
        all=lambda: [(stale_id, {"_chat_privacy_fence": {"rev": 3}})]))
    session.scalar = AsyncMock(return_value=7)
    run = SimpleNamespace(status="streaming", conversation_id=uuid4())
    session.scalar = AsyncMock(side_effect=[7, run, object(), run, 7])
    session.add = MagicMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    monkeypatch.setattr(worker, "_require_privacy_fence", AsyncMock())
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    factory = _factory(session)
    result = await worker.recover_chat_runs({"session_factory": factory, "redis": redis})
    assert result == {"requeued": 1, "failed": 1}
    redis.enqueue_job.assert_awaited_once_with(
        "process_chat_response", str(pending_id), _job_id=f"chat-response:{pending_id}", _queue_name="arq:chat",
    )
    assert run.status == "failed" and run.error_code == "TimeoutError"
    event = session.add.call_args.args[0]
    assert isinstance(event, StreamEvent) and event.response_id == stale_id and event.seq == 8
    assert event.event_type == "status" and event.data["status"] == "failed"
    session.commit.assert_awaited_once()


def _release_session(run: Any, published: Any, order: list[str], seq: int = 4) -> MagicMock:
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[run, object(), run, published, seq])
    session.execute = AsyncMock(side_effect=lambda *a, **k: order.append("delete"))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


async def test_shutdown_before_any_delta_returns_run_to_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    run = SimpleNamespace(status="streaming", conversation_id=uuid4())
    session = _release_session(run, None, order)
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock(side_effect=lambda *_: order.append("privacy")))
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_mark_failed", failed)
    await worker._release_on_shutdown(uuid4(), _factory(session), None)
    assert run.status == "pending" and order == ["privacy", "delete"]
    sql = [str(c.args[0]) for c in session.scalar.await_args_list[:3]]
    assert "FOR UPDATE" not in sql[0]
    assert "chat_conversations" in sql[1] and "FOR UPDATE" in sql[1]
    assert "chat_response_runs" in sql[2] and "FOR UPDATE" in sql[2]
    session.commit.assert_awaited_once()
    failed.assert_not_awaited()


async def test_cancel_near_job_timeout_fails_instead_of_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(status="streaming", conversation_id=uuid4())
    session = _release_session(run, None, [])
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_mark_failed", failed)
    await worker._release_on_shutdown(uuid4(), _factory(session), None, True)
    assert run.status == "streaming"
    failed.assert_awaited_once()
    session.commit.assert_not_awaited()


async def test_handler_cancel_at_job_timeout_requests_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])

    async def cancelled(**_k: Any) -> AsyncIterator[str]:
        raise asyncio.CancelledError
        yield ""  # pragma: no cover

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=cancelled))
    monkeypatch.setattr(worker, "CHAT_JOB_TIMEOUT", 0)  # elapsed since claim >= timeout - margin
    release = AsyncMock()
    monkeypatch.setattr(worker, "_release_on_shutdown", release)
    with pytest.raises(asyncio.CancelledError):
        await gen.run()
    assert release.await_args.args[3] is True


async def test_external_cancel_inside_session_call_still_releases_on_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = _Gen(monkeypatch, ["a"])
    blocked = asyncio.Event()

    async def hang(*_a: Any, **_k: Any) -> Any:
        blocked.set()
        await asyncio.sleep(3600)

    gen.session.scalar = AsyncMock(side_effect=hang)
    release = AsyncMock()
    monkeypatch.setattr(worker, "_release_on_shutdown", release)
    task = asyncio.create_task(gen.run())
    await asyncio.wait_for(blocked.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.assert_awaited_once()
    assert release.await_args.args[1] is gen.factory
    gen.failed.assert_not_awaited()


async def test_shutdown_after_partial_deltas_fails_truthfully(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(status="streaming", conversation_id=uuid4())
    session = _release_session(run, uuid4(), [])
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_mark_failed", failed)
    rid = uuid4()
    await worker._release_on_shutdown(rid, _factory(session), {"rev": 1})
    assert run.status == "streaming"
    failed.assert_awaited_once()
    assert failed.await_args.args[0] == rid and failed.await_args.args[2] == 4
    assert failed.await_args.args[3] == {"rev": 1} and isinstance(failed.await_args.args[4], TimeoutError)


async def test_cancelled_generation_releases_run_bounded_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])

    async def cancelled(**_k: Any) -> AsyncIterator[str]:
        raise asyncio.CancelledError
        yield ""  # pragma: no cover

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=cancelled))
    release = AsyncMock()
    monkeypatch.setattr(worker, "_release_on_shutdown", release)
    with pytest.raises(asyncio.CancelledError):
        await gen.run()
    release.assert_awaited_once()
    gen.failed.assert_not_awaited()


async def test_shutdown_release_never_raises_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, ["a"])

    async def cancelled(**_k: Any) -> AsyncIterator[str]:
        raise asyncio.CancelledError
        yield ""  # pragma: no cover

    async def hang(*_a: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=cancelled))
    monkeypatch.setattr(worker, "_release_on_shutdown", hang)
    monkeypatch.setattr(worker, "SHUTDOWN_RELEASE_TIMEOUT", 0.05)
    with pytest.raises(asyncio.CancelledError):  # the timeout is swallowed; cancellation still propagates
        await gen.run()


def test_chat_worker_settings_isolated_queue() -> None:
    from apps.worker.main import ChatWorkerSettings, WorkerSettings

    assert ChatWorkerSettings.queue_name == "arq:chat"
    assert (ChatWorkerSettings.max_jobs, ChatWorkerSettings.job_timeout, ChatWorkerSettings.max_tries) == (15, 600, 1)
    assert [f.name for f in ChatWorkerSettings.functions] == ["process_chat_response"]
    assert ChatWorkerSettings.keep_result == 0
    main_names = [getattr(f, "name", getattr(f, "__name__", "")) for f in WorkerSettings.functions]
    assert "process_chat_response" not in main_names


async def test_legacy_aliases_overlay_accepts_bytes_keys() -> None:
    from modules.settings.models import ALIASES, legacy_aliases

    alias = next(iter(ALIASES))
    value = json.dumps({"model": "m", "destination": "remote"}).encode()
    redis = SimpleNamespace(hgetall=AsyncMock(return_value={alias.encode(): value}))
    mappings = await legacy_aliases(redis, Settings(csrf_signing_secret="s"))  # type: ignore[arg-type]
    assert mappings[alias].model == "m"


async def test_recover_fails_expired_pending_instead_of_requeueing(monkeypatch: pytest.MonkeyPatch) -> None:
    old_id = uuid4()
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[[], [old_id]])
    session.execute = AsyncMock(return_value=SimpleNamespace(all=list))
    run = SimpleNamespace(
        status="pending", conversation_id=uuid4(),
        updated_at=datetime.now(UTC) - worker.RECOVER_PENDING_MAX_AGE - timedelta(seconds=1),
    )
    session.scalar = AsyncMock(side_effect=[run, object(), run, 0])
    session.add = MagicMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    monkeypatch.setattr(worker, "_next_event_seq", AsyncMock(return_value=1))
    redis = SimpleNamespace(enqueue_job=AsyncMock())
    result = await worker.recover_chat_runs({"session_factory": _factory(session), "redis": redis})
    assert result == {"requeued": 0, "failed": 1}
    redis.enqueue_job.assert_not_awaited()
    assert run.status == "failed" and session.add.call_args.args[0].data["status"] == "failed"


async def test_stream_iterator_closed_and_cancel_check_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, [])
    closed: list[bool] = []

    async def stream(**_k: Any) -> AsyncIterator[str]:
        try:
            for _ in range(50):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": ""}}]})
            yield "data: [DONE]"
        finally:
            closed.append(True)

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=stream))
    checks = worker.is_run_cancelled
    await gen.run()
    assert closed == [True]
    # 50 instantaneous lines: the throttle allows no per-line check (only claim/flush checks remain)
    assert checks.await_count <= 3  # type: ignore[attr-defined]


async def test_early_stop_closes_gateway_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, [])
    closed: list[bool] = []

    async def stream(**_k: Any) -> AsyncIterator[str]:
        try:
            started.append(True)
            while True:  # never exhausts: only aclosing can run the finally
                await asyncio.sleep(0.3)
                yield "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]})
        finally:
            closed.append(True)

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=stream))
    started: list[bool] = []
    monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(side_effect=lambda *a, **k: bool(started)))
    monkeypatch.setattr(worker, "_mark_cancelled", AsyncMock())
    await gen.run()
    assert closed == [True]


async def test_cancel_during_claim_commit_releases_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _Gen(monkeypatch, [])
    gen.session.commit = AsyncMock(side_effect=asyncio.CancelledError)
    release = AsyncMock()
    monkeypatch.setattr(worker, "_release_on_shutdown", release)
    with pytest.raises(asyncio.CancelledError):
        await gen.run()
    release.assert_awaited_once()


async def test_recover_pending_age_uses_updated_at() -> None:
    stmts: list[str] = []
    session = MagicMock()

    async def scalars(stmt: Any) -> list[Any]:
        stmts.append(str(stmt))
        return []

    session.scalars = scalars
    session.execute = AsyncMock(return_value=SimpleNamespace(all=list))
    await worker.recover_chat_runs({"session_factory": _factory(session), "redis": SimpleNamespace()})
    # pending (re-enqueue) and expired queries bound the max age by updated_at (refreshed on release-to-pending)
    assert all("chat_response_runs.updated_at" in s for s in stmts[:2])


async def test_fail_expired_pending_skips_run_released_to_pending_before_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # listed as expired, then claimed and released back to pending (fresh updated_at) before the row lock
    run = SimpleNamespace(status="pending", conversation_id=uuid4(), updated_at=datetime.now(UTC))
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[run, object(), run])
    session.add = MagicMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(worker, "lock_export_privacy", AsyncMock())
    await worker._fail_expired_pending(uuid4(), _factory(session))
    assert run.status == "pending"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
