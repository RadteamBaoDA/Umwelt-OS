"""Characterization tests pinning ``dispatch_github_webhooks`` phases, session order and outbox/hint state.

The dispatcher was split into per-phase helpers. These tests drive only the public entry point with
scripted sessions (one per ``async with factory()`` block, consumed in order), so they assert the
same observable contract before and after: how many sessions open, which rows are read/locked in
which order, when each commits or rolls back, and the resulting outbox/hint/capacity state.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.errors import ConflictError, ForbiddenError
from modules.connectors import public as connectors
from modules.connectors import worker
from tests.unit.modules.connectors._worker_patch import patch_worker

WORKSPACE_ID = uuid4()
SOURCE_ID = uuid4()
OUTBOX_ID = uuid4()
DELIVERY_ID = uuid4()
GRANT_EXPIRY = datetime(2099, 1, 1, tzinfo=UTC)
SETTINGS = SimpleNamespace(multi_workspace_enabled=False)


class _Rows:
    """Minimal result object exposing ``.all()`` for ``session.scalars``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Session:
    """Scripted session: ``scalar``/``scalars``/``get`` pop queued answers and log the table touched."""

    def __init__(
        self,
        log: list[str],
        index: int,
        scalar: list[Any] | None = None,
        scalars: list[list[Any]] | None = None,
        get: list[Any] | None = None,
    ) -> None:
        self.log, self.index = log, index
        self._scalar, self._scalars, self._get = (
            list(scalar or []),
            list(scalars or []),
            list(get or []),
        )

    def _note(self, kind: str, stmt: object) -> None:
        match = re.search(r"FROM (\w+)", str(stmt))
        self.log.append(f"{self.index}:{kind}:{match.group(1) if match else '?'}")

    async def scalar(self, stmt: object) -> Any:
        self._note("scalar", stmt)
        return self._scalar.pop(0)

    async def scalars(self, stmt: object) -> _Rows:
        self._note("scalars", stmt)
        return _Rows(self._scalars.pop(0))

    async def get(self, model: object, key: object) -> Any:
        self.log.append(f"{self.index}:get:{getattr(model, '__tablename__', model)}")
        return self._get.pop(0)

    async def commit(self) -> None:
        self.log.append(f"{self.index}:commit")

    async def rollback(self) -> None:
        self.log.append(f"{self.index}:rollback")


class _Factory:
    """Session factory handing out the scripted sessions in order, one per ``async with``."""

    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.log: list[str] = []
        self.sessions = [_Session(self.log, i, **script) for i, script in enumerate(scripts)]
        self.opened = 0

    def __call__(self) -> "_Factory":
        return self

    async def __aenter__(self) -> _Session:
        session = self.sessions[self.opened]
        self.opened += 1
        return session

    async def __aexit__(self, *exc: object) -> None:
        return None


def _outbox(**over: Any) -> SimpleNamespace:
    base = {
        "id": OUTBOX_ID,
        "delivery_id": DELIVERY_ID,
        "state": "pending",
        "binding_cursor": None,
        "fanout_page": None,
        "attempts": 0,
        "capacity_reserved": True,
        "next_attempt_at": datetime.now(UTC),
    }
    return SimpleNamespace(**(base | over))


def _delivery(installation_id: str | None = "22") -> SimpleNamespace:
    return SimpleNamespace(
        id=DELIVERY_ID,
        app_id="33",
        installation_id=installation_id,
        repository_id="11",
        targets=[
            {"resource": "issue", "locator_kind": "number", "locator": "9", "intent": "refresh"}
        ],
    )


def _binding() -> connectors.GitHubEventBinding:
    return connectors.GitHubEventBinding(
        workspace_id=WORKSPACE_ID,
        actor_user_id=7,
        membership_revision=3,
        workspace_configuration_revision=5,
        source_id=SOURCE_ID,
        source_status="active",
        source_generation=2,
        connector_revision=4,
        grant_state="ready",
        grant_expires_at=GRANT_EXPIRY,
        repository_id="11",
        installation_id="22",
        app_id="33",
        binding_revision=6,
        resource_scope=("issue",),
    )


def _page(*, cursor_after: str | None = None) -> dict[str, Any]:
    fanout = connectors._GitHubFanoutBinding.model_validate(_binding().model_dump(mode="json"))
    return connectors._GitHubFanoutPage(
        cursor_before=None,
        cursor_after=cursor_after,
        bindings=(fanout,),
        admissions=(),
    ).model_dump(mode="json")


async def _dispatch(factory: _Factory, **patches: Any) -> int:
    ctx = {"settings": SETTINGS, "session_factory": factory}
    with (
        patch_worker("expire_github_webhook_details", AsyncMock(return_value=0)),
        patch_worker("reclaim_github_webhook_digests", AsyncMock(return_value=0)),
        patch_worker("revoke_deleted_source_github_grants", AsyncMock(return_value=0)),
        patch(
            "modules.settings.public.module_is_enabled",
            patches.get("enabled", AsyncMock(return_value=True)),
        ),
        patch.object(connectors, "enqueue_github_hint", patches.get("enqueue", AsyncMock())),
        patch.object(connectors, "list_github_event_bindings", patches.get("list", AsyncMock())),
        patch.object(connectors, "wake_packaged_collection", patches.get("wake", AsyncMock())),
        patch.object(
            connectors,
            "reconcile_github_source_hints_lifecycle",
            patches.get("reconcile", AsyncMock()),
        ),
        patch_worker("_admit_source_job", patches.get("admit", AsyncMock(return_value=None))),
    ):
        return await worker.dispatch_github_webhooks(ctx)  # type: ignore[arg-type]


NO_OUTBOXES = {"scalars": [[]]}
NO_HINT_TAIL = {"scalar": [None, None]}


@pytest.mark.asyncio
async def test_idle_dispatch_reads_ids_then_probes_hints_and_returns_zero() -> None:
    factory = _Factory([NO_OUTBOXES, NO_HINT_TAIL])
    assert await _dispatch(factory) == 0
    assert factory.log == [
        "0:scalars:github_webhook_outbox",
        "1:scalar:github_source_hints",
        "1:scalar:github_source_hints",
        "1:rollback",
    ]


@pytest.mark.asyncio
async def test_outbox_without_installation_completes_and_releases_capacity() -> None:
    outbox, capacity = _outbox(), SimpleNamespace(pending_count=3)
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery(None)]},
            {"scalar": [outbox, capacity]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory) == 1, factory.log
    assert (outbox.state, outbox.binding_cursor, outbox.fanout_page, outbox.capacity_reserved) == (
        "complete",
        None,
        None,
        False,
    )
    assert capacity.pending_count == 2
    assert (
        factory.log[1:6]
        == [
            "1:get:github_webhook_outbox",
            "1:get:github_webhook_deliveries",
            "1:rollback",
            "2:scalar:github_webhook_outbox",
            "2:scalar:github_webhook_capacity",
        ]
        and factory.log[6] == "2:commit"
    )


@pytest.mark.asyncio
async def test_installationless_outbox_that_changed_is_skipped_without_progress() -> None:
    outbox = _outbox(state="complete")
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [_outbox(), _delivery(None)]},
            {"scalar": [outbox]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory) == 0
    assert "2:rollback" in factory.log and "2:commit" not in factory.log


@pytest.mark.asyncio
async def test_missing_outbox_or_delivery_is_skipped() -> None:
    factory = _Factory([{"scalars": [[OUTBOX_ID]]}, {"get": [None, None]}, NO_HINT_TAIL])
    assert await _dispatch(factory) == 0
    assert factory.log[1:3] == ["1:get:github_webhook_outbox", "1:rollback"]


@pytest.mark.asyncio
async def test_new_page_is_frozen_fanned_out_and_outbox_completes() -> None:
    outbox = _outbox()
    current = _outbox()
    settle = _outbox(fanout_page=_page())
    capacity = SimpleNamespace(pending_count=1)
    detached = SimpleNamespace(items=(_binding(),), next_cursor=None)
    enqueue = AsyncMock(return_value=SimpleNamespace(disposition="pending"))
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery()]},
            {},  # list_github_event_bindings runs inside this session (mocked)
            {"scalar": [current]},  # freeze page under outbox lock
            {},  # module_is_enabled + enqueue
            {"scalar": [settle, capacity]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory, list=AsyncMock(return_value=detached), enqueue=enqueue) == 1, (
        factory.log
    )
    assert current.fanout_page == _page()
    assert factory.log[4:7] == ["2:rollback", "3:scalar:github_webhook_outbox", "3:commit"]
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert (
        kwargs["delivery_receipt_id"] == DELIVERY_ID and kwargs["expected_binding_cursor"] is None
    )
    assert kwargs["scope"].source_id == SOURCE_ID and kwargs["scope"].source_generation == 2
    assert (
        settle.state == "complete"
        and settle.capacity_reserved is False
        and capacity.pending_count == 0
    )


@pytest.mark.asyncio
async def test_stored_page_with_more_cursor_advances_outbox() -> None:
    outbox = _outbox(fanout_page=_page(cursor_after="next"))
    settle = _outbox(fanout_page=_page(cursor_after="next"))
    enqueue = AsyncMock(return_value=SimpleNamespace(disposition="coalesced"))
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery()]},
            {},
            {"scalar": [settle, SimpleNamespace(pending_count=4)]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory, enqueue=enqueue) == 1, factory.log
    assert (settle.binding_cursor, settle.fanout_page, settle.state) == ("next", None, "pending")


@pytest.mark.asyncio
async def test_denied_binding_is_skipped_but_other_failures_schedule_retry() -> None:
    for exc, expected_state, attempts in (
        (ForbiddenError("no"), "complete", 0),
        (ConflictError("stale"), "complete", 0),
        (RuntimeError("boom"), "pending", 1),
    ):
        outbox = _outbox(fanout_page=_page())
        settle = _outbox(fanout_page=_page())
        factory = _Factory(
            [
                {"scalars": [[OUTBOX_ID]]},
                {"get": [outbox, _delivery()]},
                {},
                {"scalar": [settle, SimpleNamespace(pending_count=2)]},
                NO_HINT_TAIL,
            ]
        )
        assert await _dispatch(factory, enqueue=AsyncMock(side_effect=exc)) == 1, factory.log
        assert (settle.state, settle.attempts) == (expected_state, attempts)
        if attempts:
            assert settle.next_attempt_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_capacity_exhausted_counts_as_failure_and_fifth_attempt_needs_attention() -> None:
    outbox = _outbox(fanout_page=_page(), attempts=4)
    settle = _outbox(fanout_page=_page(), attempts=4)
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery()]},
            {},
            {"scalar": [settle, SimpleNamespace(pending_count=2)]},
            NO_HINT_TAIL,
        ]
    )
    enqueue = AsyncMock(return_value=SimpleNamespace(disposition="capacity_exhausted"))
    assert await _dispatch(factory, enqueue=enqueue) == 1, factory.log
    assert (settle.state, settle.attempts) == ("needs_attention", 5)
    assert settle.next_attempt_at > datetime.now(UTC) + timedelta(minutes=50)


@pytest.mark.asyncio
async def test_stale_stored_page_cursor_fails_the_attempt() -> None:
    page = _page()
    page["cursor_before"] = "other"
    outbox = _outbox(fanout_page=page)
    settle = _outbox(fanout_page=page)
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery()]},
            {"scalar": [settle, SimpleNamespace(pending_count=2)]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory) == 1, factory.log
    assert (settle.state, settle.attempts) == ("pending", 1)


@pytest.mark.asyncio
async def test_binding_listing_failure_fails_the_attempt() -> None:
    outbox = _outbox()
    settle = _outbox()
    factory = _Factory(
        [
            {"scalars": [[OUTBOX_ID]]},
            {"get": [outbox, _delivery()]},
            {},
            {"scalar": [settle, SimpleNamespace(pending_count=2)]},
            NO_HINT_TAIL,
        ]
    )
    assert await _dispatch(factory, list=AsyncMock(side_effect=RuntimeError("down"))) == 1, (
        factory.log
    )
    assert (settle.state, settle.attempts) == ("pending", 1)


def _hint(**over: Any) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "source_id": SOURCE_ID,
        "source_generation": 2,
        "connector_revision": 4,
        "dirty_revision": 5,
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
        "capacity_reserved": True,
    }
    return SimpleNamespace(**(base | over))


async def _wake_case(
    wake_result: Any, *, attempts: int = 0, admitted: bool = True
) -> SimpleNamespace:
    hint = _hint(attempts=attempts)
    settle = _hint(id=hint.id, dirty_revision=5, state="dispatched", attempts=hint.attempts + 1)
    admit = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()) if admitted else None)
    factory = _Factory([NO_OUTBOXES, {"scalar": [None, hint]}, {}, {"scalar": [settle]}])
    progressed = await _dispatch(factory, wake=AsyncMock(return_value=wake_result), admit=admit)
    assert progressed == 1
    assert factory.log[-2:] == ["3:scalar:github_source_hints", "3:commit"]
    return settle


@pytest.mark.asyncio
async def test_hint_wake_outcomes_set_the_next_state() -> None:
    now = datetime.now(UTC)
    acknowledged = await _wake_case(SimpleNamespace(outcome="acknowledged", next_eligible_at=None))
    assert acknowledged.state == "dispatched" and acknowledged.next_attempt_at > now
    deferred_at = now + timedelta(minutes=10)
    deferred = await _wake_case(SimpleNamespace(outcome="deferred", next_eligible_at=deferred_at))
    assert deferred.state == "pending" and deferred.next_attempt_at == deferred_at
    failed = await _wake_case(SimpleNamespace(outcome="failed", next_eligible_at=None))
    assert failed.state == "pending" and failed.next_attempt_at > now
    exhausted = await _wake_case(
        SimpleNamespace(outcome="failed", next_eligible_at=None), attempts=4
    )
    assert exhausted.state == "needs_attention" and exhausted.next_attempt_at > now + timedelta(
        minutes=50
    )
    unadmitted = await _wake_case(None, admitted=False)
    assert unadmitted.state == "pending" and unadmitted.next_attempt_at > now


@pytest.mark.asyncio
async def test_hint_claim_marks_dispatched_and_bumps_attempts_before_wake() -> None:
    hint = _hint(attempts=3)
    settle = _hint(id=hint.id, dirty_revision=99, state="dispatched")
    factory = _Factory([NO_OUTBOXES, {"scalar": [None, hint]}, {}, {"scalar": [settle]}])
    await _dispatch(factory, wake=AsyncMock(return_value=None), admit=AsyncMock(return_value=None))
    assert (hint.attempts, hint.state) == (4, "dispatched")
    assert factory.log[-1] == "3:rollback"  # dirty revision moved: settle discards its write


@pytest.mark.asyncio
async def test_denied_wake_is_swallowed_and_unexpected_wake_error_propagates() -> None:
    hint = _hint()
    settle = _hint(id=hint.id, dirty_revision=5, state="dispatched", attempts=1)
    admit = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    factory = _Factory([NO_OUTBOXES, {"scalar": [None, hint]}, {}, {"scalar": [settle]}])
    assert (
        await _dispatch(factory, wake=AsyncMock(side_effect=ForbiddenError("x")), admit=admit) == 1
    ), factory.log
    assert (
        factory.log[-3:-1] == ["2:rollback", "3:scalar:github_source_hints"]
        or "2:rollback" in factory.log
    )
    assert settle.state == "pending"

    from fastapi import HTTPException

    factory2 = _Factory([NO_OUTBOXES, {"scalar": [None, _hint()]}, {}])
    with pytest.raises(HTTPException):
        await _dispatch(
            factory2,
            wake=AsyncMock(side_effect=HTTPException(status_code=500, detail="boom")),
            admit=admit,
        )


@pytest.mark.asyncio
async def test_deferred_hint_recovery_reconciles_active_source_then_rotates_remaining_hints() -> (
    None
):
    remaining = [
        _hint(
            state="capacity_deferred",
            capacity_reserved=False,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    ]
    source = SimpleNamespace(status="active", generation=2)
    reconcile = AsyncMock(return_value=0)
    admit = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    factory = _Factory([NO_OUTBOXES, {"scalar": [SOURCE_ID, None], "scalars": [remaining]}])
    with (
        patch("modules.sources.public.lock_source", AsyncMock(return_value=SimpleNamespace())),
        patch("modules.sources.public.get_connector_source", AsyncMock(return_value=source)),
    ):
        assert await _dispatch(factory, reconcile=reconcile, admit=admit) == 0
    reconcile.assert_awaited_once()
    assert (
        reconcile.await_args.kwargs["active"] is True
        and reconcile.await_args.kwargs["source_generation"] == 2
    )
    assert remaining[0].next_attempt_at > datetime.now(UTC) + timedelta(seconds=30)
    assert factory.log[1:] == [
        "1:scalar:github_source_hints",
        "1:scalars:github_source_hints",
        "1:commit",
        "1:scalar:github_source_hints",
        "1:rollback",
    ]


@pytest.mark.asyncio
async def test_deferred_hint_recovery_swallows_denied_admission_and_reraises_others() -> None:
    admit = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    factory = _Factory([NO_OUTBOXES, {"scalar": [SOURCE_ID, None], "scalars": [[]]}])
    with patch("modules.sources.public.lock_source", AsyncMock(side_effect=ForbiddenError("gone"))):
        assert await _dispatch(factory, admit=admit) == 0
    assert "1:rollback" in factory.log

    from fastapi import HTTPException

    factory2 = _Factory([NO_OUTBOXES, {"scalar": [SOURCE_ID]}])
    with (
        patch(
            "modules.sources.public.lock_source",
            AsyncMock(side_effect=HTTPException(status_code=500, detail="x")),
        ),
        pytest.raises(HTTPException),
    ):
        await _dispatch(factory2, admit=admit)
