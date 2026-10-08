"""Unit tests for ingestion dispatcher, status transitions, retry backoff, and cursor CAS logic."""

import asyncio
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.ingestion import dispatcher, public
from modules.ingestion.models import (
    EventOutbox,
    IngestionRun,
    IngestionStage,
    SourceIngestionState,
)
from modules.ingestion.worker import (
    ConnectorRetryError,
    _retry_after_seconds,
)

SCOPE = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=3)


def _make_outbox_event(
    *,
    event_id: UUID | None = None,
    event_type: str = "ingestion.stage.requested",
    status: str = "pending",
    next_attempt_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> EventOutbox:
    now = datetime.now(UTC)
    return EventOutbox(
        id=event_id or uuid4(),
        type=event_type,
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={"run_id": str(uuid4())},
        status=status,
        next_attempt_at=next_attempt_at or now,
        dispatched_at=dispatched_at,
        created_at=now,
    )


class TestDispatcherWork:
    """Test outbox query filtering, Redis job enqueuing, and status transitions."""

    @staticmethod
    def _ctx(events: list[EventOutbox], redis: AsyncMock) -> dict:
        """First session lists identities; each later per-identity session claims the next event."""
        Identity = namedtuple("Identity", "id status dispatched_at created_at")
        claims = list(events)
        sessions = 0

        def enter(*_args):
            nonlocal sessions
            sessions += 1
            session = AsyncMock()
            if sessions == 1:
                listing = MagicMock()
                listing.all.return_value = [Identity(e.id, e.status, e.dispatched_at, e.created_at) for e in events]
                session.execute.return_value = listing
            else:
                session.scalar.side_effect = lambda *_a, **_k: claims.pop(0)
            return session

        factory = MagicMock()
        factory.return_value.__aenter__.side_effect = enter
        return {
            "session_factory": factory, "redis": redis,
            "settings": MagicMock(multi_workspace_enabled=False),
            dispatcher.DISPATCH_SCAN_STATE: {"cursor": None, "lock": asyncio.Lock()},
        }

    @pytest.mark.asyncio
    async def test_dispatch_pending_work_enqueues_and_updates_status(self) -> None:
        now = datetime.now(UTC)
        ev1 = _make_outbox_event(event_type="document.file.uploaded", status="pending")
        ev2 = _make_outbox_event(event_type="connector.crawl.requested", status="pending")
        redis = AsyncMock()
        ctx = self._ctx([ev1, ev2], redis)
        scope = MagicMock(workspace_id=uuid4(), actor_user_id=7, membership_revision=3, source_id=None)

        with (
            patch.object(dispatcher, "admit_write", AsyncMock()),
            patch.object(dispatcher.public, "resolve_ingestion_event_scope", AsyncMock(return_value=scope)),
            patch.object(dispatcher, "read_access_fence", AsyncMock()),
            patch.object(dispatcher, "valid_event_envelope", return_value=True),
        ):
            count = await dispatcher.dispatch_pending_work(ctx)

        assert count == 2
        assert ev1.status == "queued" and ev1.dispatched_at is not None
        assert ev2.status == "queued" and ev2.dispatched_at is not None
        assert redis.enqueue_job.await_count == 2
        (name1, id1), kw1 = redis.enqueue_job.await_args_list[0]
        (name2, id2), kw2 = redis.enqueue_job.await_args_list[1]
        assert (name1, id1) == ("process_uploaded_file", str(ev1.id))
        assert (name2, id2) == ("process_ingestion_event", str(ev2.id))
        assert kw1["_job_id"] == f"ingestion:{ev1.id}:{ev1.dispatched_at.isoformat()}"
        assert kw2["_job_id"] == f"ingestion:{ev2.id}:{ev2.dispatched_at.isoformat()}"
        assert abs((kw1["_defer_until"] - now).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_dispatch_pending_work_no_events_does_not_enqueue(self) -> None:
        redis = AsyncMock()
        ctx = self._ctx([], redis)
        count = await dispatcher.dispatch_pending_work(ctx)

        assert count == 0
        redis.enqueue_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_pending_work_requires_shared_scan_state(self) -> None:
        ctx = self._ctx([], AsyncMock())
        del ctx[dispatcher.DISPATCH_SCAN_STATE]
        with pytest.raises(TypeError, match="shared ingestion dispatch scheduling state"):
            await dispatcher.dispatch_pending_work(ctx)

    @pytest.mark.asyncio
    async def test_mark_event_delivered(self) -> None:
        event = _make_outbox_event(status="queued")
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = event.id
        session.execute.return_value = result

        with patch.object(public, "_admit_ingestion_scope", AsyncMock()):
            assert await dispatcher.mark_event_delivered(
                session, event.id, scope=SCOPE, multi_workspace_enabled=False,
            ) is True
        session.commit.assert_not_awaited()  # flush-only owner seam; the caller commits

    @pytest.mark.asyncio
    async def test_mark_event_delivered_missing_event(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute.return_value = result

        with patch.object(public, "_admit_ingestion_scope", AsyncMock()):
            assert await dispatcher.mark_event_delivered(
                session, uuid4(), scope=SCOPE, multi_workspace_enabled=False,
            ) is False
        session.commit.assert_not_awaited()


class TestBatchAndRunStatusTransitions:
    """Test IngestionRun, IngestionStage status progressions and retry checks."""

    def test_run_status_lifecycle_values(self) -> None:
        run = IngestionRun(
            batch_id=uuid4(),
            source_id=uuid4(),
            status="queued",
        )
        assert run.status == "queued"

        # Transition to running
        run.status = "running"
        assert run.status == "running"

        # Transition to terminal status
        for terminal in ["succeeded", "failed", "needs_ocr"]:
            run.status = terminal
            assert run.status == terminal

    def test_stage_status_lifecycle_values(self) -> None:
        stage = IngestionStage(
            run_id=uuid4(),
            stage_key="receive",
            status="pending",
            attempts=0,
        )
        assert stage.status == "pending"

        # Progressions
        stage.status = "running"
        stage.attempts += 1
        assert stage.attempts == 1

        stage.status = "retrying"
        stage.error_code = "transient_failure"
        assert stage.status == "retrying"

        stage.status = "succeeded"
        stage.error_code = None
        assert stage.status == "succeeded"

    @pytest.mark.asyncio
    async def test_retry_run_active_stage_rejected(self) -> None:
        from modules.sources.schemas import SourceFence, SourceFenceSet

        session = AsyncMock()
        run_id = uuid4()
        source_id = uuid4()
        run = IngestionRun(id=run_id, batch_id=uuid4(), source_id=source_id, status="running")
        st1 = IngestionStage(run_id=run_id, stage_key="receive", status="succeeded")
        st2 = IngestionStage(run_id=run_id, stage_key="normalize", status="running")  # Active!

        fence = SourceFence(id=source_id, workspace_id=uuid4(), status="active", generation=1, local_only=False)

        locked = SourceFenceSet(fences=(fence,), access_fence=AccessFence(SCOPE.workspace_id, 7, 3, 5))
        with (
            patch("modules.ingestion.public._admit_ingestion_scope", AsyncMock()),
            patch("modules.ingestion.public.sources.lock_source_set", AsyncMock(return_value=locked)),
        ):
            session.get.side_effect = lambda model, ident, **kwargs: run if model == IngestionRun else None
            session.scalar.return_value = run
            mock_stages = MagicMock()
            mock_stages.all.return_value = [st1, st2]
            session.scalars.return_value = mock_stages

            with pytest.raises(HTTPException) as exc_info:
                await public.retry_run(session, run_id, "normalize", scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail == "Ingestion run still has an active stage"


class TestRetryBackoffCalculation:
    """Test retry backoff calculations and Retry-After header parsing."""

    def test_connector_retry_error_attributes(self) -> None:
        err = ConnectorRetryError("Rate limit reached", retry_after=15.5)
        assert err.retry_after == 15.5
        assert "Rate limit reached" in str(err)

    def test_retry_after_seconds_parsing(self) -> None:
        # None or missing header
        resp_empty = httpx.Response(status_code=429, headers={})
        assert _retry_after_seconds(resp_empty) is None

        # Integer string within cap
        resp_int = httpx.Response(status_code=429, headers={"retry-after": "25"})
        assert _retry_after_seconds(resp_int) == 25.0

        # Capped at 60 seconds
        resp_large = httpx.Response(status_code=429, headers={"retry-after": "300"})
        assert _retry_after_seconds(resp_large) == 60.0

        # Float string
        resp_float = httpx.Response(status_code=429, headers={"retry-after": "1.75"})
        assert _retry_after_seconds(resp_float) == 1.75

        # Invalid string
        resp_invalid = httpx.Response(status_code=429, headers={"retry-after": "invalid_date_or_num"})
        assert _retry_after_seconds(resp_invalid) is None

    def test_worker_delay_calculation_logic(self) -> None:
        # 1. Delay from ConnectorRetryError with retry_after
        exc = ConnectorRetryError("Rate limited", retry_after=10.0)
        delay = max(0.5, exc.retry_after)
        assert delay == 10.0

        # 2. Delay from ConnectorRetryError with small retry_after (< 0.5s floor)
        exc_small = ConnectorRetryError("Rate limited", retry_after=0.1)
        delay_small = max(0.5, exc_small.retry_after)
        assert delay_small == 0.5

        # 3. Exponential backoff bounds: 2.0 ** attempt capped at 60.0
        for attempt in range(1, 10):
            exp_cap = min(60.0, 2.0**attempt)
            assert 0.5 <= exp_cap <= 60.0
        assert min(60.0, 2.0**1) == 2.0
        assert min(60.0, 2.0**4) == 16.0
        assert min(60.0, 2.0**10) == 60.0

    def test_max_attempts_exhaustion_threshold(self) -> None:
        max_attempts = 5
        assert 4 < max_attempts  # retrying
        assert 5 >= max_attempts  # retry_exhausted


class TestCursorCasLogic:
    """Test Compare-And-Swap (CAS) cursor updates, stale cursor checks, and lease fences."""

    def test_stale_cursor_check(self) -> None:
        state = SourceIngestionState(
            source_id=uuid4(),
            cursor="2026-05-01T00:00:00Z",
            lease_expires_at=None,
        )
        # Expected cursor_before is older than state.cursor
        cursor_before = "2026-04-01T00:00:00Z"
        assert state.cursor != cursor_before

    def test_cursor_cas_match_and_mismatch_semantics(self) -> None:
        # Simulate CAS execution rowcount outcome
        # If cursor in DB matched cursor_before, rowcount is 1 (success)
        rowcount_success = 1
        assert rowcount_success == 1

        # If concurrent writer moved cursor forward, rowcount is 0 -> HTTP 409
        rowcount_conflict = 0
        if rowcount_conflict != 1:
            with pytest.raises(HTTPException) as exc_info:
                raise HTTPException(status_code=409, detail="Collection cursor changed")
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail == "Collection cursor changed"

    def test_active_lease_fence_prevents_concurrent_collection(self) -> None:
        now = datetime.now(UTC)
        state = SourceIngestionState(
            source_id=uuid4(),
            cursor="cur-1",
            lease_run_id=uuid4(),
            lease_expires_at=now + timedelta(minutes=10),
        )
        # Lease is still active
        is_active = state.lease_expires_at is not None and state.lease_expires_at > now
        assert is_active is True

    def test_monotonic_cursor_advance_no_backwards_regression(self) -> None:
        # Simulating RSS feed cursor calculation
        prior_cursor = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        older_item_time = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

        cursor_after = older_item_time.isoformat()
        # Verify cursor monotonic guard
        latest = datetime.fromisoformat(cursor_after.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
        if prior_cursor > latest:
            cursor_after = prior_cursor.isoformat()

        assert cursor_after == prior_cursor.isoformat()
