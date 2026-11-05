"""``PostgresStorageFence`` semantics against a scripted fake engine (SQL order, busy, erased, permanence)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from core.storage import WorkspaceStorageBusy
from core.storage.pg_fence import PostgresStorageFence, fence_lock_key

WORKSPACE = UUID(int=5)


class _FakeDatabase:
    """Minimal stand-in for the state the fence uses: advisory locks and erased flags."""

    def __init__(self) -> None:
        """Start with no locks and no flags; ``log`` records every statement in order."""
        self.locks: set[int] = set()
        self.erased: set[UUID] = set()
        self.log: list[str] = []


class _FakeConnection:
    """Executes the fence's statements against ``_FakeDatabase`` and releases xact locks on transaction end."""

    def __init__(self, database: _FakeDatabase) -> None:
        """Bind to the shared database; remember locks taken so transaction end can release them."""
        self._db = database
        self._mine: set[int] = set()

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        """Transaction scope: advisory xact locks vanish at its end."""
        try:
            yield
        finally:
            self._db.locks -= self._mine

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        """Record writes; the INSERT sets the durable flag immediately (own committed transaction)."""
        sql = str(statement)
        if "INSERT INTO workspace_storage_state" in sql:
            self._db.log.append("flag-insert")
            assert params is not None
            self._db.erased.add(params["ws"])
        else:
            self._db.log.append("set-local")

    async def scalar(self, statement: Any, params: dict[str, Any]) -> bool:
        """Answer the try-lock and the erased-flag read."""
        if "pg_try_advisory_xact_lock" in str(statement):
            self._db.log.append("lock")
            if params["key"] in self._db.locks:
                return False
            self._db.locks.add(params["key"])
            self._mine.add(params["key"])
            return True
        self._db.log.append("read-flag")
        return params["ws"] in self._db.erased


class _FakeEngine:
    """Hands out fake connections like ``AsyncEngine.connect()`` / ``.begin()``."""

    def __init__(self, database: _FakeDatabase) -> None:
        """Bind to the shared database."""
        self._db = database

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_FakeConnection]:
        """Yield a connection (the caller opens the transaction)."""
        yield _FakeConnection(self._db)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_FakeConnection]:
        """Yield a connection inside its own short transaction."""
        connection = _FakeConnection(self._db)
        async with connection.begin():
            yield connection


@pytest.fixture
def fence() -> tuple[PostgresStorageFence, _FakeDatabase]:
    """Return a fence wired to a fresh fake database."""
    database = _FakeDatabase()
    return PostgresStorageFence(_FakeEngine(database)), database  # type: ignore[arg-type]


async def test_second_holder_is_busy_and_lock_is_released_afterwards(fence: Any) -> None:
    pg, database = fence
    async with pg.ahold(WORKSPACE):
        with pytest.raises(WorkspaceStorageBusy, match="still active"):
            async with pg.ahold(WORKSPACE):
                pytest.fail("second writer overlapped")
        with pytest.raises(WorkspaceStorageBusy):
            async with pg.ahold(WORKSPACE, erase=True):
                pytest.fail("erasure overtook a live writer")
        assert WORKSPACE not in database.erased  # a busy erase must not set the flag
    async with pg.ahold(WORKSPACE):
        pass
    assert fence_lock_key(WORKSPACE) not in database.locks


async def test_erase_sets_permanent_flag_that_rejects_later_uploads(fence: Any) -> None:
    pg, database = fence
    async with pg.ahold(WORKSPACE, erase=True):
        pass
    assert WORKSPACE in database.erased
    with pytest.raises(WorkspaceStorageBusy, match="erased"):
        async with pg.ahold(WORKSPACE):
            pytest.fail("late upload accepted after erasure")
    async with pg.ahold(WORKSPACE, erase=True):  # erasure may re-enter; the flag stays set
        pass
    assert WORKSPACE in database.erased


async def test_flag_survives_a_failing_erasure_body(fence: Any) -> None:
    pg, database = fence
    with pytest.raises(RuntimeError):
        async with pg.ahold(WORKSPACE, erase=True):
            raise RuntimeError("erasure crashed halfway")
    assert WORKSPACE in database.erased
    assert not database.locks


async def test_lock_is_taken_before_the_flag_is_written(fence: Any) -> None:
    pg, database = fence
    async with pg.ahold(WORKSPACE, erase=True):
        pass
    assert [entry for entry in database.log if entry in {"lock", "flag-insert"}] == ["lock", "flag-insert"]


def test_sync_hold_is_unsupported(fence: Any) -> None:
    pg, _ = fence
    with pytest.raises(NotImplementedError):
        pg.hold(WORKSPACE)


async def test_non_uuid_workspace_is_rejected(fence: Any) -> None:
    pg, _ = fence
    with pytest.raises(TypeError):
        async with pg.ahold("not-a-uuid"):
            pass
