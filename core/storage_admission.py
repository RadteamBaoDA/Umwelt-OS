"""Drain physical upload writers before account-erasure storage cleanup."""

import asyncio
import hashlib
import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker


class WorkspaceStorageBusy(RuntimeError):
    """A physical upload or cleanup still owns the workspace's storage lease."""


@contextmanager
def workspace_filesystem_lease(root: Path, workspace_id: UUID, *, erase: bool = False) -> Iterator[None]:
    """Fence originals at their destination independently of database connections.

    A nonblocking exclusive OS file lock serializes upload/erasure per workspace across
    processes. Erasure callers first prove durable account authority, then set and fsync a
    permanent one-byte deletion flag before touching originals. Upload callers check that
    flag before creating content and hold this lock until all physical writers have joined.
    The marker filename holds only an opaque workspace UUID; it must never be unlinked or
    restored to zero, because delayed uploads may still hold pre-deletion SQL admission.
    Busy/erased workspaces raise WorkspaceStorageBusy; invalid markers or storage errors fail
    closed. OS process death releases the lock but preserves the deletion flag.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError("Storage admission requires a workspace UUID")
    base = Path(root).resolve()
    directory = base / ".storage-fences"
    marker = directory / f"{workspace_id}.lock"
    if directory.is_symlink() or directory.is_junction() or marker.is_symlink() or marker.is_junction():
        raise ValueError("Storage fence cannot contain a symlink or junction")
    directory.mkdir(parents=True, exist_ok=True)
    # Never truncate/replace this inode: another process can already be waiting on its lock.
    # Unbuffered seeks keep the native byte-range lock offset at zero on Windows.
    with os.fdopen(os.open(marker, os.O_CREAT | os.O_RDWR, 0o600), "r+b", buffering=0) as lock_file:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkspaceStorageBusy("Workspace physical storage is still active") from exc
        try:
            state = lock_file.read(2)
            if state not in {b"", b"0", b"1"}:
                raise RuntimeError("Workspace storage deletion marker is invalid")
            if state == b"1" and not erase:
                raise WorkspaceStorageBusy("Workspace physical storage was erased")
            expected = b"1" if erase else b"0"
            if state != expected:
                lock_file.seek(0)
                lock_file.write(expected)
                lock_file.truncate(1)
                lock_file.flush()
                os.fsync(lock_file.fileno())
            yield
        finally:
            lock_file.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def workspace_storage_lease(
    factory: async_sessionmaker[AsyncSession], workspace_id: UUID, *, exclusive: bool = False,
) -> AsyncIterator[AsyncConnection]:
    """Hold a nonblocking session advisory lease without a transaction over file I/O.

    Uploads use shared leases and recheck original account admission inside them. Erasure
    uses an exclusive lease after account disablement: all earlier writers must drain and
    later writers fail their fresh admission. Connection loss releases this database lease,
    so upload/erasure callers also hold workspace_filesystem_lease at the destination;
    SQL ownership alone cannot prove physical writer cessation.
    """
    if not isinstance(workspace_id, UUID):
        raise TypeError('Storage admission requires a workspace UUID')
    key = int.from_bytes(hashlib.blake2b(workspace_id.bytes, digest_size=8, person=b'bbd-storage').digest(), 'big', signed=True)
    acquire = 'pg_try_advisory_lock' if exclusive else 'pg_try_advisory_lock_shared'
    release = 'pg_advisory_unlock' if exclusive else 'pg_advisory_unlock_shared'
    engine = factory.kw['bind']
    async with engine.connect() as connection:
        acquired = await connection.scalar(text(f'SELECT {acquire}(:key)'), {'key': key})
        await connection.commit()
        if not acquired:
            raise WorkspaceStorageBusy('Workspace storage is still active')
        try:
            yield connection
        finally:
            try:
                await asyncio.shield(connection.execute(text(f'SELECT {release}(:key)'), {'key': key}))
                await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise
