from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from fastapi import UploadFile


def storage_path(root: Path, relative_path: str) -> Path:
    """Resolve a storage-relative path beneath the configured root and reject traversal or absolute paths."""
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError("Invalid storage path")
    base = root.resolve()
    path = (base / candidate).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Invalid storage path")
    return path


def _write_block(temporary: BinaryIO, digest: hashlib._Hash, block: bytes) -> None:
    temporary.write(block)
    digest.update(block)


def _sync(temporary: BinaryIO) -> None:
    temporary.flush()
    os.fsync(temporary.fileno())


async def save_upload(
    root: Path, upload: UploadFile, document_id: UUID, suffix: str, max_bytes: int, *, workspace_id: UUID,
) -> tuple[str, int, str]:
    """Publish bounded upload bytes atomically under the caller's admitted workspace.

    Pure filesystem I/O: the caller supplies the actual authorized workspace and releases
    SQL locks before streaming. Return relative URI, byte count and SHA-256; reject invalid
    identity/suffix, empty/oversized uploads, traversal and symlinks in the publication path.
    Temporary bytes are removed on failure or cancellation; existing legacy URIs are untouched.
    """
    if not isinstance(workspace_id, UUID) or not isinstance(document_id, UUID):
        raise ValueError("Valid workspace and document UUIDs are required")
    if type(max_bytes) is not int or max_bytes <= 0 or not suffix.startswith(".") or any(char in suffix for char in ("/", "\\", ":", "\x00")):
        raise ValueError("Invalid upload suffix or byte limit")
    relative = Path("workspaces") / str(workspace_id) / "documents" / str(document_id) / f"{document_id}{suffix}"
    destination = storage_path(root, relative.as_posix())
    # Check the lexical path as well: resolve() alone would hide in-root symlink redirects.
    lexical = root.resolve() / relative
    if any(parent.is_symlink() for parent in (lexical, *lexical.parents) if parent.is_relative_to(root.resolve())):
        raise ValueError("Upload path cannot contain symlinks")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as temporary:
            while block := await upload.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise ValueError("Upload exceeds the configured size limit")
                await asyncio.to_thread(_write_block, temporary, digest, block)
            await asyncio.to_thread(_sync, temporary)
        if size == 0:
            raise ValueError("Uploaded file is empty")
        if storage_path(root, relative.as_posix()) != destination or any(
            parent.is_symlink() for parent in (lexical, *lexical.parents) if parent.is_relative_to(root.resolve())
        ):
            raise ValueError("Upload path changed during publication")
        os.replace(temporary_name, destination)
        return relative.as_posix(), size, digest.hexdigest()
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


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
