"""Source impact: scoped counts, foreign-source concealment, purge-pending refusal, error-code mapping."""

import inspect
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import WorkspaceContext
from modules.chat import public as chat
from modules.connectors import routes as connector_routes
from modules.connectors.providers.telegram import _TelegramAPIError
from modules.connectors.routes import GITHUB_RECONNECT_DETAIL, _collection_error_code
from modules.dashboard import public as dashboard
from modules.knowledge.documents import public as documents
from modules.sources import public as source_public
from modules.sources import public as sources
from modules.sources import routes as source_routes

WORKSPACE_ID = uuid4()


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


SCOPE = WorkspaceContext(user_id=7, workspace_id=WORKSPACE_ID, role="owner", membership_revision=3)
REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
    settings=SimpleNamespace(multi_workspace_enabled=False),
)))


@pytest.mark.asyncio
async def test_impact_route_404_and_409(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def missing(_session: Any, _source_id: Any, **kw: Any) -> None:
        seen.update(kw)

    async def pending(*_a: Any, **_kw: Any) -> None:
        raise HTTPException(status_code=409, detail="Source purge is pending")

    monkeypatch.setattr(source_public, "get_source_impact", missing)
    with pytest.raises(HTTPException) as nf:
        await source_routes.get_source_impact(uuid4(), None, REQUEST, SCOPE)  # type: ignore[arg-type]
    assert nf.value.status_code == 404
    assert seen == {"scope": SCOPE, "multi_workspace_enabled": False}
    monkeypatch.setattr(source_public, "get_source_impact", pending)
    with pytest.raises(HTTPException) as conflict:
        await source_routes.get_source_impact(uuid4(), None, REQUEST, SCOPE)  # type: ignore[arg-type]
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_document_count_is_workspace_scoped_and_capped_before_limit() -> None:
    session = FakeSession(4)
    assert await documents.count_source_documents(session, uuid4(), scope=SCOPE) == 4  # type: ignore[arg-type]
    sql = session.sql[0]
    assert "workspace_id" in sql and "source_id" in sql and "count(*)" in sql.lower()
    assert sql.index("workspace_id") < sql.index("LIMIT")


@pytest.mark.asyncio
async def test_find_document_identity_sql_carries_workspace_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def admitted(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(documents, "_admit_document_scope", admitted)
    session = FakeSession(uuid4())
    assert await documents.find_document_identity(
        session, uuid4(), "file:abc", scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    ) is not None
    assert "workspace_id" in session.sql[0] and "external_id" in session.sql[0]


class ImpactSession:
    """Answer get_source_impact queries; records whether the dependent counts ran."""

    def __init__(self, *scalars: Any) -> None:
        self.scalars = list(scalars)

    async def scalar(self, statement: Any) -> Any:
        return self.scalars.pop(0)


@pytest.mark.asyncio
async def test_impact_foreign_source_is_none_without_running_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_found(*_a: Any, **_kw: Any) -> None:
        return None

    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("counts must not run for a foreign source")

    monkeypatch.setattr(sources, "get_source", not_found)
    monkeypatch.setattr(documents, "count_source_documents", boom)
    assert await sources.get_source_impact(
        ImpactSession(), uuid4(), scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    ) is None


@pytest.mark.asyncio
async def test_impact_is_409_while_purge_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def found(*_a: Any, **_kw: Any) -> object:
        return object()

    monkeypatch.setattr(sources, "get_source", found)
    with pytest.raises(HTTPException) as caught:
        await sources.get_source_impact(
            ImpactSession(None), uuid4(), scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_impact_passes_scope_to_all_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def found(*_a: Any, **_kw: Any) -> object:
        return object()

    seen: list[Any] = []

    async def doc_count(_s: Any, _id: Any, *, scope: Any, cap: int = 1000) -> int:
        seen.append(scope)
        return 1

    async def gadget_count(_s: Any, _id: Any, *, scope: Any, cap: int = 1000) -> tuple[int, int]:
        seen.append(scope)
        return 2, 3

    async def conv_count(_s: Any, _id: Any, *, scope: Any, cap: int = 1000) -> int:
        seen.append(scope)
        return 4

    monkeypatch.setattr(sources, "get_source", found)
    monkeypatch.setattr(documents, "count_source_documents", doc_count)
    monkeypatch.setattr(dashboard, "count_source_gadgets", gadget_count, raising=False)
    monkeypatch.setattr(chat, "count_source_conversations", conv_count)
    impact = await sources.get_source_impact(
        ImpactSession(uuid4()), uuid4(), scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    assert impact == sources.SourceImpact(1, 2, 3, 4)
    assert seen == [SCOPE, SCOPE, SCOPE]
