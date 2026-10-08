"""Shared recorder session and scope constants for copied-owner cleanup scope tests (no DB)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=2)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=3)
FENCE = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=1)
CTX = {"scope": OWNER, "multi_workspace_enabled": False}
MEMBER_CTX = {"scope": MEMBER, "multi_workspace_enabled": False}


def admitted():
    """Patch the shared owner admission read (every owner module calls it via the workspaces module)."""
    return patch("core.workspaces.public.read_access_fence", AsyncMock(return_value=FENCE))


def evidence(**overrides: object) -> DocumentCleanupEvidenceScope:
    values: dict[str, object] = {
        "operation_id": uuid4(), "source_id": uuid4(), "document_id": uuid4(), "references": (),
        "next_cursor": None, "workspace_id": WS, "actor_user_id": 7,
    }
    values.update(overrides)
    return DocumentCleanupEvidenceScope(**values)  # type: ignore[arg-type]


class Recorder:
    """AsyncSession stand-in: every statement is compiled and kept; every read is empty."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def _record(self, statement: object) -> str:
        text = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
        self.sql.append(text)
        return text

    async def execute(self, statement: object, *_a: object, **_k: object) -> MagicMock:
        self._record(statement)
        return MagicMock(all=list, one_or_none=lambda: None)

    async def scalars(self, statement: object) -> SimpleNamespace:
        self._record(statement)
        return SimpleNamespace(all=list)

    async def scalar(self, statement: object) -> None:
        self._record(statement)

    async def flush(self) -> None:
        return None

    def add(self, _row: object) -> None:
        return None

    def selects(self) -> list[str]:
        return [text for text in self.sql if text.lstrip().upper().startswith("SELECT")]
