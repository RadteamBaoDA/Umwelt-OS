"""P14-T1 acceptance: API-engine DB timeouts and oversized-upload rejection (needs the disposable Compose harness)."""

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.main import create_app
from core.config import Settings
from core.database import make_session_factory

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

THIRTY_MIB = 30 * 1024 * 1024
UPLOAD = "/api/v1/documents/upload"


@pytest.mark.asyncio
async def test_api_engine_sessions_carry_configured_timeouts(committed_engine: AsyncEngine) -> None:
    settings = Settings()
    engine, factory = make_session_factory(
        committed_engine.url.render_as_string(hide_password=False),  # fixture already vetted it as bbd_test
        pool_size=1,
        max_overflow=0,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    try:
        async with factory() as session:
            rows = dict((await session.execute(text(
                "SELECT name, setting FROM pg_settings "
                "WHERE name IN ('statement_timeout', 'idle_in_transaction_session_timeout')"
            ))).all())  # pg_settings reports raw milliseconds
        assert rows == {
            "statement_timeout": str(settings.db_statement_timeout_ms),
            "idle_in_transaction_session_timeout": str(settings.db_idle_tx_timeout_ms),
        }
        assert settings.db_statement_timeout_ms == 60000 and settings.db_idle_tx_timeout_ms == 240000
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_running_api_rejects_30_mib_upload_with_413_before_auth(anonymous_client: AsyncClient) -> None:
    # The server answers 413 and closes as soon as it sees the declared size, so a client still
    # streaming 30 MiB gets a connection reset. Declare the size, send only a prefix, then read.
    url = anonymous_client.base_url
    reader, writer = await asyncio.open_connection(url.host, url.port or 80)
    try:
        crlf = "\r\n"
        writer.write(
            (
                f"POST {UPLOAD} HTTP/1.1{crlf}Host: {url.host}{crlf}"
                f"Content-Type: multipart/form-data; boundary=b{crlf}Content-Length: {THIRTY_MIB}{crlf}{crlf}"
            ).encode()
            + b"x" * (64 * 1024)
        )
        await writer.drain()
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        status_line, _, headers = raw_head.partition(b"\r\n")
        assert status_line.split()[1] == b"413", status_line  # not 401: the cap runs before authentication
        length = int(next(
            line.split(b":", 1)[1] for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")
        ))
        assert json.loads(await reader.readexactly(length)) == {"detail": "Request body too large"}
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_oversized_upload_does_not_grow_tmp_dir_content_length_and_chunked(
    committed_engine: AsyncEngine,
) -> None:
    """In-process ASGI app (same middleware stack): neither path may spool the body to temp files."""
    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.iterdir()}
    app = create_app(Settings(database_url=committed_engine.url.render_as_string(hide_password=False), csrf_signing_secret="s"))

    async def chunked() -> AsyncIterator[bytes]:
        boundary_head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n'
        yield boundary_head
        for _ in range(30):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--b--\r\n"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as client:
        sized = await client.post(UPLOAD, files={"file": ("big.txt", b"x" * THIRTY_MIB)})
        streamed = await client.post(
            UPLOAD, content=chunked(), headers={"content-type": "multipart/form-data; boundary=b"}
        )
    assert sized.status_code == 413 and streamed.status_code == 413
    assert streamed.json() == {"detail": "Request body too large"}
    assert {p.name for p in tmp.iterdir()} - before == set()
