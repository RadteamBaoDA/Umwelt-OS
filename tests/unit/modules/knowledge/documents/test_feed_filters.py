"""P15-T4 feed filters: language/since validation and composition with the privacy filters."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents import public

WS = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)
FENCE = SimpleNamespace(workspace_id=WS, user_id=7, membership_revision=3, configuration_revision=1)
KW: dict[str, Any] = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public, "_admit_document_scope", AsyncMock(return_value=FENCE))


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
        session, source_ids=(uuid4(),), language="vi", since=since, **KW,
    )
    sql = _sql(session)
    # Privacy fences stay in the same statement as the new filters.
    assert "sources.status" in sql and "extraction_status IN" in sql and "source_id IN" in sql
    assert "documents.language = " in sql and "coalesce(documents.observed_at" in sql


@pytest.mark.asyncio
async def test_no_filters_leaves_language_unconstrained_so_unknown_matches_any() -> None:
    session = _session()
    await public.list_gadget_document_projections(session, source_ids=(uuid4(),), **KW)
    assert "documents.language" not in _sql(session).split("WHERE")[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["xx", "EN", "en-US", "'; --", ""])
async def test_language_must_be_allowlisted(language: str) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), source_ids=(uuid4(),), language=language, **KW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [timedelta(days=400), timedelta(days=-1)])
async def test_since_must_be_within_bounded_window(delta: timedelta) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), source_ids=(uuid4(),), since=datetime.now(UTC) - delta, **KW,
        )


@pytest.mark.asyncio
async def test_since_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), source_ids=(uuid4(),), since=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1), **KW,
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


class _Stop(Exception):
    """Raised once the new Document is staged, to stop before chunking and embeddings."""


@pytest.mark.asyncio
async def test_apply_normalized_document_writes_language_on_create() -> None:
    from modules.knowledge.documents.models import Document

    captured: list[Document] = []

    def add(obj: object) -> None:
        if isinstance(obj, Document):
            captured.append(obj)
            raise _Stop

    session = AsyncMock()
    session.add = MagicMock(side_effect=add)
    session.scalar = AsyncMock(return_value=None)
    source_id = uuid4()
    payload = MagicMock(
        source_id=source_id, provider_id="p1", content="x", title="t",
        provenance={"metadata": {"language": "VI-vn"}}, observed_at=datetime.now(UTC),
        accepted_record_hash="h", telegram_order=None, content_type="text/plain",
        canonical_url=None, published_at=None,
    )
    projection = SimpleNamespace(provider="rss")
    identity = SimpleNamespace(tombstoned_at=None, document_id=None)
    with pytest.raises(_Stop):
        await public._apply_normalized_document(session, payload, projection, None, identity, WS)  # type: ignore[arg-type]
    assert captured[0].language == "vi" and captured[0].workspace_id == WS


@pytest.mark.asyncio
async def test_cursor_for_one_language_is_rejected_for_another() -> None:
    fp = {"access_fence": FENCE, "source_ids": (uuid4(),), "observed_since": None, "channel_ids": None}
    en = public._news_projection_cursor_fingerprint(**fp, language="en")  # type: ignore[arg-type]
    vi = public._news_projection_cursor_fingerprint(**fp, language="vi")  # type: ignore[arg-type]
    assert en != vi
    cursor = public._encode_news_projection_cursor(datetime.now(UTC), uuid4(), en)
    with pytest.raises(ValueError):
        public._decode_news_projection_cursor(cursor, vi)


@pytest.mark.asyncio
async def test_language_and_channel_ids_compose_in_one_where() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session, source_ids=(uuid4(),), channel_ids=("123",), language="en", **KW,
    )
    sql = _sql(session)
    assert "documents.language = " in sql and "sources.provider" in sql and "normalized_version_provenance" in sql
