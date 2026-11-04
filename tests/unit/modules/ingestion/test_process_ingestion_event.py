"""Characterization of ``process_ingestion_event``: claim phase, transient retry, last-resort failure, completion."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from arq import Retry
from sqlalchemy.exc import OperationalError

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import worker

RUN_ID = uuid4()
STAGE_ID = uuid4()
NOW = datetime.now(UTC)


class Env:
    """Fakes for the worker's collaborators plus a call log."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, stage_status: str = "pending", attempts: int = 0,
                 native: bool = True, lease: Any = "future", observed: int = 1) -> None:
        self.log: list[Any] = []
        self.observed = observed
        self.stage = SimpleNamespace(
            id=STAGE_ID, status=stage_status, attempts=attempts, lease_expires_at=None, error_code=None,
            next_attempt_at=None,
        )
        self.run = SimpleNamespace(id=RUN_ID, status="queued")
        self.event = SimpleNamespace(status="pending", next_attempt_at=None)
        expires = NOW + timedelta(minutes=5) if lease == "future" else NOW - timedelta(seconds=1)
        self.state = SimpleNamespace(lease_run_id=RUN_ID, lease_expires_at=expires)
        self.work = SimpleNamespace(
            scope=InternalJobScope(workspace_id=uuid4(), actor_user_id=7, membership_revision=3), access_fence="fence", source=SimpleNamespace(id=uuid4(), generation=1, status="active"),
            projection=SimpleNamespace(provider="rss"), state=self.state, run=self.run, batch=SimpleNamespace(id=uuid4()),
            stage=self.stage, event=self.event,
        )
        self.claim = SimpleNamespace(run_id=RUN_ID, stage_id=STAGE_ID, event_type="connector.crawl.requested")
        env = self

        async def lock(session, identifier, **kw):
            env.log.append(("lock", kw.get("expected") is not None))
            return env.work

        async def enabled(*_a, **_k):
            return True

        async def commit(session, run, stage, extras=(), **kw):
            env.log.append(("commit", run.status, stage.status, len(extras)))

        async def fail(factory, identifier, run_id, stage_id, code, **kw):
            env.log.append(("fail", code))

        async def delivered(*_a, **_k):
            env.log.append("delivered")

        async def refresh(session, run, **kw):
            run.status = "succeeded"

        async def result(*_a, **_k):
            return True

        async def collect(*_a, **_k):
            env.log.append("collect")
            if env.collect_error:
                raise env.collect_error

        self.collect_error: BaseException | None = None
        monkeypatch.setattr(worker, "_lock_worker_event", lock)
        monkeypatch.setattr(worker, "_ingestion_enabled", enabled)
        monkeypatch.setattr(worker, "_capture_worker_claim", lambda work: env.claim)
        monkeypatch.setattr(worker, "_commit_ingestion_change", commit)
        monkeypatch.setattr(worker, "_fail_ingestion_stage", fail)
        monkeypatch.setattr(worker, "_collect_web_job", collect)
        monkeypatch.setattr(worker, "_refresh_run_status", refresh)
        monkeypatch.setattr(worker.ingestion_api, "mark_event_delivered", delivered)
        monkeypatch.setattr(worker.sources, "record_collection_result_in_uow", result)
        monkeypatch.setattr(worker.connectors, "is_native_provider", lambda provider: native)

    async def run_event(self) -> None:
        """Run the entry point with a session factory whose sessions record rollback/commit."""
        env = self

        class Session:
            async def rollback(self) -> None:
                env.log.append("rollback")

            async def commit(self) -> None:
                env.log.append("session-commit")

            async def scalar(self, *_a: Any, **_k: Any) -> int:
                return env.observed

        @asynccontextmanager
        async def factory():
            yield Session()

        ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False)}
        await worker.process_ingestion_event(ctx, str(uuid4()))


@pytest.mark.asyncio
async def test_crawl_event_claims_runs_and_completes(monkeypatch):
    """Claim commit (running, attempt 1, lease renewed) -> collect -> completion commit and delivery."""
    env = Env(monkeypatch)
    # The completion phase re-locks the same rows: a running, unexpired stage with observations.
    async def lock(session, identifier, **kw):
        env.log.append(("lock", kw.get("expected") is not None))
        if kw.get("expected") is not None:
            env.stage.status = "running"
        return env.work

    monkeypatch.setattr(worker, "_lock_worker_event", lock)
    await env.run_event()
    assert env.log == [
        ("lock", False), ("commit", "running", "running", 0), "collect", ("lock", True),
        "delivered", ("commit", "succeeded", "succeeded", 1),
    ]
    assert env.stage.attempts == 1 and env.stage.error_code is None and env.stage.lease_expires_at is None
    assert env.state.lease_run_id is None  # terminal native run releases the source lease


@pytest.mark.asyncio
async def test_missing_observations_fail_the_stage_after_rollback(monkeypatch):
    """A finished stage with no observation rows is failed as ``missing_observations`` (SQL released first)."""
    env = Env(monkeypatch, observed=0)

    async def lock(session, identifier, **kw):
        env.log.append(("lock", kw.get("expected") is not None))
        if kw.get("expected") is not None:
            env.stage.status = "running"
            env.stage.lease_expires_at = NOW + timedelta(minutes=1)
        return env.work

    monkeypatch.setattr(worker, "_lock_worker_event", lock)
    await env.run_event()
    assert env.log[-2:] == ["rollback", ("fail", "missing_observations")]


@pytest.mark.asyncio
async def test_claim_phase_early_exits(monkeypatch):
    """Succeeded stage: deliver and commit; failed/leased stage: silent; exhausted budget and lost lease: fail."""
    env = Env(monkeypatch, stage_status="succeeded")
    await env.run_event()
    assert env.log == [("lock", False), "delivered", "session-commit"]

    env = Env(monkeypatch, stage_status="failed")
    await env.run_event()
    assert env.log == [("lock", False)]

    env = Env(monkeypatch, stage_status="running")
    env.stage.lease_expires_at = NOW + timedelta(minutes=1)
    await env.run_event()
    assert env.log == [("lock", False)]

    env = Env(monkeypatch, attempts=worker.MAX_STAGE_ATTEMPTS)
    await env.run_event()
    assert env.log == [("lock", False), "rollback", ("fail", "retry_exhausted")]

    for state in (None, SimpleNamespace(lease_run_id=uuid4(), lease_expires_at=NOW + timedelta(minutes=1)),
                  SimpleNamespace(lease_run_id=RUN_ID, lease_expires_at=None)):
        env = Env(monkeypatch)
        env.work.state = state
        await env.run_event()
        assert env.log == [("lock", False), "rollback", ("fail", "lease_expired")]
    env = Env(monkeypatch, lease="past")
    await env.run_event()
    assert env.log[-1] == ("fail", "lease_expired")


@pytest.mark.asyncio
async def test_transient_failure_schedules_a_retry_and_defers_the_job(monkeypatch):
    """OS/timeout/DB errors leave the stage retrying with a jittered delay and raise ``Retry``."""
    env = Env(monkeypatch)
    env.collect_error = OperationalError("stmt", {}, Exception("db"))
    with pytest.raises(Retry) as exc:
        await env.run_event()
    assert env.stage.status == "retrying" and env.stage.error_code == "transient_failure"
    assert env.run.status == "queued" and env.event.status == "pending" and env.stage.lease_expires_at is None
    assert env.event.next_attempt_at == env.stage.next_attempt_at
    assert 0.5 * 1000 <= exc.value.defer_score <= 60 * 1000
    assert env.log[-1][0] == "commit" and env.log[-1][1:] == ("queued", "retrying", 0)


@pytest.mark.asyncio
async def test_connector_retry_after_is_honoured(monkeypatch):
    """A provider ``Retry-After`` becomes the defer delay (floored at half a second)."""
    env = Env(monkeypatch)
    env.collect_error = worker.ConnectorRetryError("slow down", retry_after=12.0)
    with pytest.raises(Retry) as exc:
        await env.run_event()
    assert exc.value.defer_score == 12_000


@pytest.mark.asyncio
async def test_retry_budget_spent_during_failure_settles_instead_of_retrying(monkeypatch):
    """If the re-locked stage already used every attempt, the stage fails as ``retry_exhausted`` (no Retry)."""
    env = Env(monkeypatch)
    env.collect_error = TimeoutError()

    async def lock(session, identifier, **kw):
        env.log.append(("lock", kw.get("expected") is not None))
        if kw.get("expected") is not None:
            env.stage.attempts = worker.MAX_STAGE_ATTEMPTS
        return env.work

    monkeypatch.setattr(worker, "_lock_worker_event", lock)
    await env.run_event()
    assert env.log[-2:] == ["rollback", ("fail", "retry_exhausted")]


@pytest.mark.asyncio
async def test_unexpected_error_settles_the_stage_as_failed(monkeypatch):
    """Any other exception is the last-resort ``stage_failed`` path and is not re-raised."""
    env = Env(monkeypatch)
    env.collect_error = KeyError("boom")
    await env.run_event()
    assert env.log[-1] == ("fail", "stage_failed")


@pytest.mark.asyncio
async def test_lost_claim_stops_silently_at_each_phase(monkeypatch):
    """A claim that no longer locks yields no retry and no completion."""
    env = Env(monkeypatch)
    env.collect_error = OSError("net")
    calls = {"n": 0}

    async def lock(session, identifier, **kw):
        calls["n"] += 1
        return env.work if kw.get("expected") is None else None

    monkeypatch.setattr(worker, "_lock_worker_event", lock)
    await env.run_event()  # retry phase: work None -> returns without raising Retry
    env.collect_error = None
    await env.run_event()  # completion phase: work None -> returns
    assert ("fail", "stage_failed") not in env.log
