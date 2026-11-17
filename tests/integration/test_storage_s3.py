"""S3 storage backend against a real S3-compatible server (RustFS) and the real PostgreSQL fence.

Skipped unless the disposable harness provides ``S3_TEST_ENDPOINT_URL`` (compose profile ``s3`` in
docker-compose.test.yml, published on 127.0.0.1:39000 with ``S3_TEST_ACCESS_KEY`` / ``S3_TEST_SECRET_KEY``
equal to the RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY there). The PostgreSQL fence tests additionally need
``TEST_DATABASE_URL`` (migrated ``bbd_test``, including ``0016_workspace_storage_state``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import aioboto3
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.storage import WorkspaceStorageBusy, build_document_key
from core.storage.pg_fence import PostgresStorageFence, fence_engine
from core.storage.s3 import S3Config, S3StorageBackend

pytestmark = pytest.mark.integration

MIB = 1024 * 1024


async def _chunks(*blocks: bytes) -> AsyncIterator[bytes]:
    """Yield the given blocks as an async stream."""
    for block in blocks:
        yield block


@pytest.fixture
async def s3_config() -> S3Config:
    """Create a fresh bucket on the RustFS test service and return a config bound to it (skips when unconfigured)."""
    endpoint = os.getenv("S3_TEST_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("S3_TEST_ENDPOINT_URL is available only when the s3 compose profile runs")
    key, secret = os.getenv("S3_TEST_ACCESS_KEY", ""), os.getenv("S3_TEST_SECRET_KEY", "")
    bucket = f"it-{uuid4().hex[:20]}"
    async with aioboto3.Session().client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
    ) as client:
        await client.create_bucket(Bucket=bucket)
    return S3Config(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=key,
        secret_access_key=secret,
        part_size_bytes=5 * MIB,
    )


@pytest.fixture
def pg_fence() -> PostgresStorageFence:
    """Return the real PostgreSQL fence on the disposable database (skips without TEST_DATABASE_URL)."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is available only in the disposable test runner")
    return PostgresStorageFence(fence_engine(url))


async def test_multipart_roundtrip_conditional_create_and_listing(
    s3_config: S3Config,
    pg_fence: PostgresStorageFence,
) -> None:
    backend = S3StorageBackend(s3_config, pg_fence)
    workspace = uuid4()
    key = build_document_key(workspace, uuid4(), ".bin")
    payload = b"a" * (5 * MIB) + b"b" * 1234
    stored = await backend.put_stream(
        key,
        _chunks(payload[: 3 * MIB], payload[3 * MIB :]),
        20 * MIB,
        workspace_id=workspace,
    )
    assert stored.size == len(payload)
    assert b"".join([block async for block in backend.open_stream(key)]) == payload
    with pytest.raises(FileExistsError):
        await backend.put_stream(key, _chunks(b"again"), 100, workspace_id=workspace)
    assert await backend.list_prefix(f"workspaces/{workspace}") == [key]
    async with backend.download_to_temp(key) as path:
        assert path.read_bytes() == payload
    assert not path.exists()
    await backend.delete(key, missing_ok=False)
    assert not await backend.exists(key)


async def test_presigned_download_forces_attachment(
    s3_config: S3Config, pg_fence: PostgresStorageFence
) -> None:
    backend = S3StorageBackend(replace(s3_config, presigned_downloads=True), pg_fence)
    workspace = uuid4()
    key = build_document_key(workspace, uuid4(), ".html")
    await backend.put_stream(key, _chunks(b"<b>x</b>"), 100, workspace_id=workspace)
    redirect = await backend.afile_response(key, media_type="text/plain", filename="x.html")
    async with httpx.AsyncClient() as client:
        fetched = await client.get(redirect.headers["location"])
    assert fetched.headers["content-disposition"] == 'attachment; filename="x.html"'
    assert fetched.headers["content-type"].startswith("text/plain")


async def test_postgres_fence_excludes_writers_and_erasure_flag_is_permanent(
    pg_fence: PostgresStorageFence,
) -> None:
    workspace = uuid4()
    async with pg_fence.ahold(workspace):
        with pytest.raises(WorkspaceStorageBusy):
            async with pg_fence.ahold(workspace, erase=True):
                pytest.fail("erasure overtook a live writer")
    async with pg_fence.ahold(workspace, erase=True):
        pass
    with pytest.raises(WorkspaceStorageBusy, match="erased"):
        async with pg_fence.ahold(workspace):
            pytest.fail("delayed upload accepted after erasure")
    # The database trigger makes the flag write-once and the row permanent.
    for statement in (
        "UPDATE workspace_storage_state SET deleted_at = NULL WHERE workspace_id = :ws",
        "DELETE FROM workspace_storage_state WHERE workspace_id = :ws",
    ):
        with pytest.raises(DBAPIError):
            async with pg_fence._engine.begin() as connection:
                await connection.execute(text(statement), {"ws": workspace})
