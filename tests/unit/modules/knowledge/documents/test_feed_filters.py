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
from modules.knowledge.documents._news import _extract_media_projection

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
        session,
        source_ids=(uuid4(),),
        language="vi",
        since=since,
        **KW,
    )
    sql = _sql(session)
    # Privacy fences stay in the same statement as the new filters.
    assert "sources.status" in sql and "extraction_status IN" in sql and "source_id IN" in sql
    assert "documents.language = " in sql and "coalesce(documents.observed_at" in sql


@pytest.mark.asyncio
async def test_no_filters_leaves_language_unconstrained_so_unknown_matches_any() -> None:
    session = _session()
    await public.list_gadget_document_projections(session, source_ids=(uuid4(),), **KW)
    sql = _sql(session)
    assert "documents.language" not in sql.split("WHERE")[1]
    assert "normalized_version_provenance" not in sql


def test_media_projection_preserves_landscape_artwork_aspect() -> None:
    media = _extract_media_projection(
        SimpleNamespace(source_fields={"media_type": "game", "artwork_aspect": "landscape"})
    )
    assert media is not None
    assert media.artwork_aspect == "landscape"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["xx", "EN", "en-US", "'; --", ""])
async def test_language_must_be_allowlisted(language: str) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(),
            source_ids=(uuid4(),),
            language=language,
            **KW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [timedelta(days=400), timedelta(days=-1)])
async def test_since_must_be_within_bounded_window(delta: timedelta) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(),
            source_ids=(uuid4(),),
            since=datetime.now(UTC) - delta,
            **KW,
        )


@pytest.mark.asyncio
async def test_since_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(),
            source_ids=(uuid4(),),
            since=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
            **KW,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN-us", "en"),
        ("vi_VN", "vi"),
        (" vi ", "vi"),
        ("xx", None),
        ("", None),
        (None, None),
        (5, None),
        ("'; --", None),
    ],
)
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
        source_id=source_id,
        provider_id="p1",
        content="x",
        title="t",
        provenance={"metadata": {"language": "VI-vn"}},
        observed_at=datetime.now(UTC),
        accepted_record_hash="h",
        telegram_order=None,
        content_type="text/plain",
        canonical_url=None,
        published_at=None,
    )
    projection = SimpleNamespace(provider="rss")
    identity = SimpleNamespace(tombstoned_at=None, document_id=None)
    with pytest.raises(_Stop):
        await public._apply_normalized_document(session, payload, projection, None, identity, WS)  # type: ignore[arg-type]
    assert captured[0].language == "vi" and captured[0].workspace_id == WS


@pytest.mark.asyncio
async def test_cursor_for_one_language_is_rejected_for_another() -> None:
    fp = {
        "access_fence": FENCE,
        "source_ids": (uuid4(),),
        "observed_since": None,
        "channel_ids": None,
    }
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
        session,
        source_ids=(uuid4(),),
        channel_ids=("123",),
        language="en",
        **KW,
    )
    sql = _sql(session)
    assert (
        "documents.language = " in sql
        and "sources.provider" in sql
        and "normalized_version_provenance" in sql
    )


@pytest.mark.asyncio
async def test_media_filters_compile_jsonb_predicates_and_release_date_sort() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session,
        source_ids=(uuid4(),),
        countries=("JP", "KR"),
        genres=("action", "sci_fi"),
        languages=("ja",),
        media_types=("anime",),
        formats=("tv",),
        content_ratings=("pg_13",),
        release_statuses=("airing",),
        release_year=2026,
        release_year_from=2025,
        release_year_to=2027,
        release_season="fall",
        min_rating=80.0,
        release_window_days=14,
        sort="release_date_asc",
        **KW,
    )
    sql = _sql(session)
    assert "normalized_version_provenance" in sql
    assert "jsonb_typeof" in sql
    assert "pg_input_is_valid" in sql
    assert "CASE WHEN (jsonb_typeof" in sql and " AS NUMERIC)" in sql
    assert "@>" in sql
    assert "ORDER BY" in sql and "ASC" in sql


@pytest.mark.asyncio
async def test_min_rating_binds_exact_decimal_not_float_expansion() -> None:
    from decimal import Decimal

    session = _session()
    await public.list_gadget_document_projections(
        session, source_ids=(uuid4(),), min_rating=7.7, **KW
    )
    stmt = session.execute.await_args.args[0]
    params = stmt.compile().params
    assert Decimal("7.7") in params.values()
    assert not any(isinstance(v, float) and v == 7.7 for v in params.values())


@pytest.mark.asyncio
async def test_media_type_filter_includes_legacy_youtube_provider_fallback() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session, source_ids=(uuid4(),), media_types=("youtube",), **KW
    )
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "CASE WHEN" in str(compiled) and "jsonb_typeof" in str(compiled)
    assert "provider" in compiled.params.values()
    assert "youtube" in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"countries": ("USA",)},
        {"genres": ("Bad-Genre",)},
        {"release_year": 1799},
        {"release_year_from": 2027, "release_year_to": 2025},
        {"min_rating": 101.0},
        {"release_window_days": 91},
        {"sort": "rating_desc"},
    ],
)
async def test_invalid_media_filters_raise_value_error(bad_kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(),
            source_ids=(uuid4(),),
            **bad_kwargs,
            **KW,
        )


def test_cursor_fingerprint_changes_for_every_media_filter_and_sort() -> None:
    base_kw: dict[str, Any] = {
        "access_fence": FENCE,
        "source_ids": (uuid4(),),
        "observed_since": None,
        "channel_ids": None,
        "language": None,
    }
    base = public._news_projection_cursor_fingerprint(**base_kw)
    for override in (
        {"countries": ("JP",)},
        {"genres": ("action",)},
        {"languages": ("ja",)},
        {"media_types": ("anime",)},
        {"formats": ("tv",)},
        {"content_ratings": ("pg_13",)},
        {"release_statuses": ("airing",)},
        {"release_year": 2026},
        {"release_year_from": 2025},
        {"release_year_to": 2027},
        {"release_season": "fall"},
        {"min_rating": 80.0},
        {"release_window_days": 14},
        {"sort": "release_date_asc"},
    ):
        assert public._news_projection_cursor_fingerprint(**base_kw, **override) != base


def test_as_gadget_document_projection_populates_media_and_youtube() -> None:
    now = datetime.now(UTC)

    def item(provider: str, sf: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            document_id=uuid4(),
            document_version_id=uuid4(),
            version_number=1,
            source_id=uuid4(),
            title="Item",
            canonical_url="https://example.com/1",
            published_at=now,
            observed_at=now,
            chunks=[],
            metadata_is_version_snapshot=True,
            local_only=False,
            provider_metadata=SimpleNamespace(provider=provider, source_fields=sf, telegram=None),
        )

    media_dto = public._as_gadget_document_projection(
        item(
            "anilist_anime",
            {
                "media_type": "anime",
                "artwork_aspect": "poster",
                "country": "JP",
                "language": "ja",
                "genres": ["action"],
                "rating": 86.0,
                "release_year": 2026,
                "external_ids": {"anilist": "21"},
            },
        )
    )
    assert media_dto.media is not None
    assert media_dto.media.media_type == "anime"
    assert media_dto.media.country == "JP"
    assert media_dto.media.rating == 86.0

    yt_dto = public._as_gadget_document_projection(
        item("youtube", {"author": "3Blue1Brown", "title": "Math"})
    )
    assert yt_dto.media is not None
    assert yt_dto.media.media_type == "youtube"
    assert yt_dto.media.artwork_aspect == "video"
    assert yt_dto.media.creator == "3Blue1Brown"

    news_dto = public._as_gadget_document_projection(item("bbc_world", {"title": "News"}))
    assert news_dto.media is None


@pytest.mark.asyncio
async def test_release_date_asc_admits_single_provenance_versions_and_default_window() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session, source_ids=(uuid4(),), sort="release_date_asc", **KW
    )
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # One provenance row per version (no duplicate join rows) and a bounded default window.
    assert "count(" in sql
    assert 1 in compiled.params.values()
    assert sql.count(" >= ") >= 1 and sql.count(" <= ") >= 1
    assert any(isinstance(v, datetime) for v in compiled.params.values())


@pytest.mark.asyncio
async def test_release_season_uses_enum_equality_not_like() -> None:
    session = _session()
    await public.list_gadget_document_projections(
        session, source_ids=(uuid4(),), release_season="fall", **KW
    )
    sql = _sql(session)
    assert "LIKE" not in sql.upper().replace("ILIKE", "")
    with pytest.raises(ValueError):
        await public.list_gadget_document_projections(
            _session(), source_ids=(uuid4(),), release_season="fa%", **KW
        )


@pytest.mark.asyncio
async def test_contract_invalid_provider_record_is_skipped_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored provider_record violating the current contract hydrates to None, not a 422."""
    from modules.knowledge.documents import _news

    doc = SimpleNamespace(id=uuid4(), external_id=None, canonical_url=None, title="t")
    doc.published_at = doc.observed_at = None
    version = SimpleNamespace(
        id=uuid4(), version_number=1, content_hash="h", created_at=datetime.now(UTC)
    )
    version.observed_at = datetime.now(UTC)
    source = SimpleNamespace(
        id=uuid4(), generation=1, name="s", type="api", provider="p", local_only=False
    )
    provenance = SimpleNamespace(
        provenance_json={
            "provider_record": {
                "provider": "tvmaze_schedule",
                "identity": "x",
                "timestamp_basis": "collection",
                "coverage": "returned_snapshot",
                "content_truncated": False,
                "source_fields": {"release_date": "2026-02-30"},
            }
        },
        canonical_url=None,
        title="t",
        published_at=None,
        selection_observed_at=datetime.now(UTC),
        accepted_record_hash="a",
        normalization_version=1,
    )
    monkeypatch.setattr(
        _news,
        "_current_document_components",
        AsyncMock(return_value=(doc, version, source, provenance)),
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(all=MagicMock(return_value=[(uuid4(), 0, "chunk")]))
    )
    with caplog.at_level("WARNING"):
        result = await _news.get_news_document_projection(session, doc.id, **KW)
    assert result is None
    assert "ValidationError" in caplog.text and str(doc.id) in caplog.text
    assert "2026-02-30" not in caplog.text
