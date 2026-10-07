"""Source impact counts: owner filter in compiled SQL, caps, and purge-pending refusal."""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from modules.chat import public as chat
from modules.connectors.routes import _collection_error_code
from modules.dashboard import public as dashboard
from modules.knowledge.documents import public as documents
from modules.sources import public as sources


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
    assert "DISTINCT" in session.sql[1] and "LIMIT" in session.sql[1]


@pytest.mark.asyncio
async def test_impact_is_409_while_purge_pending() -> None:
    with pytest.raises(HTTPException) as caught:
        await sources.get_source_impact(FakeSession(None), 1, uuid4())  # type: ignore[arg-type]
    assert caught.value.status_code == 409
