"""Retry backoff with jitter and the dead-letter recorder/replay."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from arq.worker import Retry

from apps.worker import main as worker_main
from core import worker_dlq
from core.worker_retry import backoff_delay, current_try, retry_with_backoff


def test_backoff_grows_is_capped_and_jittered() -> None:
    low = [backoff_delay(n, base=5, cap=60, rand=lambda: 0.0) for n in range(1, 8)]
    high = [backoff_delay(n, base=5, cap=60, rand=lambda: 0.999999) for n in range(1, 8)]
    assert low == [2.5, 5.0, 10.0, 20.0, 30.0, 30.0, 30.0]  # floor is half the ceiling
    assert all(h <= 60 for h in high) and high[0] < 5.0 and high[-1] > 59
    assert backoff_delay(10_000, base=1, cap=7, rand=lambda: 0.5) <= 7
    with pytest.raises(ValueError):
        backoff_delay(1, base=10, cap=5)


def test_retry_with_backoff_reads_job_try() -> None:
    assert current_try({"job_try": 3}) == 3 and current_try({}) == 1 and current_try({"job_try": "x"}) == 1
    retry = retry_with_backoff({"job_try": 1}, base=30, cap=120)
    assert isinstance(retry, Retry) and 15_000 <= retry.defer_score <= 30_000  # type: ignore[operator]


def test_args_are_digested_and_only_ids_are_replayable() -> None:
    ident = str(uuid4())
    assert worker_dlq.replayable_refs([ident], {}) == [ident]
    assert worker_dlq.replayable_refs(["a@b.example"], {}) is None  # PII-shaped
    assert worker_dlq.replayable_refs([ident], {"x": 1}) is None
    assert worker_dlq.replayable_refs(["free text payload"], {}) is None
    digest = worker_dlq.args_digest(["secret payload"], {})
    assert len(digest) == 64 and "secret" not in digest


def test_error_messages_are_scrubbed_and_truncated() -> None:
    message = worker_dlq.scrub_message("fail for jo@example.com at https://x.example/?token=abc " + "A" * 40 + " " + "z" * 900)
    assert "@" not in message and "https" not in message and "A" * 32 not in message
    assert len(message) <= worker_dlq.ERROR_MESSAGE_LIMIT


class _Recorder:
    """Session factory double capturing the upsert and commit."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    def __call__(self) -> _Recorder:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_final_failed_attempt_records_a_row_and_reraises() -> None:
    factory = _Recorder()

    async def failing(ctx: dict[str, Any], event_id: str) -> None:
        raise RuntimeError("bad user@example.com")

    guarded = worker_dlq.dead_letter(failing, max_tries=3, queue="arq:queue")
    ident = str(uuid4())
    ctx = {"session_factory": factory, "job_try": 2, "job_id": "j1"}
    with pytest.raises(RuntimeError):
        await guarded(ctx, ident)
    assert len(factory.statements) == 1  # plain exceptions are never retried by arq: recorded on try 2
    factory.statements.clear()
    factory.commits = 0
    ctx["job_try"] = 3
    with pytest.raises(RuntimeError):
        await guarded(ctx, ident)
    assert len(factory.statements) == 1 and factory.commits == 1
    sql = str(factory.statements[0].compile(compile_kwargs={"literal_binds": False}))
    assert "worker_dead_letters" in sql and "ON CONFLICT" in sql
    params = factory.statements[0].compile().params
    assert params["job_name"] == "failing" and params["attempts"] == 3 and params["args_ref"] == [ident]
    assert "@" not in params["error_message"]


@pytest.mark.asyncio
async def test_plain_exception_on_first_try_is_recorded() -> None:
    factory = _Recorder()

    async def failing(ctx: dict[str, Any]) -> None:
        raise KeyError("boom")

    with pytest.raises(KeyError):
        await worker_dlq.dead_letter(failing, max_tries=5, queue="q")({"session_factory": factory, "job_try": 1})
    assert len(factory.statements) == 1 and factory.statements[0].compile().params["attempts"] == 1


@pytest.mark.asyncio
async def test_retry_and_shutdown_cancel_are_recorded_only_on_the_final_try() -> None:
    factory = _Recorder()

    async def retrying(ctx: dict[str, Any]) -> None:
        raise Retry(defer=1)

    async def cancelled(ctx: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    for job, expected in ((retrying, Retry), (cancelled, asyncio.CancelledError)):
        guarded = worker_dlq.dead_letter(job, max_tries=3, queue="q", timeout_seconds=100)
        with pytest.raises(expected):
            await guarded({"session_factory": factory, "job_try": 1})
        assert not factory.statements  # arq will retry these
        with pytest.raises(expected):
            await guarded({"session_factory": factory, "job_try": 3})
        assert len(factory.statements) == 1
        factory.statements.clear()


@pytest.mark.asyncio
async def test_timeout_is_recorded_on_a_non_final_try() -> None:
    factory = _Recorder()

    async def slow(ctx: dict[str, Any]) -> None:
        await asyncio.sleep(10)

    guarded = worker_dlq.dead_letter(slow, max_tries=5, queue="q", timeout_seconds=0.6)
    with pytest.raises(TimeoutError):  # arq's wait_for cancels the job and fails it without a retry
        await asyncio.wait_for(guarded({"session_factory": factory, "job_try": 1}), 0.2 + 0.5 + 0.3)
    assert len(factory.statements) == 1


@pytest.mark.asyncio
async def test_success_and_recording_failure_do_not_change_outcome() -> None:
    async def ok(ctx: dict[str, Any]) -> str:
        return "fine"

    assert await worker_dlq.dead_letter(ok, max_tries=1, queue="q")({"job_try": 1}) == "fine"

    class Broken(_Recorder):
        async def execute(self, statement: Any) -> None:
            raise OSError("db down")

    async def failing(ctx: dict[str, Any]) -> None:
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        await worker_dlq.dead_letter(failing, max_tries=1, queue="q")({"job_try": 1, "session_factory": Broken()})


class _ReplaySession(_Recorder):
    def __init__(self, rows: list[Any]) -> None:
        super().__init__()
        self.rows = rows

    async def scalars(self, _query: Any) -> list[Any]:
        return self.rows


@pytest.mark.asyncio
async def test_replay_is_dry_run_by_default_and_enqueues_only_replayable_rows() -> None:
    good = SimpleNamespace(id=uuid4(), job_name="process_uploaded_file", queue="arq:queue", args_ref=["a1"],
                           replayable=True, replay_count=0, replayed_at=None)
    bad = SimpleNamespace(id=uuid4(), job_name="x", queue="arq:queue", args_ref=None, replayable=False,
                          replay_count=0, replayed_at=None)
    session = _ReplaySession([good, bad])
    enqueued: list[tuple[Any, ...]] = []

    class Redis:
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            enqueued.append((args, kwargs))

    dry = await worker_dlq.replay_dead_letters(session, Redis())  # type: ignore[arg-type]
    assert dry.replayable == [good] and dry.skipped == [bad] and not enqueued and good.replayed_at is None
    done = await worker_dlq.replay_dead_letters(session, Redis(), execute=True)  # type: ignore[arg-type]
    assert done.executed and enqueued[0][0] == ("process_uploaded_file", "a1")
    assert enqueued[0][1]["_job_id"] == f"dlq:{good.id}:1" and good.replay_count == 1 and good.replayed_at


def test_worker_functions_are_dead_lettered_and_drain_waits_for_jobs() -> None:
    names = [getattr(f, "name", "") for f in worker_main.WorkerSettings.functions]
    assert "process_uploaded_file" in names
    heavy = next(f for f in worker_main.WorkerSettings.functions if getattr(f, "name", "") == "process_uploaded_file")
    assert heavy.max_tries == worker_main.HEAVY_JOB_MAX_TRIES  # type: ignore[attr-defined]
    for settings in (worker_main.WorkerSettings, worker_main.ModelWorkerSettings,
                     worker_main.ChatWorkerSettings, worker_main.CronWorkerSettings):
        assert settings.job_completion_wait > settings.job_timeout  # SIGTERM: finish running jobs first
