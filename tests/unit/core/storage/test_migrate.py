"""Local -> S3 migration tool: copy, checksum verification, idempotent resume, dry run and failure handling."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.storage import LocalFilesystemBackend, build_document_key
from core.storage.migrate import migrate_objects

WORKSPACE = UUID(int=21)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    """Yield ``data`` as one block."""
    yield data


async def _seed(local: LocalFilesystemBackend, payloads: list[bytes]) -> list[str]:
    """Store each payload locally under a fresh canonical key and return the keys."""
    keys = [build_document_key(WORKSPACE, uuid4(), ".bin") for _ in payloads]
    for key, data in zip(keys, payloads, strict=True):
        await local.put_stream(key, _chunks(data), 1024, workspace_id=WORKSPACE)
    return keys


async def test_copy_verifies_checksums_and_second_run_is_a_noop(tmp_path: Path, make_s3: Any) -> None:
    local, s3 = LocalFilesystemBackend(tmp_path), make_s3()
    payloads = [b"one", b"two" * 100]
    keys = await _seed(local, payloads)
    report = await migrate_objects(local, s3, max_bytes=1024)
    assert (report.copied, report.failures, report.ok) == (2, [], True)
    for key, data in zip(keys, payloads, strict=True):
        copied = b"".join([block async for block in s3.open_stream(key)])
        assert hashlib.sha256(copied).hexdigest() == hashlib.sha256(data).hexdigest()
    again = await migrate_objects(local, s3, max_bytes=1024)
    assert (again.copied, again.already_present, again.ok) == (0, 2, True)


async def test_dry_run_writes_nothing(tmp_path: Path, make_s3: Any) -> None:
    local, s3 = LocalFilesystemBackend(tmp_path), make_s3()
    await _seed(local, [b"abc", b"defg"])
    report = await migrate_objects(local, s3, max_bytes=1024, dry_run=True)
    assert (report.would_copy, report.copied, report.bytes_copied) == (2, 0, 7)
    assert await s3.list_prefix("") == []


async def test_different_existing_destination_is_a_failure_not_an_overwrite(tmp_path: Path, make_s3: Any) -> None:
    local, s3 = LocalFilesystemBackend(tmp_path), make_s3()
    (key,) = await _seed(local, [b"source"])
    await s3.put_stream(key, _chunks(b"other"), 100, workspace_id=WORKSPACE)
    report = await migrate_objects(local, s3, max_bytes=1024)
    assert not report.ok and "different content" in report.failures[0]
    assert b"".join([block async for block in s3.open_stream(key)]) == b"other"


async def test_oversize_and_non_canonical_keys_are_reported_not_fatal(tmp_path: Path, make_s3: Any) -> None:
    local, s3 = LocalFilesystemBackend(tmp_path), make_s3()
    await _seed(local, [b"x" * 500, b"ok"])
    legacy = tmp_path / "documents" / str(uuid4())
    legacy.mkdir(parents=True)
    (legacy / "legacy.txt").write_bytes(b"old")
    report = await migrate_objects(local, s3, max_bytes=100)
    assert report.copied == 1 and len(report.failures) == 1 and report.skipped_other == 1 and not report.ok
    assert len(await s3.list_prefix("workspaces")) == 1


async def test_erased_workspace_is_not_resurrected_on_destination(tmp_path: Path, make_s3: Any, memory_fence: Any) -> None:
    local = LocalFilesystemBackend(tmp_path)
    fence = memory_fence
    fence.erased.add(WORKSPACE)
    await _seed(local, [b"data"])
    report = await migrate_objects(local, make_s3(fence), max_bytes=1024)
    assert report.copied == 0 and "erased" in report.failures[0]
