"""Characterization of the dispatch loop's claim / enqueue / recovery semantics and the claim validations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import dispatcher

NOW = datetime.now(UTC)
SCOPE = InternalJobScope(
    workspace_id=uuid4(), actor_user_id=7, membership_revision=3, source_id=uuid4(), source_generation=1,
)


class Redis:
    """Records enqueue calls; ``result`` is the returned job (``None`` means de-duplicated)."""

    def __init__(self, result: Any = "job", error: BaseException | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result, self.error = result, error

    async def enqueue_job(self, name: str, ident: str, **kw: Any) -> Any:
        """Record the enqueue and optionally fail."""
        self.calls.append((name, ident, kw))
        if self.error:
            raise self.error
        return self.result


def ctx(redis: Redis) -> dict[str, Any]:
    """Build the invocation context with a no-op session factory."""
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace()

    return {"session_factory": factory, "redis": redis, "settings": SimpleNamespace(multi_workspace_enabled=False)}


def patch_loop(monkeypatch, identities, claim_script):
    """Replace page reservation, claim and recovery with scripted spies; returns the recorded recoveries."""
    returned: list[Any] = []

    async def reserve(c, factory):
        return identities, NOW

    async def claim(factory, rec, identifier, status, dispatched, *, now, enabled):
        outcome = claim_script.pop(0)
        if isinstance(outcome, BaseException):
            if getattr(outcome, "after_claim", False):
                rec.claimed_at = NOW
                rec.job_name = "process_ingestion_event"
                rec.scope = SCOPE
            raise outcome
        if outcome:
            rec.claimed_at = NOW
            rec.job_name = "process_ingestion_event"
            rec.scope = SCOPE
        return outcome

    async def back(factory, identifier, rec, *, enabled):
        returned.append((identifier, rec.claimed_at))

    monkeypatch.setattr(dispatcher, "_reserve_dispatch_page", reserve)
    monkeypatch.setattr(dispatcher, "_claim_for_dispatch", claim)
    monkeypatch.setattr(dispatcher, "_return_claim_to_pending", back)
    return returned


def ident() -> tuple[Any, ...]:
    """One scanned identity row."""
    return (uuid4(), "pending", None, NOW)


@pytest.mark.asyncio
async def test_enqueue_counts_only_new_jobs_with_the_claim_in_the_job_id(monkeypatch):
    """The job id carries the committed claim timestamp; a de-duplicated job is not counted."""
    rows = [ident(), ident()]
    patch_loop(monkeypatch, rows, [True, True])
    redis = Redis(result=None)
    assert await dispatcher.dispatch_pending_work(ctx(redis)) == 0
    redis.result = "job"
    patch_loop(monkeypatch, rows, [True, False])
    redis2 = Redis()
    assert await dispatcher.dispatch_pending_work(ctx(redis2)) == 1
    (name, identifier, kw) = redis2.calls[0]
    assert name == "process_ingestion_event" and identifier == str(rows[0][0])
    assert kw["_job_id"] == f"ingestion:{rows[0][0]}:{NOW.isoformat()}" and kw["_defer_until"] == NOW


@pytest.mark.asyncio
async def test_lost_authorization_skips_the_identity_and_continues(monkeypatch):
    """An ``HTTPException`` from admission is swallowed per identity; later identities still run."""
    rows = [ident(), ident()]
    patch_loop(monkeypatch, rows, [HTTPException(status_code=409, detail="x"), True])
    redis = Redis()
    assert await dispatcher.dispatch_pending_work(ctx(redis)) == 1


@pytest.mark.asyncio
async def test_error_before_the_claim_is_raised_but_after_it_the_attempt_returns_to_pending(monkeypatch):
    """A failure with no claim propagates; once claimed (even a failed commit) the exact attempt is released."""
    rows = [ident()]
    patch_loop(monkeypatch, rows, [RuntimeError("db")])
    with pytest.raises(RuntimeError):
        await dispatcher.dispatch_pending_work(ctx(Redis()))

    failure = RuntimeError("commit lost")
    failure.after_claim = True  # type: ignore[attr-defined]
    rows = [ident(), ident()]
    returned = patch_loop(monkeypatch, rows, [failure, True])
    assert await dispatcher.dispatch_pending_work(ctx(Redis())) == 1
    assert returned == [(rows[0][0], NOW)]

    rows = [ident()]
    returned = patch_loop(monkeypatch, rows, [True])
    assert await dispatcher.dispatch_pending_work(ctx(Redis(error=OSError("redis down")))) == 0
    assert returned == [(rows[0][0], NOW)]


@pytest.mark.asyncio
async def test_rollout_flag_and_scan_state_are_validated():
    """A non-boolean rollout flag or missing shared scan state is a TypeError before any query."""
    bad = ctx(Redis())
    bad["settings"] = SimpleNamespace(multi_workspace_enabled="yes")
    with pytest.raises(TypeError, match="rollout flag"):
        await dispatcher.dispatch_pending_work(bad)
    with pytest.raises(TypeError, match="scheduling state"):
        await dispatcher.dispatch_pending_work(ctx(Redis()))


class Session:
    """Session answering the claim transaction's lookups."""

    def __init__(self, event: Any, stage: Any = None) -> None:
        self.event, self.stage_row = event, stage
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, *_a: Any, **_k: Any) -> Any:
        """Return the locked outbox row."""
        return self.event

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        """Return the stage lookup."""
        return SimpleNamespace(one_or_none=lambda: self.stage_row)

    async def commit(self) -> None:
        """Count commits."""
        self.commits += 1

    async def rollback(self) -> None:
        """Count rollbacks."""
        self.rollbacks += 1


def claim_env(monkeypatch, session: Session, *, scope: Any = SCOPE, valid: bool = True):
    """Wire the claim helper's collaborators to scripted values; returns the quarantine log."""
    quarantined: list[Any] = []

    @asynccontextmanager
    async def factory():
        yield session

    async def resolve(sess, identifier, **kw):
        return scope

    async def fence(*_a, **_k):
        return "fence"

    async def quarantine(sess, identifier, status, dispatched):
        quarantined.append(identifier)

    monkeypatch.setattr(dispatcher.public, "resolve_ingestion_event_scope", resolve)
    monkeypatch.setattr(dispatcher, "read_access_fence", fence)
    monkeypatch.setattr(dispatcher, "_quarantine_event_identity", quarantine)
    monkeypatch.setattr(dispatcher, "valid_event_envelope", lambda event, scope: valid)
    return factory, quarantined


def outbox(payload: dict[str, Any] | None = None, event_type: str = "ingestion.stage.requested") -> Any:
    """Build an outbox row stand-in."""
    return SimpleNamespace(
        type=event_type, status="pending", dispatched_at=None,
        payload=payload if payload is not None else {"run_id": str(uuid4()), "stage_id": str(uuid4())},
    )


@pytest.mark.asyncio
async def test_claim_commits_the_queued_timestamp_before_redis(monkeypatch):
    """A valid event is stamped ``queued`` with the claim timestamp and committed once."""
    event = outbox()
    session = Session(event, stage=SimpleNamespace(status="pending", lease_expires_at=None))
    factory, _ = claim_env(monkeypatch, session)
    record = dispatcher._DispatchClaim()
    assert await dispatcher._claim_for_dispatch(factory, record, uuid4(), "pending", None, now=NOW, enabled=False)
    assert event.status == "queued" and event.dispatched_at == record.claimed_at and session.commits == 1
    assert record.job_name == "process_ingestion_event" and record.scope is SCOPE and record.access_fence == "fence"


@pytest.mark.asyncio
async def test_claim_skips_and_fails_per_rule(monkeypatch):
    """Quarantine unresolvable identities, skip missing/running events, fail invalid or stage-less ones."""
    factory, quarantined = claim_env(monkeypatch, Session(None), scope=None)
    record = dispatcher._DispatchClaim()
    assert not await dispatcher._claim_for_dispatch(factory, record, uuid4(), "pending", None, now=NOW, enabled=False)
    assert len(quarantined) == 1 and record.claimed_at is None

    factory, _ = claim_env(monkeypatch, Session(None))
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)

    event = outbox()
    session = Session(event)
    factory, _ = claim_env(monkeypatch, session, valid=False)
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)
    assert event.status == "failed" and session.commits == 1

    event = outbox()
    session = Session(event, stage=None)  # the stage row vanished
    factory, _ = claim_env(monkeypatch, session)
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)
    assert event.status == "failed"

    event = outbox()
    live = SimpleNamespace(status="running", lease_expires_at=datetime.now(UTC) + timedelta(minutes=10))
    session = Session(event, stage=live)  # an in-flight worker keeps its original timestamp
    factory, _ = claim_env(monkeypatch, session)
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)
    assert event.status == "pending" and session.commits == 0


@pytest.mark.asyncio
async def test_cleanup_and_purge_events_need_a_matching_retained_identity(monkeypatch):
    """Cleanup/purge events fail when their retained receipt scope no longer matches."""
    from modules.knowledge.documents import public as documents
    from modules.sources import public as sources

    operation = {"operation_id": str(uuid4())}
    event = outbox(dict(operation), "document.cleanup.requested")
    session = Session(event)
    factory, _ = claim_env(monkeypatch, session)

    async def receipt(sess, op, **kw):
        return receipt.value

    receipt.value = None
    monkeypatch.setattr(documents, "read_document_cleanup_job_identity", receipt)
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)
    assert event.status == "failed"
    event = outbox(dict(operation), "document.cleanup.requested")
    session = Session(event)
    factory, _ = claim_env(monkeypatch, session)
    receipt.value = SimpleNamespace(source_id=SCOPE.source_id, source_generation=1)
    assert await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                now=NOW, enabled=False)

    async def retained(sess, op, **kw):
        return retained.value

    retained.value = "different"
    monkeypatch.setattr(sources, "read_source_purge_job_identity", retained)
    event = outbox(dict(operation), "source.purge.requested")
    session = Session(event)
    factory, _ = claim_env(monkeypatch, session)
    assert not await dispatcher._claim_for_dispatch(factory, dispatcher._DispatchClaim(), uuid4(), "pending", None,
                                                    now=NOW, enabled=False)
    assert event.status == "failed"
