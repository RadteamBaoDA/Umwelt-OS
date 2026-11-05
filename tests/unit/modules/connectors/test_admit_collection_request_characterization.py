"""Characterization tests for ``scheduler.admit_collection_request`` outcomes and commit order."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from core.errors import ConflictError, ForbiddenError
from modules.connectors import scheduler

REQUEST_ID = uuid4()
SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()


class _Session:
    """Scripted session recording operations; ``get`` serves the peek, ``scalar`` the locked rows."""

    def __init__(self, peek: Any, scalars: list[Any], flush_error: Exception | None = None) -> None:
        self.peek, self._scalars, self.flush_error = peek, list(scalars), flush_error
        self.log: list[str] = []
        self.executed: list[Any] = []

    async def get(self, model: object, key: object, **kwargs: object) -> Any:
        self.log.append("get")
        return self.peek

    async def scalar(self, stmt: object) -> Any:
        self.log.append("scalar")
        return self._scalars.pop(0)

    async def execute(self, stmt: object) -> None:
        self.log.append("execute")
        self.executed.append(stmt)

    async def flush(self) -> None:
        self.log.append("flush")
        if self.flush_error:
            raise self.flush_error

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")


def _request(**over: Any) -> SimpleNamespace:
    base = {
        "id": REQUEST_ID, "source_id": SOURCE_ID, "workspace_id": WORKSPACE_ID, "status": "queued",
        "actor_user_id": 7, "membership_revision": 3, "source_generation": 2, "attempt": 0,
        "available_at": datetime.now(UTC) - timedelta(seconds=1), "provider_deadline": None,
        "error_code": None, "active_admission_token": None,
    }
    return SimpleNamespace(**(base | over))


async def _admit(session: _Session, *, enabled: bool = True, access: Any = None, stale: str | None = None,
                 lock: Any = None) -> tuple[Any, dict[str, Any]]:
    spies = {"defer": AsyncMock(), "stale": AsyncMock(return_value=stale)}
    with patch.object(scheduler.connectors, "_connector_access", access or AsyncMock()), \
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=enabled)), \
            patch.object(scheduler.provisioning, "lock_connector", lock or AsyncMock(return_value=("fence", "row", {}))), \
            patch("modules.sources.public.get_connector_source", AsyncMock(return_value=SimpleNamespace(provider=None, configuration=None))), \
            patch.object(scheduler, "_stale_reason", spies["stale"]), \
            patch.object(scheduler, "_defer", spies["defer"]), \
            patch.object(scheduler, "_penalize", lambda *a, **k: None):
        result = await scheduler.admit_collection_request(session, REQUEST_ID, multi_workspace_enabled=False)  # type: ignore[arg-type]
    return result, spies


@pytest.mark.asyncio
async def test_missing_or_non_queued_request_is_ignored() -> None:
    session = _Session(None, [])
    result, _ = await _admit(session)
    assert result is None and session.log == ["get", "rollback"]
    session2 = _Session(_request(status="running"), [])
    assert (await _admit(session2))[0] is None and session2.log == ["get", "rollback"]


@pytest.mark.asyncio
async def test_disabled_module_leaves_the_request_untouched() -> None:
    session = _Session(_request(), [])
    result, _ = await _admit(session, enabled=False)
    assert result is None and session.log == ["get", "rollback", "rollback"] and session.executed == []


@pytest.mark.asyncio
async def test_denied_principal_cancels_with_the_denial_code() -> None:
    session = _Session(_request(), [])
    result, _ = await _admit(session, access=AsyncMock(side_effect=ForbiddenError("no")))
    assert result is None and session.log[-3:] == ["rollback", "execute", "commit"]
    assert "cancelled" in str(session.executed[0].compile().params.values())
    assert "permission_lost" in str(session.executed[0].compile().params.values())


@pytest.mark.asyncio
async def test_conflict_is_reresolved_once_then_becomes_stale_scope() -> None:
    access = AsyncMock(side_effect=[ConflictError("x"), None])
    request = _request()
    slot = SimpleNamespace(occupied_request_id=None)
    session = _Session(_request(), [request, None, None, slot])
    result, _ = await _admit(session, access=access)
    assert result is not None and access.await_count == 2

    access2 = AsyncMock(side_effect=ConflictError("x"))
    session2 = _Session(_request(), [])
    result2, _ = await _admit(session2, access=access2)
    assert result2 is None and access2.await_count == 2
    assert "stale_scope" in str(session2.executed[0].compile().params.values())


@pytest.mark.asyncio
async def test_missing_future_or_non_queued_locked_request_rolls_back() -> None:
    for locked in (None, _request(status="running"), _request(available_at=datetime.now(UTC) + timedelta(hours=1))):
        session = _Session(_request(), [locked])
        result, _ = await _admit(session)
        assert result is None and session.log[-1] == "rollback"


@pytest.mark.asyncio
async def test_stale_and_exhausted_requests_are_terminal() -> None:
    locked = _request()
    session = _Session(_request(), [locked])
    result, _ = await _admit(session, stale="source_changed")
    assert result is None and (locked.status, locked.error_code) == ("cancelled", "source_changed")
    assert session.log[-1] == "commit"

    exhausted = _request(attempt=scheduler.MAX_ATTEMPTS)
    session2 = _Session(_request(), [exhausted, SimpleNamespace()])
    result2, _ = await _admit(session2)
    assert result2 is None and (exhausted.status, exhausted.error_code) == ("failed", "attempts_exhausted")


@pytest.mark.asyncio
async def test_busy_workspace_or_live_lease_or_no_slot_defers_without_attempt() -> None:
    now = datetime.now(UTC)
    for scripted in (
        [_request(), None, 5],                                        # workspace already holds a slot
        [_request(), SimpleNamespace(lease_expires_at=now + timedelta(minutes=5)), None],  # live ingestion lease
        [_request(), None, None, None],                               # no free global slot
    ):
        locked = scripted[0]
        session = _Session(_request(), scripted)
        result, spies = await _admit(session)
        assert result is None and locked.attempt == 0
        assert session.log[-1] == "rollback"
        spies["defer"].assert_awaited_once()
        assert spies["defer"].await_args.args[2] == scheduler.BUSY_DEFER


@pytest.mark.asyncio
async def test_free_slot_is_claimed_and_request_marked_running() -> None:
    locked = _request()
    slot = SimpleNamespace(occupied_request_id=None)
    session = _Session(_request(), [locked, None, None, slot])
    result, _ = await _admit(session)
    assert result is not None and result.request_id == REQUEST_ID and result.attempt == 1
    assert (locked.status, locked.attempt, locked.error_code) == ("running", 1, None)
    assert slot.occupied_request_id == REQUEST_ID and slot.lease_kind == "collection"
    assert slot.admission_token == locked.active_admission_token == result.admission_token
    assert session.log[-2:] == ["flush", "commit"]


@pytest.mark.asyncio
async def test_concurrent_same_workspace_admission_defers_after_integrity_error() -> None:
    locked = _request()
    slot = SimpleNamespace(occupied_request_id=None)
    session = _Session(_request(), [locked, None, None, slot], flush_error=IntegrityError("s", {}, Exception("x")))
    result, spies = await _admit(session)
    assert result is None and session.log[-2:] == ["flush", "rollback"]
    spies["defer"].assert_awaited_once()
