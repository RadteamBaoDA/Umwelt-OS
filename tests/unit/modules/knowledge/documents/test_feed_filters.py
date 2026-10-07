"""P15-T4 feed filters: language/since validation and composition with the privacy filters."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents import public


def _session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return session


def _sql(session: AsyncMock) -> str:
    return str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_language_and_since_compose_with_privacy_filters() -> None:
    session = _session()
    since = datetime.now(UTC) - timedelta(days=7)
    await public.list_gadget_document_projections(
        session, owner_id=1, source_ids=(uuid4(),), language="vi", since=since,
    )
    sql = _sql(session)
    # Privacy fences stay in the same statement as the new filters.
    assert "sources.status" in sql and "extraction_status IN" in sql and "source_id IN" in sql
    assert "documents.language = " in sql and "coalesce(documents.observed_at" in sql


@pytest.mark.asyncio
async def test_no_filters_leaves_language_unconstrained_so_unknown_matches_any() -> None:
    session = _session()
    await public.list_gadget_document_projections(session, owner_id=1, source_ids=(uuid4(),))
    assert "documents.language" not in _sql(session).split("WHERE")[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["xx", "EN", "en-US", "'; --", ""])
async def test_language_must_be_allowlisted(language: str) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), owner_id=1, source_ids=(uuid4(),), language=language,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [timedelta(days=400), timedelta(days=-1)])
async def test_since_must_be_within_bounded_window(delta: timedelta) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), owner_id=1, source_ids=(uuid4(),), since=datetime.now(UTC) - delta,
        )


@pytest.mark.asyncio
async def test_since_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), owner_id=1, source_ids=(uuid4(),), since=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
        )


@pytest.mark.parametrize(("raw", "expected"), [
    ("en", "en"), ("EN-us", "en"), ("vi_VN", "vi"), (" vi ", "vi"),
    ("xx", None), ("", None), (None, None), (5, None), ("'; --", None),
])
def test_normalize_document_language(raw: object, expected: str | None) -> None:
    assert public.normalize_document_language(raw) == expected


def test_provenance_language_reads_metadata_and_ignores_garbage() -> None:
    assert public._provenance_language({"metadata": {"language": "vi-VN"}}) == "vi"
    for bad in ({}, {"metadata": None}, {"metadata": {"language": "zz"}}, None, "x"):
        assert public._provenance_language(bad) is None


@pytest.mark.asyncio
async def test_upsert_normalized_document_writes_language_on_create(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.knowledge.documents.models import Document

    source = MagicMock(status="active", generation=1)
    source.id = uuid4()
    projection = MagicMock(status="active", generation=1, provider="rss")
    projection.id = source.id
    monkeypatch.setattr(public.sources, "lock_source", AsyncMock(return_value=source))
    monkeypatch.setattr(public.sources, "get_connector_source", AsyncMock(return_value=projection))
    captured: list[Document] = []
    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: captured.append(obj) if isinstance(obj, Document) else None)
    session.scalar = AsyncMock(return_value=None)
    payload = MagicMock(
        source_id=source.id, expected_source_generation=1, content="x", title="t",
        provenance={"metadata": {"language": "VI-vn"}},
    )
    await public.upsert_normalized_document(session, payload)
    assert captured and captured[0].language == "vi"


@pytest.mark.asyncio
async def test_language_and_channel_ids_compose_in_one_where() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session, owner_id=1, source_ids=(uuid4(),), channel_ids=("123",), language="en",
    )
    sql = _sql(session)
    assert "documents.language = " in sql and "sources.provider" in sql and "normalized_version_provenance" in sql
