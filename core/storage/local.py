"""Local-filesystem storage backend, fence and the legacy path helpers it is built on."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

from fastapi import Response, UploadFile
from fastapi.responses import FileResponse

from core.heavy_work.slots import to_thread_joined
from core.storage.admission import workspace_filesystem_lease
from core.storage.backend import (
    OrphanProbe,
    StoredObject,
    build_document_key,
    iter_upload,
    require_document_key,
)


def storage_path(root: Path, relative_path: str) -> Path:
    """Resolve a storage-relative path beneath the configured root and reject traversal or absolute paths.

    Backslashes and drive-letter prefixes are rejected on every platform: on POSIX a backslash is an
    ordinary filename character, but keys are always generated with ``/`` separators, so a backslash
    or ``C:`` form can only be a Windows traversal/absolute attempt that would escape the root if the
    same key were ever resolved on Windows (or by a backend that normalises separators).

    Raises:
        ValueError: ``"Invalid storage path"`` for any rejected or escaping path.
    """
    if "\\" in relative_path or (len(relative_path) > 1 and relative_path[:1].isalpha() and relative_path[1] == ":"):
        raise ValueError("Invalid storage path")
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError("Invalid storage path")
    base = root.resolve()
    path = (base / candidate).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Invalid storage path")
    return path


def _write_block(temporary: BinaryIO, digest: hashlib._Hash, block: bytes) -> None:
    """Write one block and fold it into the running digest (runs in a joined worker thread)."""
    temporary.write(block)
    digest.update(block)


def _sync(temporary: BinaryIO) -> None:
    """Flush and fsync the temporary file (runs in a joined worker thread)."""
    temporary.flush()
    os.fsync(temporary.fileno())


async def save_upload(
    root: Path, upload: UploadFile, document_id: UUID, suffix: str, max_bytes: int, *, workspace_id: UUID,
) -> tuple[str, int, str]:
    """Publish bounded upload bytes atomically under the caller's admitted workspace.

    Compatibility wrapper over ``LocalFilesystemBackend.put_stream`` returning the relative URI,
    byte count and SHA-256. Validation, fence, atomicity and cleanup contracts are documented on
    ``put_stream``; invalid identity/suffix/byte limit raise ``ValueError`` before any I/O.
    """
    key = build_document_key(workspace_id, document_id, suffix)
    stored = await LocalFilesystemBackend(root).put_stream(
        key, iter_upload(upload), max_bytes, workspace_id=workspace_id,
    )
    return stored.key, stored.size, stored.sha256


def cleanup_orphaned_files(root: Path, referenced: set[str], grace_seconds: int) -> int:
    """Delete old unreferenced files only in legacy/scoped document upload layouts.

    Operator caller proves a complete live-reference set and owns maintenance admission;
    no workspace authorization is inferred here. Preserve referenced URIs verbatim, skip
    symlinks/escaping paths, and never enumerate arbitrary workspace files. Grace must be
    nonnegative. Return the number removed; no live URI is moved or duplicated.
    """
    if grace_seconds < 0:
        raise ValueError("Orphan cleanup grace must be nonnegative")
    if not root.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - grace_seconds
    removed = 0
    base = root.resolve()
    candidates = (path for pattern in ("documents/*/*", "workspaces/*/documents/*/*") for path in root.glob(pattern))
    for path in candidates:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            continue
        if not path.is_file() or not path.resolve().is_relative_to(base) or path.stat().st_mtime > cutoff:
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        document_text = parts[-2]
        try:
            if str(UUID(document_text)) != document_text:
                continue
            if parts[0] == "workspaces" and str(UUID(parts[1])) != parts[1]:
                continue
        except ValueError:
            continue
        # Only known upload publication/temporary names are candidates. A maintenance
        # scan of workspaces must never consume unrelated files with a similar directory.
        if not (path.name.startswith(document_text + ".") or path.name.startswith(".upload-")):
            continue
        if relative not in referenced and path.resolve().relative_to(base).as_posix() not in referenced:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _has_symlink_component(root: Path, lexical: Path) -> bool:
    """Return whether ``lexical`` or any parent inside ``root`` is a symlink or Windows junction.

    Checking the lexical path matters because ``resolve()`` alone would hide in-root redirects.
    """
    return any(
        (parent.is_symlink() or parent.is_junction())
        for parent in (lexical, *lexical.parents) if parent.is_relative_to(root)
    )


class LocalFilesystemFence:
    """``StorageFence`` backed by per-workspace OS file locks plus a permanent deletion flag."""

    def __init__(self, root: Path) -> None:
        """Bind the fence to the storage root that holds ``.storage-fences``."""
        self._root = Path(root)

    def hold(self, workspace_id: UUID, *, erase: bool = False) -> AbstractContextManager[None]:
        """Return ``workspace_filesystem_lease(root, workspace_id, erase=erase)`` unchanged."""
        return workspace_filesystem_lease(self._root, workspace_id, erase=erase)


class LocalFilesystemBackend:
    """``StorageBackend`` over a local directory; paths and URIs match the pre-abstraction layout.

    Blocking filesystem work that previously ran synchronously still does (see the A9 notes on
    ``put_stream``); only chunk writes are offloaded with ``to_thread_joined``.
    """

    def __init__(self, root: Path | str) -> None:
        """Bind to ``root`` (resolved lazily per call) and create the matching fence."""
        self._root = Path(root)
        self.fence = LocalFilesystemFence(self._root)

    async def put_stream(
        self, key: str, chunks: AsyncIterator[bytes], max_bytes: int, *, workspace_id: UUID,
    ) -> StoredObject:
        """Publish bounded bytes atomically under the caller's admitted workspace.

        Pure filesystem I/O: the caller supplies the actual authorized workspace and releases
        SQL locks before streaming. Return key, byte count and SHA-256; reject invalid byte
        limits, empty/oversized uploads, traversal and symlinks in the publication path.
        An OS destination lock and permanent deletion flag reject stale admitted uploads after account
        erasure, even if their SQL lease was lost. Cancellation joins physical writer threads before
        cleanup/unlock. Temporary bytes are removed on failure; existing legacy URIs are untouched.
        """
        root = self._root
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Invalid upload suffix or byte limit")
        # The key must belong to the workspace whose fence is held below (blocks cross-workspace/fence-dir writes).
        require_document_key(workspace_id, key)
        # A9 (deliberately not offloaded): the lease, mkdir/mkstemp and the final os.replace are microsecond-scale
        # on local disk. Moving them to threads would let a cancellation land after os.replace published the file
        # (or after mkstemp created an fd) and change the cleanup/atomicity contract above. A network backend
        # implements its own publication protocol behind the same interface.
        with self.fence.hold(workspace_id):
            relative = Path(key)
            destination = storage_path(root, relative.as_posix())
            # Check the lexical path as well: resolve() alone would hide in-root symlink redirects.
            lexical = root.resolve() / relative
            if _has_symlink_component(root.resolve(), lexical):
                raise ValueError("Upload path cannot contain symlinks")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
            size = 0
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as temporary:
                    async for block in chunks:
                        size += len(block)
                        if size > max_bytes:
                            raise ValueError("Upload exceeds the configured size limit")
                        await to_thread_joined(_write_block, temporary, digest, block)
                    await to_thread_joined(_sync, temporary)
                if size == 0:
                    raise ValueError("Uploaded file is empty")
                if storage_path(root, relative.as_posix()) != destination or _has_symlink_component(root.resolve(), lexical):
                    raise ValueError("Upload path changed during publication")
                os.replace(temporary_name, destination)
                return StoredObject(relative.as_posix(), size, digest.hexdigest())
            except BaseException:
                Path(temporary_name).unlink(missing_ok=True)
                raise

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the object's bytes in 1 MiB blocks, each read offloaded to a joined thread.

        Raises ``ValueError`` for invalid keys and ``FileNotFoundError`` when absent.
        """
        path = storage_path(self._root, key)
        handle: BinaryIO = await to_thread_joined(path.open, "rb")
        try:
            while block := cast(bytes, await to_thread_joined(handle.read, 1024 * 1024)):
                yield block
        finally:
            handle.close()

    def local_path(self, key: str) -> Path:
        """Return the validated absolute path of ``key`` (``ValueError`` on traversal)."""
        return storage_path(self._root, key)

    @asynccontextmanager
    async def download_to_temp(self, key: str) -> AsyncIterator[Path]:
        """Yield the real on-disk path: local objects are already files, so nothing is copied."""
        yield self.local_path(key)

    def file_response(self, key: str, *, media_type: str, filename: str) -> Response:
        """Return ``FileResponse`` for the object's path with the given media type and filename."""
        return FileResponse(self.local_path(key), media_type=media_type, filename=filename)

    async def delete(self, key: str, *, missing_ok: bool = True) -> None:
        """Unlink the object synchronously (A9: ordering with SQL under fences must not split).

        With ``missing_ok=False`` also raises ``RuntimeError`` if anything (file, directory or special
        entry) remains at the path afterwards, matching the pre-abstraction ``Path.exists()`` confirmation.
        """
        path = self.local_path(key)
        path.unlink(missing_ok=missing_ok)
        if not missing_ok and path.exists():
            raise RuntimeError("Original storage unlink was not confirmed")

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` is an existing regular file."""
        return self.local_path(key).is_file()

    async def list_prefix(self, prefix: str) -> list[str]:
        """List regular files beneath ``prefix`` (sorted POSIX keys), never following symlinks."""
        base = storage_path(self._root, prefix) if prefix else self._root.resolve()
        root = self._root.resolve()

        def walk() -> list[str]:
            """Collect keys in a worker thread."""
            found: list[str] = []
            for directory, names, files in os.walk(base, followlinks=False):
                names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
                found.extend(
                    (Path(directory) / name).relative_to(root).as_posix()
                    for name in files if not (Path(directory) / name).is_symlink()
                )
            return sorted(found)

        return await to_thread_joined(walk)

    async def cleanup_orphans(self, referenced: set[str], grace_seconds: int) -> int:
        """Run ``cleanup_orphaned_files`` in a thread (the worker's previous ``asyncio.to_thread``)."""
        return await asyncio.to_thread(cleanup_orphaned_files, self._root, referenced, grace_seconds)

    def check_erasable(self, key: str, workspace_id: UUID) -> None:
        """Validate that ``key`` is a non-symlink original in the legacy or this workspace's layout.

        Raises ``ValueError`` with the same messages the erasure stage used before the abstraction.
        """
        root = self._root.resolve()
        target = storage_path(root, key)
        if _has_symlink_component(root, root / key):
            raise ValueError("Original storage path contains a symlink")
        if not (target.is_relative_to(root / "documents") or target.is_relative_to(
            root / "workspaces" / str(workspace_id) / "documents",
        )):
            raise ValueError("Original storage path is outside the account workspace/document layouts")

    def probe_workspace_orphan(self, workspace_id: UUID) -> OrphanProbe:
        """Advance the orphan walk by one entry: read one child per level, at most one mutation.

        A recursive glob could list every sibling before finding a file, so only one entry is read
        per level; a large orphan tree cannot monopolize a worker invocation. Deliberately
        synchronous (A9): the stat/scandir/rmdir calls interleave with SQL under the erasure fence.
        """
        root = self._root.resolve()
        lexical = root / "workspaces" / str(workspace_id) / "documents"
        if _has_symlink_component(root, lexical):
            raise ValueError("Scoped original storage directory contains a symlink")
        directory = storage_path(root, f"workspaces/{workspace_id}/documents")
        if not directory.exists():
            return OrphanProbe("absent")
        if not directory.is_dir():
            raise ValueError("Scoped original storage root is not a directory")
        entry = directory
        for _depth in range(16):
            if entry.is_symlink() or entry.is_junction():
                raise ValueError("Scoped original storage contains a symlink")
            if not entry.is_dir():
                break
            with os.scandir(entry) as entries:
                child = next(entries, None)
            if child is None:
                entry.rmdir()
                return OrphanProbe("pruned_root" if entry == directory else "pruned_subdir")
            entry = Path(child.path)
        else:
            raise ValueError("Scoped original storage exceeds the supported directory depth")
        if entry.is_symlink() or entry.is_junction():
            raise ValueError("Scoped original storage contains a symlink")
        if not entry.is_file():
            raise ValueError("Scoped original storage contains an unsupported entry")
        return OrphanProbe("file", entry.relative_to(root).as_posix())
