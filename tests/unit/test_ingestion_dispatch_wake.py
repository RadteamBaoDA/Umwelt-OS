"""Continuation paths that return an outbox row to ``pending`` must wake the dispatcher on commit."""
from __future__ import annotations

from typing import Any

from core import cron
from modules.ingestion import worker as ingestion_worker


def test_continuation_wake_registers_dispatch_with_capped_delay() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    async def hook(name: str, defer_seconds: float = 1.0) -> None: ...

    session: Any = Session(create_engine("sqlite://"))
    cron.set_wake_hook(hook)
    try:
        ingestion_worker._wake_dispatcher_after_commit(session, 1.0)
        assert session.info["cron_wakes"] == {"dispatch_pending_work": 1.0}
        other: Any = Session(create_engine("sqlite://"))
        ingestion_worker._wake_dispatcher_after_commit(other, 60.0)
        assert other.info["cron_wakes"] == {"dispatch_pending_work": 5.0}  # capped at the wake-debounce window
    finally:
        cron.set_wake_hook(None)


def test_every_pending_return_path_wakes_the_dispatcher() -> None:
    import inspect

    for fn in (ingestion_worker._settle_stage, ingestion_worker._schedule_transient_retry,
               ingestion_worker._settle_failure, ingestion_worker._defer_until_observations_enabled):
        assert "_wake_dispatcher_after_commit(" in inspect.getsource(fn), fn.__name__
