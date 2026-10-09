"""Scheduler retry, coalescing, uniqueness and action-required policy (mocked, no database)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors import scheduler
from modules.connectors.models import ConnectorCollectionRequest

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class FakeSession:
    """Returns the queued scalars in call order; swallows execute and flush."""

    def __init__(self, *scalars):
        self.scalars = list(scalars)

    async def scalar(self, *_a, **_k):
        return self.scalars.pop(0)

    async def get(self, *_a, **_k):
        return self.scalars.pop(0)

    async def execute(self, *_a, **_k):
        return None

    async def flush(self):
        return None


def running(token, attempt=1, deadline=None):
    return SimpleNamespace(
        status="running", active_admission_token=token, attempt=attempt, provider_deadline=deadline,
        error_code=None, source_id=uuid4(), connector_revision=3, credential_revision=7, terms_revision=2,
        available_at=NOW, enqueue_next_at=NOW)


def schedule():
    return SimpleNamespace(
        failure_count=0, next_eligible_at=None, blocked_error_code=None, blocked_dimensions=None,
        blocked_connector_revision=None, blocked_credential_revision=None, blocked_terms_revision=None,
        workspace_id=uuid4())


def test_long_retry_after_is_honoured_not_capped():
    deadline = NOW + timedelta(hours=6)
    assert scheduler.retry_at(NOW, 1, deadline) == deadline
    assert scheduler.retry_at(NOW, 1, None) == NOW + timedelta(seconds=30)
    assert scheduler.retry_at(NOW, 20, None) == NOW + timedelta(seconds=900)  # local backoff is capped


@pytest.mark.asyncio
async def test_retryable_failure_requeues_at_the_provider_deadline():
    token, deadline = uuid4(), datetime.now(UTC) + timedelta(hours=6)
    request = running(token)
    assert await scheduler.settle_admission_in_uow(
        FakeSession(request, schedule()), uuid4(), token, outcome="failed", error_code="rate_limited",
        retryable=True, provider_deadline=deadline)
    assert request.status == "queued" and request.available_at == deadline


@pytest.mark.asyncio
async def test_max_attempts_fails_and_penalizes_the_schedule():
    token, sched = uuid4(), schedule()
    request = running(token, attempt=scheduler.MAX_ATTEMPTS)
    await scheduler.settle_admission_in_uow(
        FakeSession(request, sched), uuid4(), token, outcome="failed", error_code="timeout", retryable=True)
    assert request.status == "failed" and request.error_code == "timeout"
    assert sched.failure_count == 1


@pytest.mark.parametrize("code,dimension", [
    ("terms_not_accepted", "terms"), ("invalid_credential", "credential"),
    ("credential_missing", "credential"), ("schema_changed", "config")])
@pytest.mark.asyncio
async def test_terms_auth_schema_failures_are_not_retried_and_block_the_schedule(code, dimension):
    token, sched = uuid4(), schedule()
    request = running(token, attempt=1)
    await scheduler.settle_admission_in_uow(
        FakeSession(request, sched), uuid4(), token, outcome="failed", error_code=code, retryable=True)
    assert request.status == "failed"
    assert sched.blocked_error_code == code and sched.blocked_dimensions == [dimension]
    assert sched.blocked_credential_revision == 7 and sched.blocked_connector_revision == 3


def test_late_schedule_coalesces_to_one_catch_up():
    due = NOW - timedelta(hours=10)
    nxt = scheduler.next_due_after(NOW, due, 60)
    assert nxt > NOW and nxt - NOW <= timedelta(minutes=60)  # one catch-up, no backlog of 10 requests
    assert scheduler.next_due_after(NOW, NOW + timedelta(minutes=5), 60) == NOW + timedelta(minutes=65)


def test_one_active_request_per_source_regardless_of_trigger():
    index = next(i for i in ConnectorCollectionRequest.__table__.indexes
                 if i.name == "uq_connector_collection_requests_active")
    assert index.unique and [c.name for c in index.columns] == ["source_id"]  # manual + scheduled share one slot


@pytest.mark.asyncio
async def test_clear_block_needs_newer_credential_revision():
    sched = schedule()
    sched.blocked_error_code, sched.blocked_dimensions = "invalid_credential", ["credential"]
    sched.blocked_credential_revision = 7
    assert not await scheduler.clear_collection_block(FakeSession(sched), uuid4())  # revision absent
    assert not await scheduler.clear_collection_block(FakeSession(sched), uuid4(), credential_revision=7)
    assert not await scheduler.clear_collection_block(
        FakeSession(sched), uuid4(), credential_revision=8, workspace_id=uuid4())  # foreign workspace
    assert await scheduler.clear_collection_block(
        FakeSession(sched), uuid4(), credential_revision=8, workspace_id=sched.workspace_id)
    assert sched.blocked_error_code is None and sched.blocked_credential_revision is None
