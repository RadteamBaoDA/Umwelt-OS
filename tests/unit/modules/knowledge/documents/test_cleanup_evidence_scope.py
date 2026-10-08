"""Documents cleanup evidence page: owner admission, workspace/actor-bound operation and references."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents import public as documents
from tests.unit.modules.knowledge.documents._scope import SCOPE, SCOPE_KW, WORKSPACE_ID


class _Session:
    def __init__(self, operation: object | None) -> None:
        self.operation, self.sql = operation, []

    async def scalar(self, statement: object) -> object | None:
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined]
        return self.operation

    async def scalars(self, statement: object) -> SimpleNamespace:
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined]
        return SimpleNamespace(all=list)


async def test_member_denied_before_sql() -> None:
    member = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
    session = _Session(None)
    with pytest.raises(HTTPException) as caught:
        await documents.list_document_cleanup_evidence_scope(
            session, uuid4(), scope=member, multi_workspace_enabled=False,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 403 and session.sql == []


async def test_operation_is_workspace_and_actor_bound_and_dto_carries_both() -> None:
    session = _Session(None)
    assert await documents.list_document_cleanup_evidence_scope(session, uuid4(), **SCOPE_KW) is None  # type: ignore[arg-type]
    where = session.sql[0].split("WHERE", 1)[1]
    assert "workspace_id = " in where and "actor_user_id = " in where
    operation = SimpleNamespace(
        id=uuid4(), source_id=uuid4(), document_id=uuid4(), workspace_id=WORKSPACE_ID, actor_user_id=1,
    )
    session = _Session(operation)
    page = await documents.list_document_cleanup_evidence_scope(session, operation.id, **SCOPE_KW)  # type: ignore[arg-type]
    assert page is not None and page.workspace_id == WORKSPACE_ID and page.actor_user_id == SCOPE.user_id
    assert "workspace_id = " in session.sql[1].split("WHERE", 1)[1]
