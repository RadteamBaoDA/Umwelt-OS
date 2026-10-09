"""P15-T9 "Not relevant": hide/unhide, feed exclusion, purge cascade and export state."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents import public
from modules.knowledge.documents.models import DocumentInteraction
from modules.knowledge.documents.schemas import GadgetDocumentInteractionPatch
from tests.unit.modules.knowledge.documents._scope import FENCE, SCOPE, SCOPE_KW


def _interaction_session(row: DocumentInteraction | None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=object())
    session.get = AsyncMock(return_value=row)
    return session


async def _patch(session: AsyncMock, version_id, **fields):  # type: ignore[no-untyped-def]
    projection = SimpleNamespace(version_number=1, document_version_id=version_id)
    with patch.object(public, "get_news_document_projection", AsyncMock(return_value=projection)), \
            patch.object(public, "_admit_document_scope", AsyncMock(return_value=FENCE)), \
            patch.object(public, "commit_with_replay", AsyncMock()), \
            patch.object(public.sources, "lock_source_for_document", AsyncMock()):
        return await public.set_gadget_document_interaction(
            session, document_id=uuid4(), version_number=1,
            payload=GadgetDocumentInteractionPatch(**fields), **SCOPE_KW,
        )


def test_patch_accepts_dismissed_only_and_rejects_empty() -> None:
    assert GadgetDocumentInteractionPatch(dismissed=True).dismissed is True
    with pytest.raises(ValueError):
        GadgetDocumentInteractionPatch()


@pytest.mark.asyncio
async def test_hide_creates_row_and_is_idempotent() -> None:
    version_id = uuid4()
    session = _interaction_session(None)
    result = await _patch(session, version_id, dismissed=True)
    assert result is not None and result.dismissed_at is not None
    created = session.add.call_args.args[0]
    assert created.dismissed_at is not None and created.read_at is None
    assert created.owner_id == FENCE.user_id  # the admitted actor, never a client-supplied owner
    # Hiding again keeps the row and leaves read/saved untouched.
    again = await _patch(_interaction_session(created), version_id, dismissed=True)
    assert again is not None and again.dismissed_at is not None


@pytest.mark.asyncio
async def test_unhide_deletes_empty_row_but_keeps_other_state() -> None:
    version_id = uuid4()
    now = datetime.now(UTC)
    only_hidden = DocumentInteraction(owner_id=1, document_version_id=version_id, dismissed_at=now)
    session = _interaction_session(only_hidden)
    result = await _patch(session, version_id, dismissed=False)
    assert result is not None and result.dismissed_at is None
    session.delete.assert_awaited_once_with(only_hidden)
    # Unhide twice is a no-op, not an error.
    await _patch(_interaction_session(None), version_id, dismissed=False)

    saved = DocumentInteraction(owner_id=1, document_version_id=version_id, bookmarked_at=now, dismissed_at=now)
    kept = _interaction_session(saved)
    result = await _patch(kept, version_id, dismissed=False)
    assert result is not None and result.bookmarked_at == now and result.dismissed_at is None
    kept.delete.assert_not_awaited()


def _projection(version_id):  # type: ignore[no-untyped-def]
    return SimpleNamespace(document_version_id=version_id)


async def _feed(rows: list[DocumentInteraction], versions: list, **kwargs):  # type: ignore[no-untyped-def]
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    with patch.object(public, "list_news_document_projections", AsyncMock(return_value=([_projection(v) for v in versions], None))), \
         patch.object(public, "_as_gadget_document_projection", lambda item, interaction=None: item.document_version_id), \
         patch.object(public, "_admit_document_scope", AsyncMock(return_value=FENCE)), \
         patch.object(public, "GadgetDocumentProjectionList", lambda **kw: SimpleNamespace(**kw)):
        page = await public.list_gadget_document_projections(session, source_ids=(uuid4(),), **SCOPE_KW, **kwargs)
    return page.items


@pytest.mark.asyncio
async def test_feed_excludes_hidden_unless_requested() -> None:
    hidden, shown = uuid4(), uuid4()
    rows = [DocumentInteraction(owner_id=1, document_version_id=hidden, dismissed_at=datetime.now(UTC))]
    assert await _feed(rows, [hidden, shown]) == [shown]
    assert await _feed(rows, [hidden, shown], include_dismissed=True) == [hidden, shown]


@pytest.mark.asyncio
async def test_version_export_maps_interaction_fields_and_scopes_join() -> None:
    session = AsyncMock()
    captured: list[str] = []
    read_at, saved_at, hidden_at = (datetime(2026, 1, d, tzinfo=UTC) for d in (1, 2, 3))
    columns = {
        "version_id": uuid4(), "document_id": uuid4(), "source_id": uuid4(), "source_status": "active",
        "source_generation": 1, "version_number": 1, "document_current_version": 1,
        "version_content": "x", "version_observed_at": read_at, "version_created_at": read_at,
        "provider_id": None, "document_created_at": read_at, "document_updated_at": read_at,
        "version_content_hash": "a" * 64, "interaction_read_at": read_at,
        "interaction_bookmarked_at": saved_at, "interaction_dismissed_at": hidden_at,
    }

    class _Result:
        def mappings(self):  # type: ignore[no-untyped-def]
            async def rows():  # type: ignore[no-untyped-def]
                yield columns
            return rows()

        async def close(self) -> None:
            return None

    async def stream(statement, **_kw):  # type: ignore[no-untyped-def]
        captured.append(str(statement.compile(dialect=postgresql.dialect())))
        return _Result()

    session.stream = stream
    with patch.object(public, "_require_document_export_owner", AsyncMock(return_value=FENCE)), \
         patch.object(public, "_document_export_count", AsyncMock(return_value=1)):
        page = await public.export_page(session, owner_id=SCOPE.user_id, record_kind="versions", **SCOPE_KW)
    item = page.items[0]
    assert (item.read_at, item.bookmarked_at, item.dismissed_at) == (read_at, saved_at, hidden_at)
    assert "document_interactions.owner_id =" in captured[0]


@pytest.mark.asyncio
async def test_dashboard_projection_route_forwards_include_dismissed() -> None:
    from modules.knowledge.documents import routes

    forward = AsyncMock(return_value="page")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(multi_workspace_enabled=False))))
    with patch.object(routes.public, "list_gadget_document_projections", forward):
        for flag in (False, True):
            await routes.list_dashboard_projections(
                session=AsyncMock(), request=request, workspace=SCOPE, source_ids=[uuid4()],
                channel_ids=None, limit=50, cursor=None, language="vi", since=None, include_dismissed=flag,
            )
            assert forward.await_args.kwargs["include_dismissed"] is flag
            assert forward.await_args.kwargs["language"] == "vi" and forward.await_args.kwargs["scope"] == SCOPE
