"""Characterization of ``recover_news_work`` and the ready-event happy path: lock order and cursors."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.errors import ConflictError, ForbiddenError
from core.workspaces.schemas import AccessFence
from modules.news import worker

WS = uuid4()
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=2, configuration_revision=3)


class _Rec:
    """Record the call order of patched collaborators and return scripted results."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def mock(self, name: str, result: object = None) -> AsyncMock:
        async def _call(*_a: object, **_k: object) -> object:
            self.order.append(name)
            if isinstance(result, BaseException):
                raise result
            return result
        return AsyncMock(side_effect=_call)


def _ctx(session: AsyncMock) -> dict[str, object]:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return {"session_factory": MagicMock(return_value=cm), "settings": SimpleNamespace(multi_workspace_enabled=True)}


def _projection(source_id: object, document_id: object) -> SimpleNamespace:
    return SimpleNamespace(
        source_id=source_id, document_id=document_id, document_version_id=uuid4(), version_number=1,
        current_source_generation=4,
    )


def _session(checkpoint: SimpleNamespace | None, workspace_ids: list[object]) -> AsyncMock:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: workspace_ids))
    session.scalar = AsyncMock(return_value=checkpoint)
    return session


def _patches(rec: _Rec, *, projection: SimpleNamespace, read_fence: object = FENCE, enabled: bool = True,
             list_projections: AsyncMock | None = None) -> list[object]:
    source_id = projection.source_id
    owner = SimpleNamespace(user_id=1, membership_revision=2)
    source_fence = SimpleNamespace(status="active", generation=4)
    return [
        patch.object(worker, "_read_cursor", rec.mock("read_cursor", None)),
        patch.object(worker, "_write_cursor", rec.mock("write_cursor")),
        patch.object(worker.documents, "list_ready_document_workspace_ids", rec.mock("init_scan", [])),
        patch.object(worker.workspaces, "resolve_workspace_owner_context", rec.mock("owner", owner)),
        patch.object(worker.workspaces, "read_access_fence", rec.mock("read_fence", read_fence)),
        patch.object(worker.workspaces, "lock_access_fence", rec.mock("lock_fence", FENCE)),
        patch.object(worker.settings_public, "module_is_enabled", rec.mock("module", enabled)),
        patch.object(worker.sources, "list_active_gadget_sources",
                     rec.mock("source_page", SimpleNamespace(items=[SimpleNamespace(id=source_id)], next_cursor="sc"))),
        patch.object(worker.documents, "list_news_document_projections",
                     list_projections or rec.mock("projections", ([projection], "dc"))),
        patch.object(worker.sources, "lock_source", rec.mock("lock_source", source_fence)),
        patch.object(worker.documents, "lock_document_ids", rec.mock("lock_docs", [projection.document_id])),
        patch.object(worker.documents, "get_news_document_projection", rec.mock("reproject", projection)),
        patch.object(worker, "cluster_observation", rec.mock("cluster")),
        patch.object(worker, "commit_with_replay", rec.mock("commit")),
    ]


async def _run(session: AsyncMock, patches: list[object]) -> int:
    from contextlib import ExitStack
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)  # type: ignore[arg-type]
        return await worker.recover_news_work(_ctx(session))


@pytest.mark.asyncio
async def test_recovery_clusters_page_in_domain_lock_order_and_advances_cursors() -> None:
    """Admission locks precede source, document and cursor publication; the checkpoint advances."""
    rec, workspace_id = _Rec(), uuid4()
    projection = _projection(uuid4(), uuid4())
    checkpoint = SimpleNamespace(source_cursor=None, document_cursor=None)
    session = _session(checkpoint, [workspace_id])
    session.execute = AsyncMock(side_effect=lambda *_a, **_k: rec.order.append("advisory_lock"))
    count = await _run(session, _patches(rec, projection=projection))
    assert count == 1
    assert rec.order == [
        "read_cursor", "init_scan", "write_cursor",  # initialization pass finds nothing
        "read_cursor", "owner", "read_fence", "module", "lock_fence",
        "source_page", "projections", "lock_source", "lock_docs", "reproject", "cluster",
        "advisory_lock", "commit", "write_cursor",
    ]
    # More documents remain on this source page, so only the document cursor moves.
    assert checkpoint.document_cursor == "dc" and checkpoint.source_cursor is None


@pytest.mark.asyncio
async def test_recovery_advances_source_cursor_when_documents_exhausted() -> None:
    """A finished document page moves the checkpoint to the next source page."""
    rec = _Rec()
    projection = _projection(uuid4(), uuid4())
    checkpoint = SimpleNamespace(source_cursor=None, document_cursor="old")
    session = _session(checkpoint, [uuid4()])
    patches = _patches(rec, projection=projection, list_projections=rec.mock("projections", ([], None)))
    assert await _run(session, patches) == 0
    assert checkpoint.source_cursor == "sc" and checkpoint.document_cursor is None
    assert "cluster" not in rec.order


@pytest.mark.asyncio
async def test_recovery_restarts_page_when_stored_cursor_is_undecodable() -> None:
    """A fingerprint-mismatched document cursor restarts the source page; other errors surface."""
    rec = _Rec()
    projection = _projection(uuid4(), uuid4())
    calls: list[object] = []

    async def projections(_s: object, *, cursor: object, **_k: object) -> object:
        calls.append(cursor)
        if cursor is not None:
            raise ValueError("Invalid cursor")
        return [projection], None

    checkpoint = SimpleNamespace(source_cursor=None, document_cursor="stale")
    session = _session(checkpoint, [uuid4()])
    assert await _run(session, _patches(rec, projection=projection, list_projections=AsyncMock(side_effect=projections))) == 1
    assert calls == ["stale", None]
    other = _patches(_Rec(), projection=projection, list_projections=AsyncMock(side_effect=ValueError("boom")))
    with pytest.raises(ValueError, match="boom"):
        await _run(_session(SimpleNamespace(source_cursor=None, document_cursor="stale"), [uuid4()]), other)


@pytest.mark.asyncio
async def test_recovery_denial_rolls_back_and_continues_but_other_errors_raise() -> None:
    """401/403/404/409 admission failures skip the workspace; the recovery cursor still advances."""
    rec = _Rec()
    projection = _projection(uuid4(), uuid4())
    session = _session(SimpleNamespace(source_cursor=None, document_cursor=None), [uuid4()])
    denied = _patches(rec, projection=projection, read_fence=ForbiddenError("no"))
    assert await _run(session, denied) == 0
    session.rollback.assert_awaited()
    assert rec.order[-1] == "write_cursor" and "cluster" not in rec.order
    stale = _patches(_Rec(), projection=projection, read_fence=ConflictError("stale"))
    assert await _run(_session(None, [uuid4()]), stale) == 0
    other = _patches(_Rec(), projection=projection, read_fence=RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        await _run(_session(None, [uuid4()]), other)


@pytest.mark.asyncio
async def test_recovery_skips_disabled_module_and_missing_checkpoint() -> None:
    """A disabled module or missing checkpoint processes nothing and takes no cursor lock."""
    projection = _projection(uuid4(), uuid4())
    rec = _Rec()
    assert await _run(_session(None, [uuid4()]), _patches(rec, projection=projection, enabled=False)) == 0
    assert "lock_fence" not in rec.order
    rec = _Rec()
    assert await _run(_session(None, [uuid4()]), _patches(rec, projection=projection)) == 0
    assert "source_page" not in rec.order
    rec = _Rec()
    assert await _run(_session(None, []), _patches(rec, projection=projection)) == 0
    assert rec.order[-1] == "write_cursor" and "owner" not in rec.order


@pytest.mark.asyncio
async def test_initialization_pass_creates_checkpoints_for_legacy_workspaces() -> None:
    """Workspaces with ready documents but no checkpoint get one under the legacy catch-up lock."""
    rec, workspace_id = _Rec(), uuid4()
    projection = _projection(uuid4(), uuid4())
    session = _session(None, [])
    session.execute = AsyncMock(side_effect=lambda *_a, **_k: rec.order.append("advisory_lock"))
    patches = _patches(rec, projection=projection)
    patches.append(patch.object(worker.documents, "list_ready_document_workspace_ids", rec.mock("init_scan", [workspace_id])))
    patches.append(patch.object(worker, "_ensure_recovery_checkpoint", rec.mock("ensure_checkpoint")))
    await _run(session, patches)
    assert rec.order[:9] == [
        "read_cursor", "init_scan", "owner", "read_fence", "module", "lock_fence",
        "advisory_lock", "ensure_checkpoint", "commit",
    ]
    assert rec.order[9] == "write_cursor"


async def test_ensure_recovery_checkpoint_inserts_on_conflict_then_locks() -> None:
    """The checkpoint is created with ON CONFLICT DO NOTHING, then row-locked; never a plain ``add``."""
    from sqlalchemy.dialects import postgresql

    stmts: list[str] = []
    session = MagicMock()

    async def execute(stmt: object, *_a: object, **_k: object) -> None:
        stmts.append(str(stmt.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined]

    async def scalar(stmt: object, *_a: object, **_k: object) -> None:
        stmts.append(str(stmt.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined]

    session.execute, session.scalar, session.commit = execute, scalar, AsyncMock()
    await worker._ensure_recovery_checkpoint(session, WS)
    assert "INSERT INTO news_recovery_checkpoints" in stmts[0] and "ON CONFLICT (workspace_id) DO NOTHING" in stmts[0]
    assert stmts[1].startswith("SELECT") and stmts[1].endswith("FOR UPDATE")
    session.add.assert_not_called()
    session.commit.assert_not_called()
