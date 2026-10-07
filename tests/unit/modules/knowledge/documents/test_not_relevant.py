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


def _interaction_session(row: DocumentInteraction | None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=object())
    session.get = AsyncMock(return_value=row)
    return session


async def _patch(session: AsyncMock, version_id, **fields):  # type: ignore[no-untyped-def]
    projection = SimpleNamespace(version_number=1, document_version_id=version_id)
    with patch.object(public, "get_news_document_projection", AsyncMock(return_value=projection)):
        return await public.set_gadget_document_interaction(
            session, owner_id=1, document_id=uuid4(), version_number=1,
            payload=GadgetDocumentInteractionPatch(**fields),
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
         patch.object(public, "_as_gadget_document_projection", lambda item, interaction=None: item.document_version_id),          patch.object(public, "GadgetDocumentProjectionList", lambda **kw: SimpleNamespace(**kw)):
        page = await public.list_gadget_document_projections(session, owner_id=1, source_ids=(uuid4(),), **kwargs)
    return page.items


@pytest.mark.asyncio
async def test_feed_excludes_hidden_unless_requested() -> None:
    hidden, shown = uuid4(), uuid4()
    rows = [DocumentInteraction(owner_id=1, document_version_id=hidden, dismissed_at=datetime.now(UTC))]
    assert await _feed(rows, [hidden, shown]) == [shown]
    assert await _feed(rows, [hidden, shown], include_dismissed=True) == [hidden, shown]


def test_purge_cascades_through_document_version_fk() -> None:
    fk = next(iter(DocumentInteraction.__table__.c.document_version_id.foreign_keys))
    assert fk.ondelete == "CASCADE" and fk.column.table.name == "document_versions"
    assert next(iter(DocumentInteraction.__table__.c.owner_id.foreign_keys)).ondelete == "CASCADE"


@pytest.mark.asyncio
async def test_version_export_selects_owner_scoped_interaction_state() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    captured: list[str] = []

    async def stream(statement, **_kw):  # type: ignore[no-untyped-def]
        captured.append(str(statement.compile(dialect=postgresql.dialect())))
        raise RuntimeError("stop")

    session.stream = stream
    with patch.object(public, "_require_document_export_owner", AsyncMock()), \
         patch.object(public, "_document_export_count", AsyncMock(return_value=0)), \
         pytest.raises(RuntimeError, match="stop"):
        await public.export_page(session, owner_id=7, record_kind="versions")
    sql = captured[0]
    assert "document_interactions.dismissed_at" in sql and "document_interactions.owner_id =" in sql
