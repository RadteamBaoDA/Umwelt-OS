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


async def save_upload(root: Path, upload: UploadFile, document_id: UUID, suffix: str, max_bytes: int) -> tuple[str, int, str]:
    """Stream an upload to a temporary file with a byte limit and SHA-256 digest, then atomically publish it; remove temporary data on failure."""
    relative = Path("documents") / str(document_id) / f"{document_id}{suffix}"
    destination = storage_path(root, relative.as_posix())
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
        os.replace(temporary_name, destination)
        return relative.as_posix(), size, digest.hexdigest()
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def cleanup_orphaned_files(root: Path, referenced: set[str], grace_seconds: int) -> int:
    """Remove unreferenced document files older than the grace period and return the deletion count."""
    if not root.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - grace_seconds
    removed = 0
    for path in root.glob("documents/**/*"):
        if not path.is_file() or path.stat().st_mtime > cutoff:
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in referenced:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
