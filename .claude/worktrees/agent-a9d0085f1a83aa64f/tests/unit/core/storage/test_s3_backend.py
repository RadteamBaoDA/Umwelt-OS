"""S3-specific behaviour (against an offline moto server): multipart, conditional create, SSE, presign, sweeps."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from core.storage import WorkspaceStorageBusy, build_document_key, get_storage_backend
from core.storage.s3 import S3StorageBackend, attachment_disposition, checked_key

WORKSPACE = UUID(int=11)
MIB = 1024 * 1024


async def _chunks(*blocks: bytes) -> AsyncIterator[bytes]:
    """Yield the given blocks as an async stream."""
    for block in blocks:
        yield block


def _key(suffix: str = ".bin") -> str:
    """Return a fresh canonical key in the test workspace."""
    return build_document_key(WORKSPACE, uuid4(), suffix)


async def _raw(backend: S3StorageBackend, op: str, **kwargs: Any) -> Any:
    """Call a raw S3 operation on the backend's bucket (test inspection helper)."""
    async with backend._client() as client:
        return await getattr(client, op)(Bucket=backend._config.bucket, **kwargs)


async def test_large_object_uses_multipart_and_reports_exact_sha256(make_s3: Any) -> None:
    backend = make_s3(part_size_bytes=5 * MIB)
    payload = b"a" * (5 * MIB) + b"b" * (5 * MIB) + b"tail"
    key = _key()
    stored = await backend.put_stream(
        key,
        _chunks(*(payload[i : i + MIB] for i in range(0, len(payload), MIB))),
        20 * MIB,
        workspace_id=WORKSPACE,
    )
    assert (stored.size, stored.sha256) == (len(payload), hashlib.sha256(payload).hexdigest())
    head = await _raw(backend, "head_object", Key=key)
    assert head["ContentLength"] == len(payload)
    assert (await _raw(backend, "list_multipart_uploads")).get("Uploads", []) == []


async def test_oversize_mid_multipart_aborts_without_leaving_parts_or_object(make_s3: Any) -> None:
    backend = make_s3(part_size_bytes=5 * MIB)
    key = _key()
    with pytest.raises(ValueError, match="size limit"):
        await backend.put_stream(
            key, _chunks(b"x" * (6 * MIB), b"y" * (6 * MIB)), 8 * MIB, workspace_id=WORKSPACE
        )
    assert not await backend.exists(key)
    assert (await _raw(backend, "list_multipart_uploads")).get("Uploads", []) == []


async def test_stream_failure_after_first_part_aborts_upload(make_s3: Any) -> None:
    backend = make_s3(part_size_bytes=5 * MIB)

    async def failing() -> AsyncIterator[bytes]:
        """Yield one full part then fail like a dropped client connection."""
        yield b"x" * (6 * MIB)
        raise ConnectionError("client went away")

    with pytest.raises(ConnectionError):
        await backend.put_stream(_key(), failing(), 20 * MIB, workspace_id=WORKSPACE)
    assert (await _raw(backend, "list_multipart_uploads")).get("Uploads", []) == []
    assert await backend.list_prefix("") == []


async def test_existing_object_is_never_overwritten(make_s3: Any) -> None:
    backend = make_s3()
    key = _key()
    await backend.put_stream(key, _chunks(b"first"), 100, workspace_id=WORKSPACE)
    with pytest.raises(FileExistsError):
        await backend.put_stream(key, _chunks(b"second"), 100, workspace_id=WORKSPACE)
    assert b"".join([b async for b in backend.open_stream(key)]) == b"first"


async def test_staging_mode_publishes_via_copy_cleans_staging_and_never_overwrites(
    make_s3: Any,
) -> None:
    backend = make_s3(conditional_put=False)
    key = _key()
    await backend.put_stream(key, _chunks(b"first"), 100, workspace_id=WORKSPACE)
    assert await backend.list_prefix("staging") == []
    with pytest.raises(FileExistsError):
        await backend.put_stream(key, _chunks(b"second"), 100, workspace_id=WORKSPACE)
    assert await backend.list_prefix("staging") == []
    assert b"".join([b async for b in backend.open_stream(key)]) == b"first"


async def test_prefix_and_server_side_encryption_are_applied(make_s3: Any) -> None:
    backend = make_s3(prefix="umwelt/", sse="AES256")
    key = _key()
    await backend.put_stream(key, _chunks(b"data"), 100, workspace_id=WORKSPACE)
    listed = await _raw(backend, "list_objects_v2")
    assert [item["Key"] for item in listed["Contents"]] == [f"umwelt/{key}"]
    assert (await _raw(backend, "head_object", Key=f"umwelt/{key}"))[
        "ServerSideEncryption"
    ] == "AES256"
    assert await backend.list_prefix(f"workspaces/{WORKSPACE}") == [key]


async def test_list_prefix_requires_directory_boundary(make_s3: Any) -> None:
    backend = make_s3()
    other = UUID(int=0x110)
    await backend.put_stream(_key(), _chunks(b"a"), 10, workspace_id=WORKSPACE)
    await backend.put_stream(
        build_document_key(other, uuid4(), ".bin"), _chunks(b"a"), 10, workspace_id=other
    )
    assert len(await backend.list_prefix(f"workspaces/{WORKSPACE}")) == 1
    assert len(await backend.list_prefix("workspaces")) == 2


async def test_listing_is_paginated(make_s3: Any) -> None:
    backend = make_s3()
    keys = sorted(_key() for _ in range(5))
    for key in keys:
        await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    pages = [item async for item in backend._iter_objects("workspaces", page_size=2)]
    assert sorted(key for key, _modified in pages) == keys
    assert (await backend.aprobe_workspace_orphan(WORKSPACE)).key == keys[0]


async def test_download_to_temp_is_bounded_and_always_cleaned(make_s3: Any) -> None:
    backend = make_s3(max_download_bytes=10)
    small, big = _key(".pdf"), _key(".pdf")
    await backend.put_stream(small, _chunks(b"0123456789"), 100, workspace_id=WORKSPACE)
    await backend.put_stream(big, _chunks(b"0123456789A"), 100, workspace_id=WORKSPACE)
    async with backend.download_to_temp(small) as path:
        assert path.suffix == ".pdf"
        assert path.read_bytes() == b"0123456789"
    assert not path.exists()
    with pytest.raises(ValueError, match="download limit"):
        async with backend.download_to_temp(big):
            pytest.fail("oversize object must not be handed to a parser")
    with pytest.raises(RuntimeError, match="boom"):
        async with backend.download_to_temp(small) as failing:
            raise RuntimeError("boom")
    assert not failing.exists()
    with pytest.raises(FileNotFoundError):
        async with backend.download_to_temp(_key()):
            pytest.fail("missing object")


async def test_default_download_streams_through_api_with_length_and_attachment(
    make_s3: Any,
) -> None:
    backend = make_s3()
    key = _key(".txt")
    await backend.put_stream(key, _chunks(b"hello"), 100, workspace_id=WORKSPACE)
    response = await backend.afile_response(key, media_type="text/plain", filename="a b.txt")
    assert response.status_code == 200
    assert response.headers["content-length"] == "5"
    assert response.headers["content-disposition"].startswith("attachment;")
    with pytest.raises(FileNotFoundError):
        await backend.afile_response(_key(), media_type="text/plain", filename="x")


async def test_presigned_download_is_short_lived_attachment_with_forced_type(make_s3: Any) -> None:
    backend = make_s3(
        presigned_downloads=True, presign_ttl_seconds=3600
    )  # over-long TTL is clamped to 60 s
    key = _key(".html")
    await backend.put_stream(
        key, _chunks(b"<script>alert(1)</script>"), 100, workspace_id=WORKSPACE
    )
    response = await backend.afile_response(key, media_type="text/plain", filename="evil.html")
    assert response.status_code == 307
    url = response.headers["location"]
    query = parse_qs(urlparse(url).query)
    assert query["response-content-disposition"] == ['attachment; filename="evil.html"']
    assert query["response-content-type"] == ["text/plain"]
    assert int(query["X-Amz-Expires"][0]) <= 60
    assert response.headers["referrer-policy"] == "no-referrer"
    async with httpx.AsyncClient() as client:
        fetched = await client.get(url)
    assert fetched.headers["content-disposition"] == 'attachment; filename="evil.html"'
    assert fetched.headers["content-type"].startswith("text/plain")


async def test_orphan_sweep_removes_old_unreferenced_uploads_and_stale_staging_only(
    make_s3: Any,
) -> None:
    backend = make_s3()
    kept, orphan = _key(), _key()
    for key in (kept, orphan):
        await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    await _raw(backend, "put_object", Key=f"staging/{WORKSPACE}/abc", Body=b"s")
    await _raw(backend, "put_object", Key=f"workspaces/{WORKSPACE}/notes.txt", Body=b"unrelated")
    assert await backend.cleanup_orphans({kept}, 3600) == 0
    assert await backend.cleanup_orphans({kept}, 0) == 2  # orphan upload + staging object
    assert await backend.exists(kept)
    assert not await backend.exists(orphan)
    assert await backend.exists(f"workspaces/{WORKSPACE}/notes.txt")
    with pytest.raises(ValueError, match="nonnegative"):
        await backend.cleanup_orphans(set(), -1)


async def test_delete_missing_not_ok_confirms_and_check_erasable(make_s3: Any) -> None:
    backend = make_s3()
    key = _key()
    await backend.put_stream(key, _chunks(b"a"), 10, workspace_id=WORKSPACE)
    await backend.delete(key, missing_ok=False)
    with pytest.raises(FileNotFoundError):
        await backend.delete(key, missing_ok=False)
    backend.check_erasable(key, WORKSPACE)
    backend.check_erasable("documents/x/y.txt", WORKSPACE)
    with pytest.raises(ValueError, match="outside the account"):
        backend.check_erasable(key, UUID(int=12))
    with pytest.raises(NotImplementedError):
        backend.local_path(key)
    with pytest.raises(NotImplementedError):
        backend.probe_workspace_orphan(WORKSPACE)


def test_checked_key_rejects_traversal_and_odd_forms() -> None:
    for bad in ("../x", "/abs", "a//b", "a/./b", "a/", "a\\b", "C:/x", "a\x00b", ""):
        with pytest.raises(ValueError, match="Invalid storage path"):
            checked_key(bad)
    assert checked_key("", allow_empty=True) == ""


def test_attachment_disposition_quotes_non_ascii() -> None:
    assert attachment_disposition("a.txt") == 'attachment; filename="a.txt"'
    assert attachment_disposition("tài liệu.pdf").startswith("attachment; filename*=utf-8''")


def test_factory_builds_s3_backend_with_postgres_fence(moto_endpoint: str) -> None:
    settings = SimpleNamespace(
        storage_backend="s3",
        s3_bucket="bkt",
        s3_region="us-east-1",
        s3_endpoint_url=moto_endpoint,
        s3_access_key_id=SecretStr("k"),
        s3_secret_access_key=SecretStr("s"),
        s3_prefix="/pfx/",
        s3_sse="aws:kms",
        s3_sse_kms_key_id="key-1",
        s3_path_style=True,
        s3_conditional_put=True,
        s3_part_size_bytes=5 * MIB,
        storage_presigned_downloads=False,
        storage_presigned_ttl_seconds=60,
        s3_presign_endpoint_url=None,
        upload_max_bytes=123,
        database_url="postgresql+asyncpg://u:p@localhost:1/db",
    )
    backend = get_storage_backend(settings)
    assert isinstance(backend, S3StorageBackend)
    assert type(backend.fence).__name__ == "PostgresStorageFence"
    assert backend._config.prefix == "pfx/" and backend._config.max_download_bytes == 123
    assert backend._encryption() == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "key-1"}


async def test_concurrent_writers_to_one_workspace_second_gets_busy(make_s3: Any) -> None:
    backend = make_s3()
    gate = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        """Hold the fence open until released."""
        yield b"a"
        await gate.wait()

    first = asyncio.create_task(backend.put_stream(_key(), slow(), 10, workspace_id=WORKSPACE))
    await asyncio.sleep(0.2)
    with pytest.raises(WorkspaceStorageBusy):
        await backend.put_stream(_key(), _chunks(b"b"), 10, workspace_id=WORKSPACE)
    gate.set()
    await first
