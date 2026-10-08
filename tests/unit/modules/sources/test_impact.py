"""Source impact counts: owner filter in compiled SQL, caps, and purge-pending refusal."""

import inspect
import json
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from modules.chat import public as chat
from modules.chat.schemas import Citation
from modules.connectors import routes as connector_routes
from modules.connectors.providers.telegram import _TelegramAPIError
from modules.connectors.routes import GITHUB_RECONNECT_DETAIL, _collection_error_code
from modules.dashboard import public as dashboard
from modules.knowledge.documents import public as documents
from modules.sources import public as source_public
from modules.sources import public as sources
from modules.sources import routes as source_routes


class FakeSession:
    """Capture compiled statements and answer scalar() from a queue."""

    def __init__(self, *scalars: Any) -> None:
        self.scalars = list(scalars)
        self.sql: list[str] = []

    async def scalar(self, statement: Any) -> Any:
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
        return self.scalars.pop(0)

    async def get(self, _model: Any, _id: Any) -> object:
        return object()


@pytest.mark.parametrize(("status", "code"), [
    (401, "provider_unauthorized"), (403, "provider_unauthorized"),
    (500, "provider_collection_failed"), (503, "provider_collection_failed"),
])
def test_provider_status_maps_to_error_code(status: int, code: str) -> None:
    request = httpx.Request("GET", "https://example.test")
    exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    assert _collection_error_code(exc) == code
    assert _collection_error_code(TimeoutError()) == "provider_collection_failed"


@pytest.mark.parametrize(("exc", "code"), [
    (_TelegramAPIError("telegram_credentials_rejected"), "provider_unauthorized"),
    (_TelegramAPIError("telegram_provider_unavailable"), "provider_collection_failed"),
    (HTTPException(409, GITHUB_RECONNECT_DETAIL), "provider_unauthorized"),
    (HTTPException(409, "other"), "provider_collection_failed"),
])
def test_non_httpx_auth_failures_map_to_error_code(exc: BaseException, code: str) -> None:
    assert _collection_error_code(exc) == code


def test_fetch_native_provider_uses_error_code_helper_in_both_branches() -> None:
    source = inspect.getsource(connector_routes.fetch_native_provider)
    assert source.count("error_code=_collection_error_code(exc)") == 2


@pytest.mark.asyncio
async def test_impact_route_404_and_409(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = SimpleNamespace(owner_id=1)

    async def missing(*_a: Any) -> None:
        return None

    async def pending(*_a: Any) -> None:
        raise HTTPException(status_code=409, detail="Source purge is pending")

    monkeypatch.setattr(source_public, "get_source_impact", missing)
    with pytest.raises(HTTPException) as nf:
        await source_routes.get_source_impact(uuid4(), None, owner)  # type: ignore[arg-type]
    assert nf.value.status_code == 404
    monkeypatch.setattr(source_public, "get_source_impact", pending)
    with pytest.raises(HTTPException) as conflict:
        await source_routes.get_source_impact(uuid4(), None, owner)  # type: ignore[arg-type]
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_dashboard_counts_are_owner_scoped_and_capped() -> None:
    session = FakeSession(2, 3)
    assert await dashboard.count_source_gadgets(session, 7, uuid4(), cap=5) == (2, 3)  # type: ignore[arg-type]
    assert all("owner_id" in sql and "count(*)" in sql.lower() and "LIMIT" in sql for sql in session.sql)


@pytest.mark.asyncio
async def test_document_and_chat_counts_are_bounded_count_queries() -> None:
    session = FakeSession(4, 1)
    source_id = uuid4()
    assert await documents.count_source_documents(session, source_id) == 4  # type: ignore[arg-type]
    assert await chat.count_source_conversations(session, source_id) == 1  # type: ignore[arg-type]
    assert "source_id" in session.sql[0] and "LIMIT" in session.sql[0]
    chat_sql = session.sql[1]
    assert "LIMIT" in chat_sql and "UNION" in chat_sql and "retrieval_context" in chat_sql
    assert chat_sql.count("@>") >= 3  # source_id and sourceId citations, plus source_scope



@pytest.mark.asyncio
async def test_chat_count_matches_worker_citation_shape() -> None:
    """worker.py persists Citation.model_dump(mode="json", by_alias=True): key `source_id`, JSON-safe."""
    source_id, other = uuid4(), uuid4()
    stored = Citation(
        sourceId=source_id, documentId=other, documentVersionId=other, chunkId=other, title="t", quote="q",
    ).model_dump(mode="json", by_alias=True)
    json.dumps(stored)  # the engine has no custom json_serializer
    assert stored["source_id"] == str(source_id)

    class Params:
        params: ClassVar[list[Any]] = []

        async def scalar(self, statement: Any) -> int:
            self.params = list(statement.compile(dialect=postgresql.dialect()).params.values())
            return 0

    session = Params()
    await chat.count_source_conversations(session, source_id)  # type: ignore[arg-type]
    assert [{"source_id": str(source_id)}] in session.params  # the exact key the worker writes
    assert [{"sourceId": str(source_id)}] in session.params  # older rows


@pytest.mark.asyncio
async def test_impact_is_409_while_purge_pending() -> None:
    with pytest.raises(HTTPException) as caught:
        await sources.get_source_impact(FakeSession(None), 1, uuid4())  # type: ignore[arg-type]
    assert caught.value.status_code == 409
