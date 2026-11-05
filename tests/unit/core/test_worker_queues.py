"""Bare enqueues from non-default workers must reach the default queue; explicit queues stay explicit."""
from __future__ import annotations

import inspect
from typing import Any

from arq.connections import ArqRedis
from arq.constants import default_queue_name

from apps.worker import main as worker_main
from core.cron import CRON_QUEUE
from core.worker_queues import MODEL_QUEUE, pin_default_queue


class _Pipe:
    """Minimal arq pipeline stand-in recording the queue each job is zadd-ed to."""

    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def watch(self, *_: Any) -> None: ...
    async def exists(self, *_: Any) -> int:
        return 0

    def multi(self) -> None: ...
    def psetex(self, *_: Any) -> None: ...
    def zadd(self, queue: str, *_: Any) -> None:
        self.sink.append(queue)

    async def execute(self) -> None: ...


def _cron_pool(sink: list[str]) -> ArqRedis:
    """A pool shaped like the one arq gives the cron worker: default queue == its own queue."""
    pool = ArqRedis(default_queue_name=CRON_QUEUE)  # type: ignore[call-arg]
    pool.pipeline = lambda **_: _Pipe(sink)  # type: ignore[method-assign,assignment]
    return pool


async def test_bare_enqueue_from_cron_pool_lands_on_default_queue() -> None:
    """Before the fix this landed on arq:cron where no worker registers process_*."""
    sink: list[str] = []
    pool = _cron_pool(sink)
    pin_default_queue(pool)
    await pool.enqueue_job("process_document_ready", "doc")
    await pool.enqueue_job("translate_content", _queue_name=MODEL_QUEUE)
    await pool.enqueue_job("run_cron_job", _queue_name=CRON_QUEUE)
    assert sink == [default_queue_name, MODEL_QUEUE, CRON_QUEUE]


async def test_unpinned_cron_pool_misroutes() -> None:
    """Documents the arq behaviour the pin works around."""
    sink: list[str] = []
    await _cron_pool(sink).enqueue_job("process_document_ready", "doc")
    assert sink == [CRON_QUEUE]


def test_every_non_default_worker_pins_its_pool() -> None:
    """cron/chat/model startups all pin (cron via _boot_runtime)."""
    for hook in (worker_main._boot_runtime, worker_main.chat_startup, worker_main.model_startup):
        assert "pin_default_queue(" in inspect.getsource(hook)
