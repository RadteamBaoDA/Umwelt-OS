"""Workspace-scope contracts for frozen Documents readers (compiled SQL, denial, cursors; no DB)."""

import contextlib
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents import public
from modules.knowledge.observations import public as observations
from tests.unit.modules.knowledge.documents._scope import FENCE, SCOPE, SCOPE_KW, WORKSPACE_ID


class _Rows:
    """Result stub for execute/scalars."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def one_or_none(self):
        return None


class _Session:
    """Record every statement; results are empty."""

    def __init__(self) -> None:
        self.statements: list = []
        self.scalars = AsyncMock(side_effect=self._scalars)

    async def _scalars(self, statement):
        self.statements.append(statement)
        return _Rows([])

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows([])

    async def scalar(self, statement):
        self.statements.append(statement)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_review_version_fences_reader_carries_workspace_predicate() -> None:
    session = _Session()
    await public.review_version_fences(session, [uuid4()], **SCOPE_KW)
    assert "workspace_id" in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_member_without_share_reads_nothing() -> None:
    member = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await public.review_version_fences(session, [uuid4()], scope=member, multi_workspace_enabled=True)
    assert exc.value.status_code == 403
    assert session.statements == []


@pytest.mark.asyncio
async def test_lock_chat_evidence_chunks_never_locks_foreign_identity() -> None:
    session = _Session()
    lock_sources = AsyncMock()
    with patch.object(public.sources, "lock_source_set", lock_sources):
        result = await public.lock_chat_evidence_chunks(
            session, [(uuid4(), uuid4())], **SCOPE_KW,
        )
    assert result == []
    assert "workspace_id" in _sql(session.statements[0])
    lock_sources.assert_not_awaited()
    session.scalars.assert_not_awaited()  # no Document/version/chunk row lock without scoped identities


def test_v3_export_cursors_reject_revision_mismatch() -> None:
    now, row = datetime.now(UTC), uuid4()
    doc = public._encode_document_export_cursor(1, WORKSPACE_ID, "documents", now, now, row, FENCE)
    obs = observations._observation_export_cursor(1, WORKSPACE_ID, now, now, row, FENCE)
    assert public._decode_document_export_cursor(doc, 1, WORKSPACE_ID, "documents", FENCE)[2] == row
    assert observations._decode_observation_export_cursor(obs, 1, WORKSPACE_ID, FENCE)[2] == row
    stale = AccessFence(WORKSPACE_ID, 1, 1, 2)
    with pytest.raises(ValueError):
        public._decode_document_export_cursor(doc, 1, WORKSPACE_ID, "documents", stale)
    with pytest.raises(ValueError):
        observations._decode_observation_export_cursor(obs, 1, WORKSPACE_ID, stale)


# (reader, positional args, extra kwargs); scope/flag are always added.
_FROZEN_READERS = [
    ("document_metadata", ([uuid4()],), {}),
    ("existing_document_ids", ([uuid4()],), {}),
    ("read_extraction_input", (uuid4(),), {}),
    ("get_ready_version_ref", (uuid4(),), {}),
    ("list_ready_version_refs", (), {}),
    ("read_evidence_refs", ([(uuid4(), uuid4())],), {}),
    ("review_version_locator", (uuid4(),), {}),
    ("review_version_fences", ([uuid4()],), {}),
    ("lock_document_ids", ([uuid4()],), {}),
    ("read_chat_evidence_chunks", ([(uuid4(), uuid4())],), {}),
    ("lock_chat_evidence_chunks", ([(uuid4(), uuid4())],), {}),
    ("cleanup_evidence_version_document", (uuid4(),), {}),
    ("get_news_document_projection", (uuid4(),), {}),
    ("news_retained_observation_allowed", (), {"document_id": uuid4(), "source_id": uuid4(),
                                               "expected_source_generation": 1}),
    ("news_projection_scope_unavailable", (uuid4(), 1), {}),
    ("news_current_scope_status", ((uuid4(),),), {}),
    ("list_news_document_projections", (), {"source_ids": (uuid4(),)}),
    ("list_gadget_highlight_projection_page", (), {"source_ids": (uuid4(),)}),
    ("list_gadget_document_projections", (), {"source_ids": (uuid4(),)}),
    ("set_gadget_document_interaction", (), {"document_id": uuid4(), "version_number": 1,
                                             "payload": SimpleNamespace(read=True, bookmarked=None)}),
    ("list_provider_snapshots", (), {"source_ids": [uuid4()]}),
    ("read_provider_snapshots", ([uuid4()],), {}),
    ("get_tool_document", (uuid4(),), {"source_ids": frozenset(), "owner_all": True}),
    ("list_tool_documents", (), {"limit": 10, "cursor": None, "source_ids": frozenset(), "owner_all": True}),
]
_IDS = [row[0] for row in _FROZEN_READERS]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "kwargs"), _FROZEN_READERS, ids=_IDS)
async def test_each_frozen_reader_admits_then_carries_workspace_predicate(name, args, kwargs, admission) -> None:
    session = _Session()
    with patch.object(public.sources, "lock_source_for_document", AsyncMock()), \
            patch.object(public, "commit_with_replay", AsyncMock()), \
            contextlib.suppress(Exception):  # empty stub results may abort after the first statement
        await getattr(public, name)(session, *args, **kwargs, **SCOPE_KW)
    admission.assert_awaited_with(session, scope=SCOPE, multi_workspace_enabled=False)
    assert any("workspace_id" in _sql(item) for item in session.statements), name


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "kwargs"), _FROZEN_READERS, ids=_IDS)
async def test_each_frozen_reader_runs_no_query_when_admission_denied(name, args, kwargs, admission) -> None:
    admission.side_effect = HTTPException(status_code=409)
    session = _Session()
    with pytest.raises(HTTPException):
        await getattr(public, name)(session, *args, **kwargs, **SCOPE_KW)
    assert session.statements == []


def test_interaction_and_projection_writers_have_no_owner_id() -> None:
    for fn in (public.list_gadget_document_projections, public.set_gadget_document_interaction):
        assert "owner_id" not in inspect.signature(fn).parameters


@pytest.mark.asyncio
async def test_interaction_uses_admitted_actor_and_fenced_commit() -> None:
    version_id = uuid4()
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[uuid4(), SimpleNamespace()])  # source id, locked document
    session.get = AsyncMock(return_value=None)
    projection = SimpleNamespace(version_number=1, document_version_id=version_id)
    commit, lock = AsyncMock(), AsyncMock()
    with patch.object(public, "get_news_document_projection", AsyncMock(return_value=projection)), \
            patch.object(public, "commit_with_replay", commit), \
            patch.object(public.sources, "lock_source_for_document", lock):
        await public.set_gadget_document_interaction(
            session, document_id=uuid4(), version_number=1,
            payload=SimpleNamespace(read=True, bookmarked=None, dismissed=None), **SCOPE_KW,
        )
    assert session.get.await_args.args[1] == (FENCE.user_id, version_id)
    assert session.add.call_args.args[0].owner_id == FENCE.user_id
    assert lock.await_args.kwargs["expected_access_fence"] == FENCE
    assert commit.await_args.args[1] == []
    assert commit.await_args.kwargs["access_fence"] == FENCE


@pytest.mark.asyncio
async def test_read_evidence_refs_write_passes_expected_fence_to_source_set() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[_Rows([uuid4()]), _Rows([uuid4()]), _Rows([])])
    lock_set = AsyncMock(return_value=SimpleNamespace(fences=[object()]))
    with patch("modules.sources.public.lock_source_set", lock_set), \
            patch.object(public, "_read_evidence_ref_rows", AsyncMock(return_value=[])):
        await public.read_evidence_refs(session, [(uuid4(), uuid4())], for_write=True, **SCOPE_KW)
    assert lock_set.await_args.kwargs["expected_access_fence"] == FENCE


@pytest.mark.asyncio
async def test_chat_lock_mixed_workspaces_locks_only_scoped_ids_with_fence() -> None:
    """Capture returns the A row only; the foreign B version/chunk are never locked."""
    source, document, version, chunk = (uuid4() for _ in range(4))
    foreign = (uuid4(), uuid4())
    session = MagicMock()
    session.execute = AsyncMock(return_value=_Rows([(source, document, version, chunk)]))
    session.scalars = AsyncMock(return_value=_Rows([]))
    lock_set = AsyncMock()
    with patch.object(public.sources, "lock_source_set", lock_set), \
            patch.object(public, "read_chat_evidence_chunks", AsyncMock(return_value=[])):
        await public.lock_chat_evidence_chunks(session, [(version, chunk), foreign], **SCOPE_KW)
    assert lock_set.await_args.args[1] == [source]
    assert lock_set.await_args.kwargs["expected_access_fence"] == FENCE
    bound: set[str] = set()
    for call in session.scalars.await_args_list:
        for value in call.args[0].compile(dialect=postgresql.dialect()).params.values():
            bound |= {str(x) for x in value} if isinstance(value, (list, tuple)) else {str(value)}
    assert {str(document), str(version), str(chunk)} <= bound
    assert not {str(foreign[0]), str(foreign[1])} & bound


def test_news_fingerprint_changes_with_either_revision() -> None:
    def fp(fence: AccessFence) -> str:
        return public._news_projection_cursor_fingerprint(
            access_fence=fence, source_ids=(), observed_since=None, channel_ids=None,
        )

    base = fp(FENCE)
    assert fp(AccessFence(WORKSPACE_ID, 1, 2, 1)) != base
    assert fp(AccessFence(WORKSPACE_ID, 1, 1, 2)) != base


def test_observation_list_fingerprint_changes_with_either_revision() -> None:
    from modules.knowledge.observations.schemas import ObservationQuery

    def fp(fence: AccessFence) -> str:
        return observations._cursor_fingerprint(ObservationQuery.model_construct(limit=10, source_ids=[]), (), access_fence=fence)

    base = fp(FENCE)
    assert fp(AccessFence(WORKSPACE_ID, 1, 2, 1)) != base
    assert fp(AccessFence(WORKSPACE_ID, 1, 1, 2)) != base


@pytest.mark.asyncio
async def test_get_first_chunk_id_is_workspace_scoped_and_denies_members_first() -> None:
    session = _Session()
    assert await public.get_first_chunk_id(session, uuid4(), **SCOPE_KW) is None  # foreign/absent version
    sql = _sql(session.statements[0])
    assert "documents.workspace_id" in sql and "document_versions" in sql

    member = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
    denied = _Session()
    with pytest.raises(HTTPException) as exc:
        await public.get_first_chunk_id(denied, uuid4(), scope=member, multi_workspace_enabled=True)
    assert exc.value.status_code == 403
    assert denied.statements == []
