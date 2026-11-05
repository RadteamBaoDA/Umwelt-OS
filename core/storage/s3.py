"""S3-compatible (AWS S3, MinIO) implementation of the ``StorageBackend`` contract.

Keys keep the local layout (``workspaces/<ws>/documents/<doc>/<doc><suffix>``) under an optional
bucket prefix, so ``raw_uri`` values are portable between backends. Publication is atomic by
construction: bytes stream through a multipart upload (or a single PUT for small objects) that is
only visible once completed, and the completion is conditional (``If-None-Match: *``) so an existing
object is never overwritten. Stores without conditional writes (``conditional_put=False``) publish via
a ``staging/`` key plus ``CopyObject`` and a pre-copy existence check (best effort: not race free).
The writer/erasure fence is injected; production uses ``PostgresStorageFence`` because S3 has no
file locks. Orphans are found with paginated listings; see docs/operations.md for the bucket
lifecycle rule recommended for the ``staging/`` prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import Response
from fastapi.responses import RedirectResponse, StreamingResponse

from core.heavy_work.slots import to_thread_joined
from core.storage.backend import (
    OrphanProbe,
    StorageFence,
    StoredObject,
    require_document_key,
)

READ_BLOCK_BYTES = 1024 * 1024
MAX_PRESIGN_TTL_SECONDS = 60
STAGING_DIR = "staging"
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict"}
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UPLOAD_KEY = re.compile(rf"^(?:workspaces/{_UUID}/)?documents/({_UUID})/\1\.[^/]+$")


@dataclass(frozen=True)
class S3Config:
    """Connection and behaviour settings of ``S3StorageBackend`` (credentials are plain strings in memory only)."""

    bucket: str
    region: str = "us-east-1"
    endpoint_url: str | None = None
    access_key_id: str = ""
    secret_access_key: str = ""
    prefix: str = ""
    sse: str = ""
    sse_kms_key_id: str = ""
    path_style: bool = True
    conditional_put: bool = True
    part_size_bytes: int = 8 * 1024 * 1024
    presigned_downloads: bool = False
    presign_ttl_seconds: int = MAX_PRESIGN_TTL_SECONDS
    presign_endpoint_url: str | None = None
    max_download_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Any) -> S3Config:
        """Build the config from ``core.config.Settings`` (secrets unwrapped here, never logged)."""
        prefix = str(settings.s3_prefix).strip("/")
        return cls(
            bucket=settings.s3_bucket, region=settings.s3_region, endpoint_url=settings.s3_endpoint_url or None,
            access_key_id=settings.s3_access_key_id.get_secret_value(),
            secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            prefix=f"{prefix}/" if prefix else "", sse=settings.s3_sse, sse_kms_key_id=settings.s3_sse_kms_key_id,
            path_style=settings.s3_path_style, conditional_put=settings.s3_conditional_put,
            part_size_bytes=settings.s3_part_size_bytes, presigned_downloads=settings.storage_presigned_downloads,
            presign_ttl_seconds=settings.storage_presigned_ttl_seconds,
            presign_endpoint_url=settings.s3_presign_endpoint_url or None,
            max_download_bytes=settings.upload_max_bytes,
        )


def checked_key(key: str, *, allow_empty: bool = False) -> str:
    """Return ``key`` if it is a safe storage-relative POSIX key, else raise ``ValueError('Invalid storage path')``.

    Same rules as the local ``storage_path``: no backslash, drive letter, NUL, absolute path or
    ``''``/``.``/``..`` segment, so a key can never address outside its prefix on any backend.
    """
    if key == "" and allow_empty:
        return key
    if "\\" in key or "\x00" in key or (len(key) > 1 and key[:1].isalpha() and key[1] == ":"):
        raise ValueError("Invalid storage path")
    if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("Invalid storage path")
    return key


def _error_code(error: ClientError) -> str:
    """Return the S3 error code (or HTTP status text) carried by ``error``."""
    return str(error.response.get("Error", {}).get("Code", ""))


def _is_missing(error: ClientError) -> bool:
    """Return whether ``error`` means the object or bucket key does not exist."""
    return _error_code(error) in _MISSING_CODES


def attachment_disposition(filename: str) -> str:
    """Return a ``Content-Disposition: attachment`` value for ``filename`` (RFC 6266, as Starlette's FileResponse)."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


class _UploadWriter:
    """Streams bytes into one S3 object: single PUT when small, multipart when larger than a part.

    At most ``part_size`` bytes (plus one input block) are buffered. Nothing is visible at the target
    key until ``finish`` succeeds; ``abort`` discards any multipart upload so no partial object remains.
    """

    def __init__(self, client: Any, bucket: str, key: str, part_size: int, extra: dict[str, str], conditional: bool) -> None:
        """Bind to ``key``; ``extra`` holds the encryption arguments, ``conditional`` adds ``If-None-Match: *``."""
        self._client, self._bucket, self._key = client, bucket, key
        self._part_size, self._extra, self._conditional = part_size, extra, conditional
        self._buffer = bytearray()
        self._upload_id: str | None = None
        self._parts: list[dict[str, Any]] = []

    async def write(self, block: bytes) -> None:
        """Buffer ``block`` and upload every full part, starting the multipart upload on the first one."""
        self._buffer.extend(block)
        while len(self._buffer) >= self._part_size:
            part = bytes(self._buffer[: self._part_size])
            del self._buffer[: self._part_size]
            await self._upload_part(part)

    async def _upload_part(self, part: bytes) -> None:
        """Upload one part, creating the multipart upload lazily so small objects need only one PUT."""
        if self._upload_id is None:
            created = await self._client.create_multipart_upload(Bucket=self._bucket, Key=self._key, **self._extra)
            self._upload_id = created["UploadId"]
        number = len(self._parts) + 1
        response = await self._client.upload_part(
            Bucket=self._bucket, Key=self._key, UploadId=self._upload_id, PartNumber=number, Body=part,
        )
        self._parts.append({"ETag": response["ETag"], "PartNumber": number})

    async def finish(self) -> None:
        """Publish the object: one PUT, or the last part plus ``CompleteMultipartUpload``.

        Raises ``FileExistsError`` when the conditional create finds an existing object.
        """
        condition: dict[str, str] = {"IfNoneMatch": "*"} if self._conditional else {}
        try:
            if self._upload_id is None:
                body = bytes(self._buffer)
                await self._client.put_object(
                    Bucket=self._bucket, Key=self._key, Body=body, ContentLength=len(body), **self._extra, **condition,
                )
                return
            if self._buffer:
                await self._upload_part(bytes(self._buffer))
                self._buffer.clear()
            await self._client.complete_multipart_upload(
                Bucket=self._bucket, Key=self._key, UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts}, **condition,
            )
            self._upload_id = None  # completed: nothing left to abort
        except ClientError as exc:
            if _error_code(exc) in _CONFLICT_CODES:
                raise FileExistsError(self._key) from exc
            raise

    async def abort(self) -> None:
        """Discard an in-flight multipart upload (best effort; lifecycle rules also expire stale ones)."""
        if self._upload_id is not None:
            with contextlib.suppress(ClientError):
                await self._client.abort_multipart_upload(Bucket=self._bucket, Key=self._key, UploadId=self._upload_id)
            self._upload_id = None


class S3StorageBackend:
    """``StorageBackend`` over an S3 bucket; see the module docstring for the publication protocol."""

    def __init__(self, config: S3Config, fence: StorageFence) -> None:
        """Bind to ``config`` and the (injected) workspace ``fence``; no network I/O happens here."""
        self._config = config
        self.fence = fence
        self._session = aioboto3.Session()

    def _client(self, *, endpoint_url: str | None = None) -> AbstractAsyncContextManager[Any]:
        """Return an async context manager yielding an S3 client (one per operation; the backend is stateless)."""
        cfg = self._config
        kwargs: dict[str, Any] = {}
        if cfg.access_key_id:
            kwargs.update(aws_access_key_id=cfg.access_key_id, aws_secret_access_key=cfg.secret_access_key)
        return cast("AbstractAsyncContextManager[Any]", self._session.client(
            "s3", region_name=cfg.region, endpoint_url=endpoint_url or cfg.endpoint_url,
            config=BotoConfig(
                signature_version="s3v4", s3={"addressing_style": "path" if cfg.path_style else "virtual"},
                retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=10, read_timeout=60,
            ), **kwargs,
        ))

    def _full(self, key: str) -> str:
        """Return the bucket key for ``key``: validated and prefixed with the configured bucket prefix."""
        return self._config.prefix + checked_key(key)

    def _encryption(self) -> dict[str, str]:
        """Return the server-side-encryption arguments for writes (empty when SSE is not configured)."""
        if not self._config.sse:
            return {}
        extra = {"ServerSideEncryption": self._config.sse}
        if self._config.sse == "aws:kms" and self._config.sse_kms_key_id:
            extra["SSEKMSKeyId"] = self._config.sse_kms_key_id
        return extra

    async def put_stream(
        self, key: str, chunks: AsyncIterator[bytes], max_bytes: int, *, workspace_id: UUID,
    ) -> StoredObject:
        """Atomically publish ``chunks`` at ``key`` under the workspace fence, never overwriting.

        Validation (limit, canonical workspace key) precedes the fence; the fence is held for the whole
        transfer so an erasure cannot start under a live writer and an admitted-but-delayed upload is
        rejected after erasure. Bytes are hashed and counted while streaming; more than ``max_bytes`` or
        zero bytes raise ``ValueError`` and abort the transfer, leaving no object (a staging object, if
        used, is deleted). An existing object at ``key`` raises ``FileExistsError``. Cancellation aborts the
        multipart upload through a shielded cleanup.
        """
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Invalid upload suffix or byte limit")
        require_document_key(workspace_id, key)
        final_key = self._full(key)
        conditional = self._config.conditional_put
        target = final_key if conditional else f"{self._config.prefix}{STAGING_DIR}/{workspace_id}/{uuid4().hex}"
        async with self.fence.ahold(workspace_id), self._client() as client:
            writer = _UploadWriter(
                client, self._config.bucket, target, self._config.part_size_bytes, self._encryption(), conditional,
            )
            try:
                size, digest = await self._stream_into(writer, chunks, max_bytes)
                await writer.finish()
                if not conditional:
                    await self._copy_into_place(client, target, final_key)
            except BaseException:
                await asyncio.shield(self._discard(client, writer, None if conditional else target))
                raise
            if not conditional:
                with contextlib.suppress(ClientError):
                    await client.delete_object(Bucket=self._config.bucket, Key=target)
        return StoredObject(key, size, digest)

    @staticmethod
    async def _stream_into(writer: _UploadWriter, chunks: AsyncIterator[bytes], max_bytes: int) -> tuple[int, str]:
        """Feed ``chunks`` to ``writer`` enforcing the byte bound; return (size, sha256 hex). Rejects empty input."""
        size = 0
        digest = hashlib.sha256()
        async for block in chunks:
            size += len(block)
            if size > max_bytes:
                raise ValueError("Upload exceeds the configured size limit")
            digest.update(block)
            await writer.write(block)
        if size == 0:
            raise ValueError("Uploaded file is empty")
        return size, digest.hexdigest()

    async def _copy_into_place(self, client: Any, staging_key: str, final_key: str) -> None:
        """Publish a staged object at ``final_key`` without overwriting (best-effort check + CopyObject).

        Used only when conditional writes are unavailable; the existence check and the copy are two
        requests, so the guarantee relies on keys being unique per document (uuid7) rather than on S3.
        """
        bucket = self._config.bucket
        try:
            await client.head_object(Bucket=bucket, Key=final_key)
        except ClientError as exc:
            if not _is_missing(exc):
                raise
        else:
            raise FileExistsError(final_key)
        await client.copy_object(
            Bucket=bucket, Key=final_key, CopySource={"Bucket": bucket, "Key": staging_key}, **self._encryption(),
        )

    async def _discard(self, client: Any, writer: _UploadWriter, staging_key: str | None) -> None:
        """Abort the multipart upload and delete the staging object after a failed publication."""
        await writer.abort()
        if staging_key is not None:
            with contextlib.suppress(ClientError):
                await client.delete_object(Bucket=self._config.bucket, Key=staging_key)

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the object's bytes in 1 MiB blocks straight from the store.

        Raises ``ValueError`` for invalid keys and ``FileNotFoundError`` when absent (at first iteration).
        """
        full = self._full(key)
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._config.bucket, Key=full)
            except ClientError as exc:
                if _is_missing(exc):
                    raise FileNotFoundError(key) from exc
                raise
            body = response["Body"]
            try:
                async for block in body.iter_chunks(READ_BLOCK_BYTES):
                    yield block
            finally:
                body.close()

    def local_path(self, key: str) -> Path:
        """Unsupported: objects have no filesystem path. Use ``download_to_temp``."""
        raise NotImplementedError("S3 objects have no local path; use download_to_temp()")

    @asynccontextmanager
    async def download_to_temp(self, key: str) -> AsyncIterator[Path]:
        """Stream the object into a private temp file for path-based parsers; delete it on exit.

        The copy is bounded by ``max_download_bytes`` (declared length checked first, running total
        enforced while streaming), keeps the key's suffix so parsers can sniff it, and is removed in
        ``finally`` even on error or cancellation. Raises ``FileNotFoundError`` when absent.
        """
        suffix = PurePosixPath(checked_key(key)).suffix
        descriptor, name = tempfile.mkstemp(prefix="umwelt-s3-", suffix=suffix)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                total = 0
                # aclosing releases the HTTP body and client immediately on a size error or cancellation.
                async with contextlib.aclosing(cast(AsyncGenerator[bytes, None], self.open_stream(key))) as stream:
                    async for block in stream:
                        total += len(block)
                        if total > self._config.max_download_bytes:
                            raise ValueError("Stored object exceeds the configured download limit")
                        await to_thread_joined(handle.write, block)
            yield path
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _headers(filename: str) -> dict[str, str]:
        """Return the fixed attachment header for a streamed download."""
        return {"Content-Disposition": attachment_disposition(filename)}

    def file_response(self, key: str, *, media_type: str, filename: str) -> Response:
        """Return a streaming attachment response without a known length (use ``afile_response`` for one)."""
        self._full(key)
        return StreamingResponse(self.open_stream(key), media_type=media_type, headers=self._headers(filename))

    async def afile_response(self, key: str, *, media_type: str, filename: str) -> Response:
        """Return the download response for ``key``, either streamed through the API or a presigned redirect.

        Default: stream through the API with ``Content-Length`` from a HEAD (authorization, nosniff and
        cache headers set by the caller stay on the response). With ``presigned_downloads`` a 307 to a
        <= 60 s presigned GET is returned instead, signing ``response-content-disposition=attachment``
        and ``response-content-type=<stored type>`` so the browser cannot be handed inline/sniffed content;
        the object bytes then bypass API headers (documented trade-off). Raises ``FileNotFoundError`` when absent.
        """
        full = self._full(key)
        disposition = attachment_disposition(filename)
        if self._config.presigned_downloads:
            ttl = max(1, min(self._config.presign_ttl_seconds, MAX_PRESIGN_TTL_SECONDS))
            async with self._client(endpoint_url=self._config.presign_endpoint_url) as client:
                url = await client.generate_presigned_url(
                    "get_object", ExpiresIn=ttl, Params={
                        "Bucket": self._config.bucket, "Key": full,
                        "ResponseContentDisposition": disposition, "ResponseContentType": media_type,
                    },
                )
            return RedirectResponse(url, status_code=307, headers={"Referrer-Policy": "no-referrer"})
        async with self._client() as client:
            try:
                head = await client.head_object(Bucket=self._config.bucket, Key=full)
            except ClientError as exc:
                if _is_missing(exc):
                    raise FileNotFoundError(key) from exc
                raise
        headers = self._headers(filename)
        headers["Content-Length"] = str(head["ContentLength"])
        return StreamingResponse(self.open_stream(key), media_type=media_type, headers=headers)

    async def delete(self, key: str, *, missing_ok: bool = True) -> None:
        """Delete the object. S3 deletes are idempotent, so ``missing_ok=False`` pre-checks with HEAD.

        With ``missing_ok=False`` raises ``FileNotFoundError`` when absent and ``RuntimeError`` if the
        object is still present after the delete (matching the local confirmation semantics).
        """
        full = self._full(key)
        async with self._client() as client:
            if not missing_ok and not await self._head_exists(client, full):
                raise FileNotFoundError(key)
            await client.delete_object(Bucket=self._config.bucket, Key=full)
            if not missing_ok and await self._head_exists(client, full):
                raise RuntimeError("Original storage unlink was not confirmed")

    async def _head_exists(self, client: Any, full_key: str) -> bool:
        """Return whether a HEAD finds ``full_key`` (missing -> False; other errors propagate)."""
        try:
            await client.head_object(Bucket=self._config.bucket, Key=full_key)
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise
        return True

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` names an existing object (``ValueError`` for invalid keys)."""
        full = self._full(key)
        async with self._client() as client:
            return await self._head_exists(client, full)

    async def _iter_objects(self, prefix: str, *, page_size: int | None = None) -> AsyncGenerator[tuple[str, datetime]]:
        """Yield ``(storage key, last_modified)`` beneath ``prefix`` page by page (bounded memory).

        ``prefix`` names a directory-like scope: a trailing ``/`` is enforced so ``workspaces/…07`` never
        matches ``workspaces/…078``. Keys are returned without the bucket prefix.
        """
        scope = f"{checked_key(prefix.rstrip('/'), allow_empty=True)}/" if prefix else ""
        paging: dict[str, Any] = {"PageSize": page_size} if page_size else {}
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self._config.bucket, Prefix=self._config.prefix + scope, PaginationConfig=paging,
            ):
                for item in page.get("Contents", []):
                    yield item["Key"][len(self._config.prefix):], item["LastModified"]

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return sorted keys beneath ``prefix`` using paginated ``ListObjectsV2`` (``""`` lists the whole store)."""
        return sorted([key async for key, _modified in self._iter_objects(prefix)])

    async def cleanup_orphans(self, referenced: set[str], grace_seconds: int) -> int:
        """Delete old unreferenced upload objects and stale ``staging/`` objects; return the count removed.

        Same semantics as the local sweep: only canonical upload keys (``workspaces/<uuid>/documents/<uuid>/<uuid>.*``
        or the legacy ``documents/…`` layout) older than ``grace_seconds`` and absent from the caller's complete
        live-reference set are removed; unrelated keys are never touched. ``staging/`` objects are never
        referenced, so they only need to be older than the grace. Deletes go in batches of 1000.
        """
        if grace_seconds < 0:
            raise ValueError("Orphan cleanup grace must be nonnegative")
        cutoff = datetime.now(UTC).timestamp() - grace_seconds
        removed = 0
        batch: list[str] = []
        for scope in ("workspaces", "documents", STAGING_DIR):
            async for key, modified in self._iter_objects(scope):
                if modified.timestamp() > cutoff or key in referenced:
                    continue
                if scope != STAGING_DIR and not _UPLOAD_KEY.match(key):
                    continue
                batch.append(key)
                if len(batch) >= 1000:
                    removed += await self._delete_batch(batch)
                    batch = []
        return removed + (await self._delete_batch(batch) if batch else 0)

    async def _delete_batch(self, keys: list[str]) -> int:
        """Delete up to 1000 storage keys in one request; return how many S3 reports as deleted."""
        async with self._client() as client:
            response = await client.delete_objects(
                Bucket=self._config.bucket,
                Delete={"Objects": [{"Key": self._config.prefix + key} for key in keys], "Quiet": False},
            )
        return len(response.get("Deleted", []))

    def check_erasable(self, key: str, workspace_id: UUID) -> None:
        """Raise ``ValueError`` unless ``key`` is a safe key in the legacy ``documents/`` or this workspace's layout."""
        checked_key(key)
        if not key.startswith(("documents/", f"workspaces/{workspace_id}/documents/")):
            raise ValueError("Original storage path is outside the account workspace/document layouts")

    async def aprobe_workspace_orphan(self, workspace_id: UUID) -> OrphanProbe:
        """Return ``file`` for the first remaining object of the workspace or ``absent`` (no directories exist)."""
        scope = f"workspaces/{workspace_id}/documents"
        async with contextlib.aclosing(self._iter_objects(scope, page_size=1)) as listing:
            async for key, _modified in listing:
                return OrphanProbe("file", key)
        return OrphanProbe("absent")

    def probe_workspace_orphan(self, workspace_id: UUID) -> OrphanProbe:
        """Unsupported synchronously (listing is network I/O); use ``aprobe_workspace_orphan``."""
        raise NotImplementedError("S3 storage probes are async-only; use aprobe_workspace_orphan()")
