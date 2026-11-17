"""Contract tests every ``StorageBackend`` implementation must satisfy (local filesystem and S3 via moto)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from core.storage import (
    LocalFilesystemBackend,
    StorageBackend,
    WorkspaceStorageBusy,
    build_document_key,
    factory,
    get_storage_backend,
)

WORKSPACE = UUID(int=7)


@pytest.fixture(params=["local", "s3"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> StorageBackend:
    """Build each backend in isolation: local under a temp dir, S3 on a fresh moto bucket with an in-memory fence."""
    if request.param == "local":
        return LocalFilesystemBackend(tmp_path)
    return cast(StorageBackend, request.getfixturevalue("make_s3")())


async def _chunks(*blocks: bytes) -> AsyncIterator[bytes]:
    """Yield the given blocks as an async stream."""
    for block in blocks:
        yield block


async def _collect(stream: AsyncIterator[bytes]) -> bytes:
    """Concatenate an async byte stream."""
    return b"".join([block async for block in stream])


def _key(document: UUID | None = None, suffix: str = ".txt") -> str:
    """Return a canonical document key in the test workspace."""
    return build_document_key(WORKSPACE, document or uuid4(), suffix)


@pytest.mark.asyncio
async def test_put_stream_roundtrip_reports_size_and_sha256(backend: StorageBackend) -> None:
    key = _key()
    stored = await backend.put_stream(
        key, _chunks(b"hello ", b"world"), 100, workspace_id=WORKSPACE
    )
    assert (stored.key, stored.size) == (key, 11)
    assert stored.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert await backend.exists(key)
    assert await _collect(backend.open_stream(key)) == b"hello world"
    async with backend.download_to_temp(key) as path:
        assert path.read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_put_stream_rejects_empty_and_oversized_and_leaves_nothing(
    backend: StorageBackend,
) -> None:
    key = _key()
    with pytest.raises(ValueError, match="empty"):
        await backend.put_stream(key, _chunks(), 10, workspace_id=WORKSPACE)
    with pytest.raises(ValueError, match="size limit"):
        await backend.put_stream(key, _chunks(b"x" * 11), 10, workspace_id=WORKSPACE)
    with pytest.raises(ValueError, match="byte limit"):
        await backend.put_stream(key, _chunks(b"x"), 0, workspace_id=WORKSPACE)
    assert not await backend.exists(key)
    assert await backend.list_prefix("workspaces") == []


@pytest.mark.asyncio
async def test_delete_is_idempotent_unless_missing_not_ok(backend: StorageBackend) -> None:
    key = _key()
    await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    await backend.delete(key)
    assert not await backend.exists(key)
    await backend.delete(key)
    with pytest.raises(FileNotFoundError):
        await backend.delete(key, missing_ok=False)


@pytest.mark.asyncio
async def test_list_prefix_returns_sorted_keys_under_prefix(backend: StorageBackend) -> None:
    keys = sorted(_key() for _ in range(3))
    for key in keys:
        await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    assert await backend.list_prefix(f"workspaces/{WORKSPACE}/documents") == keys
    assert await backend.list_prefix(f"workspaces/{UUID(int=8)}") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../escape", "/abs/path", "a/../../b"])
async def test_invalid_keys_are_rejected_with_value_error(
    backend: StorageBackend, bad: str
) -> None:
    with pytest.raises(ValueError, match="Invalid storage path"):
        await backend.exists(bad)
    with pytest.raises(ValueError, match="Invalid storage path"):
        await backend.delete(bad)


def test_build_document_key_validates_identity_and_suffix() -> None:
    document = uuid4()
    assert (
        build_document_key(WORKSPACE, document, ".pdf")
        == f"workspaces/{WORKSPACE}/documents/{document}/{document}.pdf"
    )
    with pytest.raises(ValueError, match="UUIDs"):
        build_document_key("x", document, ".pdf")  # type: ignore[arg-type]
    for suffix in ("pdf", ".a/b", ".a\\b", ".a:b", ".a\x00"):
        with pytest.raises(ValueError, match="suffix"):
            build_document_key(WORKSPACE, document, suffix)


@pytest.mark.asyncio
async def test_fence_blocks_second_writer_and_erased_workspace(backend: StorageBackend) -> None:
    async with backend.fence.ahold(WORKSPACE):
        with pytest.raises(WorkspaceStorageBusy):
            async with backend.fence.ahold(WORKSPACE, erase=True):
                pytest.fail("erasure overtook a live writer")
    async with backend.fence.ahold(WORKSPACE, erase=True):
        pass
    with pytest.raises(WorkspaceStorageBusy, match="erased"):
        await backend.put_stream(_key(), _chunks(b"late"), 10, workspace_id=WORKSPACE)
    assert await backend.list_prefix("workspaces") == []


@pytest.mark.asyncio
async def test_cleanup_orphans_keeps_referenced_and_recent(backend: StorageBackend) -> None:
    kept, orphan = _key(), _key()
    for key in (kept, orphan):
        await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    assert await backend.cleanup_orphans({kept}, 3600) == 0  # inside grace window
    # Windows clock granularity can leave a fresh mtime ahead of ``now``; step past it so grace=0 is deterministic.
    await asyncio.sleep(0.1)
    assert await backend.cleanup_orphans({kept}, 0) == 1
    assert await backend.exists(kept)
    assert not await backend.exists(orphan)


def test_factory_defaults_to_local_and_rejects_unknown(tmp_path: Path) -> None:
    assert isinstance(
        get_storage_backend(SimpleNamespace(data_dir=tmp_path)), LocalFilesystemBackend
    )
    assert isinstance(
        get_storage_backend(SimpleNamespace(data_dir=tmp_path, storage_backend="local")),
        LocalFilesystemBackend,
    )
    with pytest.raises(ValueError, match="Unknown storage backend"):
        get_storage_backend(SimpleNamespace(data_dir=tmp_path, storage_backend="nope"))


def test_registry_allows_plugging_another_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = LocalFilesystemBackend(tmp_path)
    monkeypatch.setitem(factory._BUILDERS, "memory-test", lambda _settings: sentinel)
    assert (
        get_storage_backend(SimpleNamespace(data_dir=tmp_path, storage_backend="memory-test"))
        is sentinel
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        lambda: build_document_key(UUID(int=8), uuid4(), ".txt"),  # foreign workspace
        lambda: ".storage-fences/evil",
        lambda: f"workspaces/{WORKSPACE}/documents/x.txt",
        lambda: f"workspaces/{WORKSPACE}/documents/{uuid4()}/other.txt",
        lambda: f"workspaces/{WORKSPACE}/documents/{uuid4()}/../x",
    ],
)
async def test_put_stream_rejects_non_canonical_or_foreign_keys(
    backend: StorageBackend, bad: Callable[[], str]
) -> None:
    with pytest.raises(ValueError, match="Invalid storage path"):
        await backend.put_stream(bad(), _chunks(b"x"), 10, workspace_id=WORKSPACE)
    assert await backend.list_prefix("") == []


@pytest.mark.asyncio
async def test_delete_not_missing_ok_raises_when_absent(backend: StorageBackend) -> None:
    with pytest.raises(FileNotFoundError):
        await backend.delete(_key(), missing_ok=False)


@pytest.mark.asyncio
async def test_orphan_probe_states_and_file_response(backend: StorageBackend) -> None:
    assert (await backend.aprobe_workspace_orphan(WORKSPACE)).state == "absent"
    key = _key()
    await backend.put_stream(key, _chunks(b"abc"), 10, workspace_id=WORKSPACE)
    probe = await backend.aprobe_workspace_orphan(WORKSPACE)
    assert (probe.state, probe.key) == ("file", key)
    backend.check_erasable(key, WORKSPACE)
    with pytest.raises(ValueError, match="outside the account"):
        backend.check_erasable(key, UUID(int=9))
    for response in (
        backend.file_response(key, media_type="text/plain", filename="a.txt"),
        await backend.afile_response(key, media_type="text/plain", filename="a.txt"),
    ):
        assert response.media_type == "text/plain"
        assert "a.txt" in response.headers["content-disposition"]
    await backend.delete(key)
    if isinstance(backend, LocalFilesystemBackend):
        # erasure.py relies on the local bounded walk: one empty-directory prune per probe, then "absent".
        states = [(await backend.aprobe_workspace_orphan(WORKSPACE)).state for _ in range(3)]
        assert states == ["pruned_subdir", "pruned_root", "absent"]
    else:
        # Remote stores have no directories to prune and answer "absent" at once.
        assert (await backend.aprobe_workspace_orphan(WORKSPACE)).state == "absent"
