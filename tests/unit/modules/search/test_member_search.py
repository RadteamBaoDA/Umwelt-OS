"""Member search: grant predicate before ORDER BY/LIMIT, no rerank/embedding, 409 on stale fence."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents.models import Document
from modules.search import public
from modules.search.schemas import SearchRequest

WS = uuid4()
MEMBER = WorkspaceContext(2, WS, "member", 1)
OWNER = WorkspaceContext(1, WS, "owner", 1)
FENCE = AccessFence(WS, 2, 1, 1)


class FakeSession:
    def __init__(self, rows: list | None = None) -> None:
        self.statements: list = []
        self.rows = rows or []

    async def scalars(self, statement, *_a):
        self.statements.append(statement)
        return SimpleNamespace(all=list)

    async def scalar(self, statement):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: self.rows)


@pytest.fixture
def grants(monkeypatch):
    marker = select(Document.id).where(Document.title == "GRANT_MARKER")
    monkeypatch.setattr(public.workspaces, "granted_resource_ids", lambda *, scope, kind: marker)

    async def fence(session, *, scope, multi_workspace_enabled):
        return FENCE

    monkeypatch.setattr(public.workspaces, "read_access_fence", fence)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_grant_predicate_precedes_order_and_limit(grants) -> None:
    session = FakeSession()
    request = SearchRequest(query="alpha", mode="hybrid")
    result = await public.search(
        session, None, None, request, scope=MEMBER, multi_workspace_enabled=True,  # type: ignore[arg-type]
    )
    sql = _sql(session.statements[0])
    assert "GRANT_MARKER" in sql
    assert sql.index("GRANT_MARKER") < sql.index("ORDER BY") < sql.index("LIMIT")
    # hybrid downgraded: no embedding/rerank path for members (redis/settings are None and unused)
    assert result.effective_mode == "lexical" and result.items == []


@pytest.mark.asyncio
async def test_owner_has_no_grant_predicate(grants) -> None:
    session = FakeSession()
    await public.search(
        session, None, None, SearchRequest(query="alpha", mode="lexical"), scope=OWNER,  # type: ignore[arg-type]
        multi_workspace_enabled=True,
    )
    assert "GRANT_MARKER" not in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_changed_fence_is_409_not_partial_page(grants, monkeypatch) -> None:
    calls = iter([FENCE, AccessFence(WS, 2, 2, 1)])

    async def fence(session, *, scope, multi_workspace_enabled):
        return next(calls)

    monkeypatch.setattr(public.workspaces, "read_access_fence", fence)
    # one selected chunk that hydration can no longer see -> whole page fails for a member
    chunk = uuid4()

    class Session(FakeSession):
        async def scalars(self, statement, *_a):
            return SimpleNamespace(all=lambda: [chunk])

    with pytest.raises(HTTPException) as exc:
        await public.search(
            Session(), None, None, SearchRequest(query="alpha"), scope=MEMBER,  # type: ignore[arg-type]
            multi_workspace_enabled=True,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_member_cannot_use_remote_destination_or_index(grants) -> None:
    from core.tools.schemas import ToolDestination

    with pytest.raises(HTTPException) as exc:
        await public.search(
            FakeSession(), None, None, SearchRequest(query="a"), scope=MEMBER,  # type: ignore[arg-type]
            multi_workspace_enabled=True, destination=ToolDestination.REMOTE, source_generation_fences={uuid4(): 1},
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        await public.index_status(
            FakeSession(), scope=MEMBER, multi_workspace_enabled=True, settings=None, redis=None,  # type: ignore[arg-type]
        )
    assert exc2.value.status_code == 403
