"""Storage abstraction for original uploads: backend and fence contracts.

``StorageBackend`` is the only interface feature code uses to publish, read, delete and enumerate
original files; ``StorageFence`` is the physical writer/erasure fence that complements the SQL
workspace lease. The local filesystem implementation lives in ``core.storage.local``; another backend
(for example S3) can be registered with ``core.storage.factory.register_storage_backend`` without
editing callers. Keys are storage-relative POSIX strings such as
``workspaces/<workspace>/documents/<document>/<document><suffix>`` and are persisted verbatim as raw URIs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from fastapi import Response

UPLOAD_READ_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    """Result of a successful ``put_stream``: storage key, byte count and SHA-256 hex digest."""

    key: str
    size: int
    sha256: str


@dataclass(frozen=True)
class OrphanProbe:
    """One bounded step of the account-erasure walk over a workspace's original-file tree.

    ``state`` is ``absent`` (nothing stored), ``pruned_root`` (the now-empty workspace root was
    removed: finished), ``pruned_subdir`` (an empty nested directory was removed: call again) or
    ``file`` (``key`` names one remaining object the caller must authorize and delete).
    """

    state: Literal["absent", "pruned_root", "pruned_subdir", "file"]
    key: str | None = None


class UploadSource(Protocol):
    """Anything with ``await read(n)`` returning bytes (``starlette.UploadFile`` satisfies it)."""

    async def read(self, size: int = -1, /) -> bytes:
        """Return up to ``size`` bytes; an empty bytes object signals end of stream."""
        ...


def require_document_key(workspace_id: UUID, key: str) -> None:
    """Raise ``ValueError('Invalid storage path')`` unless ``key`` is a canonical document key of ``workspace_id``.

    Canonical means exactly ``workspaces/<workspace_id>/documents/<uuid>/<uuid><.ext>``; this ties a
    write to the workspace whose fence is held, so foreign workspaces and fence markers are unreachable.
    """
    parts = key.split("/")
    try:
        valid = (
            len(parts) == 5
            and parts[0] == "workspaces"
            and parts[1] == str(workspace_id)
            and parts[2] == "documents"
            and str(UUID(parts[3])) == parts[3]
            and parts[4].startswith(parts[3])
            and build_document_key(workspace_id, UUID(parts[3]), parts[4][len(parts[3]) :]) == key
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid storage path")


def build_document_key(workspace_id: UUID, document_id: UUID, suffix: str) -> str:
    """Return the canonical key ``workspaces/<ws>/documents/<doc>/<doc><suffix>``.

    Raises ``ValueError`` when either identity is not a UUID or the suffix is not a plain ``.ext``
    (no separators, colon or NUL). Pure: touches no storage.
    """
    if not isinstance(workspace_id, UUID) or not isinstance(document_id, UUID):
        raise ValueError("Valid workspace and document UUIDs are required")
    if not suffix.startswith(".") or any(char in suffix for char in ("/", "\\", ":", "\x00")):
        raise ValueError("Invalid upload suffix or byte limit")
    return (
        Path("workspaces")
        / str(workspace_id)
        / "documents"
        / str(document_id)
        / f"{document_id}{suffix}"
    ).as_posix()


async def iter_upload(
    upload: UploadSource, block_bytes: int = UPLOAD_READ_BLOCK_BYTES
) -> AsyncIterator[bytes]:
    """Yield an upload's bytes in ``block_bytes`` blocks until it reports end of stream.

    Reads lazily, one block per iteration, so the consumer controls memory and cancellation points.
    """
    while block := await upload.read(block_bytes):
        yield block


@runtime_checkable
class StorageFence(Protocol):
    """Physical fence serializing workspace uploads against account erasure.

    Independent of database connections: an implementation must make a stale, SQL-admitted upload
    unable to create content after erasure, and an erasure unable to start under a live writer.
    """

    def hold(self, workspace_id: UUID, *, erase: bool = False) -> AbstractContextManager[None]:
        """Hold the workspace fence; ``erase=True`` additionally records permanent deletion.

        Raises ``WorkspaceStorageBusy`` when a writer owns the fence or the workspace was erased
        (for non-erase callers); fails closed on invalid markers or storage errors.
        """
        ...

    def ahold(
        self, workspace_id: UUID, *, erase: bool = False
    ) -> AbstractAsyncContextManager[None]:
        """Async twin of ``hold`` with identical semantics, usable by fences that need network I/O.

        Remote fences (``PostgresStorageFence``) implement only this form; ``hold`` then raises
        ``NotImplementedError``. Feature code should always prefer ``ahold``.
        """
        ...


@runtime_checkable
class StorageBackend(Protocol):
    """Async contract for original-file storage; all keys are validated against traversal."""

    @property
    def fence(self) -> StorageFence:
        """Return the physical writer/erasure fence for this backend."""
        ...

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
        *,
        workspace_id: UUID,
    ) -> StoredObject:
        """Atomically publish ``chunks`` at ``key`` under the workspace's upload fence.

        ``key`` MUST be the canonical ``build_document_key(workspace_id, ...)`` key of the fenced
        workspace (``require_document_key``); any other key raises ``ValueError('Invalid storage path')``
        before the fence is taken, so a write can never land in another workspace or a fence marker.
        Rejects empty or oversized content and unsafe paths with ``ValueError``; busy/erased
        workspaces raise ``WorkspaceStorageBusy``. No partial object remains on failure.
        """
        ...

    def open_stream(self, key: str) -> AsyncIterator[bytes]:
        """Return an async iterator over the object's bytes (``ValueError`` for invalid keys)."""
        ...

    def local_path(self, key: str) -> Path:
        """Return the on-disk path of ``key`` (validated); remote backends raise ``NotImplementedError``."""
        ...

    def download_to_temp(self, key: str) -> AbstractAsyncContextManager[Path]:
        """Yield a filesystem path holding the object's bytes for parsers; cleaned on exit if a copy was made."""
        ...

    def file_response(self, key: str, *, media_type: str, filename: str) -> Response:
        """Return a download response for ``key`` (``FileResponse`` semantics for local storage)."""
        ...

    async def afile_response(self, key: str, *, media_type: str, filename: str) -> Response:
        """Async ``file_response``: may consult the store (size, presigned URL) before answering.

        Always an attachment with the stored ``media_type``; callers add cache/nosniff headers after
        authorizing the request. Local storage delegates to ``file_response``.
        """
        ...

    async def delete(self, key: str, *, missing_ok: bool = True) -> None:
        """Remove the object; ``missing_ok=False`` propagates ``FileNotFoundError`` when absent."""
        ...

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` names an existing object (``ValueError`` for invalid keys)."""
        ...

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return sorted keys of regular objects beneath ``prefix`` (symlinks are never followed)."""
        ...

    async def cleanup_orphans(self, referenced: set[str], grace_seconds: int) -> int:
        """Delete old unreferenced upload files in supported layouts; return the number removed."""
        ...

    def check_erasable(self, key: str, workspace_id: UUID) -> None:
        """Raise ``ValueError`` unless ``key`` is a safe original inside this workspace/legacy layouts.

        Synchronous on purpose (A9 local-ordering with SQL under the erasure fence).
        """
        ...

    async def aprobe_workspace_orphan(self, workspace_id: UUID) -> OrphanProbe:
        """Async ``probe_workspace_orphan``; remote backends answer only ``absent`` or ``file``."""
        ...

    def probe_workspace_orphan(self, workspace_id: UUID) -> OrphanProbe:
        """Advance the bounded erasure walk by at most one filesystem mutation (see ``OrphanProbe``).

        Synchronous because of A9 local-ordering constraints; the ``pruned_*`` states describe local
        directory pruning. S-track follow-up: a remote backend (S3) may implement them as no-ops
        (return ``absent``/``file`` only) rather than faking directories or blocking the event loop.
        """
        ...
