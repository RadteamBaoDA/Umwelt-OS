"""Unit tests for the stages that ``execute_browser_read`` is composed of."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

import httpx
import pytest

from modules.connectors.public import AgentBrowserScope
from modules.tools import browser_public as bp

JOB_ID = uuid4()
OP_ID = uuid4()
SOURCE_ID = uuid4()
GRANT = AgentBrowserScope(
    workspace_id=uuid4(), source_id=SOURCE_ID, source_generation=1, connector_revision=1, grant_revision=1,
    scope_hash="s", origin="https://example.com", path_prefix="/", local_only=False, enabled=True,
)


def _plan(max_pages: int = 2) -> bp._DispatchPlan:
    """Build a dispatch plan whose service instance is already known."""
    payload = {"service_instance_id": "svc", "max_pages": max_pages}
    return bp._DispatchPlan(payload, "http://svc", "secret", "token", OP_ID, GRANT)


def _page(raw: bytes = b"<p>hi</p>", **over: Any) -> dict[str, Any]:
    """Build one wire page that is valid by default."""
    page = {
        "requested_url": "https://example.com/a", "final_url": "https://example.com/a",
        "observed_at": datetime.now(UTC).isoformat(), "extracted_text": "hi",
        "raw_content": base64.b64encode(raw).decode(), "content_digest": hashlib.sha256(raw).hexdigest(),
    }
    page.update(over)
    return page


def _data(pages: list[dict[str, Any]], **over: Any) -> dict[str, object]:
    """Wrap pages in a matching service envelope."""
    data: dict[str, object] = {
        "job_id": str(JOB_ID), "operation_id": str(OP_ID), "claim_generation": 1, "service_instance_id": "svc",
        "pages": pages,
    }
    data.update(over)
    return data


def test_validate_pages_accepts_and_hashes() -> None:
    """Valid pages produce rows, reads, the byte total and both result hashes."""
    result = bp._validate_service_pages(_data([_page(), _page(b"second")]), _plan(), JOB_ID, 1)
    assert [row.page_number for row in result.rows] == [1, 2]
    assert result.raw_total == len(b"<p>hi</p>") + len(b"second")
    assert len(result.result_hash) == 64 and len(result.service_result_hash) == 64
    assert [read.id for read in result.reads] == [row.id for row in result.rows]


@pytest.mark.parametrize("override", [
    {"job_id": "other"}, {"operation_id": "other"}, {"claim_generation": 2}, {"service_instance_id": "x"},
    {"pages": []}, {"pages": [_page()] * 3}, {"pages": ["notadict"]},
])
def test_validate_pages_rejects_bad_envelope(override: dict[str, Any]) -> None:
    """Identity or bound mismatches are ValueError (invalid_result)."""
    data = _data([_page()])
    data.update(override)
    with pytest.raises(ValueError, match="invalid|bounds"):
        bp._validate_service_pages(data, _plan(), JOB_ID, 1)


def test_validate_pages_scope_digest_and_size() -> None:
    """Out-of-scope URLs are PermissionError; digest, text, time and base64 faults are ValueError."""
    with pytest.raises(PermissionError):
        bp._validate_service_pages(_data([_page(final_url="https://evil.example/")]), _plan(), JOB_ID, 1)
    with pytest.raises(ValueError, match="digest"):
        bp._validate_service_pages(_data([_page(content_digest="0" * 64)]), _plan(), JOB_ID, 1)
    with pytest.raises(ValueError, match="bound"):
        bp._validate_service_pages(_data([_page(extracted_text="x" * 20_001)]), _plan(), JOB_ID, 1)
    with pytest.raises(ValueError, match="timezone"):
        bp._validate_service_pages(_data([_page(observed_at="2026-01-01T00:00:00")]), _plan(), JOB_ID, 1)
    with pytest.raises(ValueError, match=r"."):
        bp._validate_service_pages(_data([_page(raw_content="***")]), _plan(), JOB_ID, 1)


class _Response:
    """Minimal streamed httpx response double."""

    def __init__(self, chunks: list[bytes], status: int = 200, length: str | None = None) -> None:
        """Store the body chunks, status and optional content-length header."""
        self._chunks, self.status_code = chunks, status
        self.headers = {} if length is None else {"content-length": length}
        self.request = httpx.Request("POST", "http://svc/agent-reads")

    async def aiter_bytes(self) -> Any:
        """Yield the configured chunks."""
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_read_envelope_bounds_and_status() -> None:
    """Valid JSON objects parse; oversize, non-object and 4xx bodies raise the documented errors."""
    assert await bp._read_service_envelope(_Response([b'{"a":', b"1}"])) == {"a": 1}
    with pytest.raises(ValueError, match="exceeds"):
        await bp._read_service_envelope(_Response([b"{}"], length=str(9 * 1024 * 1024)))
    with pytest.raises(ValueError, match="exceeds"):
        await bp._read_service_envelope(_Response([b"x" * (8 * 1024 * 1024 + 1)]))
    with pytest.raises(ValueError, match="invalid"):
        await bp._read_service_envelope(_Response([b"[1]"]))
    with pytest.raises(httpx.HTTPStatusError):
        await bp._read_service_envelope(_Response([b"{}"], status=429))


@pytest.mark.asyncio
async def test_call_service_maps_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP status errors keep their code; other transport faults become status 0 with no data."""
    class Client:
        """AsyncClient double failing the instance probe."""

        def __init__(self, **_kw: Any) -> None:
            """Ignore client options."""

        async def __aenter__(self) -> Self:
            """Enter the client scope."""
            return self

        async def __aexit__(self, *_a: object) -> None:
            """Leave the client scope."""

        async def get(self, *_a: Any, **_k: Any) -> Any:
            """Fail with the configured error."""
            raise error

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    error: Exception = httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"), response=httpx.Response(403))
    assert await bp._call_browser_service(_plan(), deadline=1e12) == (None, 403, False)
    error = httpx.ConnectError("down")
    assert await bp._call_browser_service(_plan(), deadline=1e12) == (None, 0, False)
    error = json.JSONDecodeError("bad", "", 0)
    assert await bp._call_browser_service(_plan(), deadline=1e12) == (None, 0, False)
