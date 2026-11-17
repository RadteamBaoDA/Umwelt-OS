"""PostgreSQL-backed workspace storage fence for object-storage backends (no filesystem locks).

Same invariants as ``workspace_filesystem_lease``: a writer excludes every other writer and any erasure
for the workspace, an erasure records a permanent deletion flag before touching originals, and a delayed
upload that still holds a pre-deletion SQL admission is rejected once the flag exists. The flag is the
``workspace_storage_state.deleted_at`` row (migration ``0016_workspace_storage_state``); a database
trigger forbids clearing it or deleting the row.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.storage.admission import WorkspaceStorageBusy

# Upper bound for one fence hold. Applied as idle_in_transaction_session_timeout so a crashed holder's
# transaction (and its advisory lock) is released by the server instead of leaking forever.
FENCE_MAX_HOLD_MS = 2 * 60 * 60 * 1000

_ENGINES: dict[str, AsyncEngine] = {}

# Process-wide cap on concurrently held fences. Each hold keeps one dedicated NullPool connection for the whole
# transfer (two briefly during erasure), so without a cap an upload burst could reach the server's max_connections.
# A plain counter (not a semaphore) stays safe across event loops; saturation fails fast as 503 (WorkspaceStorageBusy).
MAX_CONCURRENT_HOLDS = 16
_active_holds = 0


def fence_lock_key(workspace_id: UUID) -> int:
    """Return the signed 64-bit advisory-lock key for ``workspace_id`` (namespaced apart from the SQL lease)."""
    return int.from_bytes(
        hashlib.blake2b(workspace_id.bytes, digest_size=8, person=b"bbd-sfence").digest(),
        "big",
        signed=True,
    )


def fence_engine(database_url: str) -> AsyncEngine:
    """Return a process-wide ``NullPool`` engine for ``database_url`` (created lazily, never disposed).

    ``NullPool`` opens one short-lived connection per use, so the engine is safe across event loops
    (API, worker, tests) and the fence never competes with the request pool for connections.
    """
    engine = _ENGINES.get(database_url)
    if engine is None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        _ENGINES[database_url] = engine
    return engine


class PostgresStorageFence:
    """``StorageFence`` using ``pg_try_advisory_xact_lock`` plus the durable ``workspace_storage_state`` flag."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind to ``engine``; connections are opened per hold and closed on exit."""
        self._engine = engine

    def hold(self, workspace_id: UUID, *, erase: bool = False) -> AbstractContextManager[None]:
        """Unsupported: database I/O is asynchronous. Raises ``NotImplementedError``; use ``ahold``."""
        raise NotImplementedError("PostgresStorageFence is async-only; use ahold()")

    @asynccontextmanager
    async def ahold(self, workspace_id: UUID, *, erase: bool = False) -> AsyncIterator[None]:
        """Hold the workspace fence for the body of the ``async with`` block.

        Order matters: (1) take the non-blocking advisory transaction lock on a dedicated connection and
        keep that transaction open for the whole body (no data is read or written through it, so there
        is no row lock or snapshot to contend on); busy raises ``WorkspaceStorageBusy`` before anything
        changes. (2) ``erase=True`` commits the permanent deletion flag on a SEPARATE short transaction
        so it is durable even if the erasure later fails or the holder dies; ``erase=False`` reads the
        flag and raises ``WorkspaceStorageBusy('...erased')`` when set. The flag is never cleared.
        Connection loss releases only the lock, never the flag, mirroring the file-lock fence.
        """
        if not isinstance(workspace_id, UUID):
            raise TypeError("Storage admission requires a workspace UUID")
        global _active_holds
        if _active_holds >= MAX_CONCURRENT_HOLDS:
            raise WorkspaceStorageBusy("Too many concurrent storage operations; retry shortly")
        _active_holds += 1
        try:
            async with self._fenced(workspace_id, erase):
                yield
        finally:
            _active_holds -= 1

    @asynccontextmanager
    async def _fenced(self, workspace_id: UUID, erase: bool) -> AsyncIterator[None]:
        """Take the advisory lock and check or set the erasure flag; see ``ahold`` for the ordering contract."""
        async with self._engine.connect() as lock_connection, lock_connection.begin():
            await lock_connection.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {FENCE_MAX_HOLD_MS}")
            )
            acquired = await lock_connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": fence_lock_key(workspace_id)},
            )
            if not acquired:
                raise WorkspaceStorageBusy("Workspace physical storage is still active")
            if erase:
                async with self._engine.begin() as flag_connection:
                    # COALESCE keeps the first deletion time; the trigger rejects any other change.
                    await flag_connection.execute(
                        text(
                            "INSERT INTO workspace_storage_state (workspace_id, deleted_at) VALUES (:ws, now()) "
                            "ON CONFLICT (workspace_id) DO UPDATE "
                            "SET deleted_at = COALESCE(workspace_storage_state.deleted_at, EXCLUDED.deleted_at)"
                        ),
                        {"ws": workspace_id},
                    )
            else:
                erased = await lock_connection.scalar(
                    text(
                        "SELECT deleted_at IS NOT NULL FROM workspace_storage_state WHERE workspace_id = :ws"
                    ),
                    {"ws": workspace_id},
                )
                if erased:
                    raise WorkspaceStorageBusy("Workspace physical storage was erased")
            yield
