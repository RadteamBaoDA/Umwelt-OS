"""News/gadget projections of current ready versions, interactions and selection fences."""

import base64
import binascii
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Numeric,
    Select,
    and_,
    case,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_encode, fingerprint_payload
from core.realtime import commit_with_replay
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._language import FEED_LANGUAGES
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentInteraction,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    GadgetDocumentInteractionPatch,
    GadgetDocumentInteractionRead,
    GadgetDocumentProjectionList,
    GadgetDocumentProjectionRead,
    GadgetDocumentSelectionFence,
    GadgetHighlightProjectionPage,
    GadgetProviderMetadataRead,
    GadgetTelegramMediaRead,
    GadgetTelegramRecordRead,
    MediaProjectionRead,
    ProviderRecordMetadata,
)
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source


@dataclass(frozen=True)
class NewsChunkProjection:
    """Expose bounded immutable chunk identity and text to the News owner."""

    id: UUID
    index: int
    content: str


@dataclass(frozen=True)
class NewsDocumentProjection:
    """Carry a detached current-version snapshot for owner-authorized News reads."""

    document_id: UUID
    document_version_id: UUID
    version_number: int
    source_id: UUID
    current_source_generation: int
    source_name: str
    source_type: str
    provider: str | None
    local_only: bool
    canonical_url: str | None
    content_hash: str
    provider_item_id: str | None
    scope_discriminator: str | None
    title: str
    published_at: datetime | None
    observed_at: datetime
    created_at: datetime
    chunks: tuple[NewsChunkProjection, ...]
    metadata_is_version_snapshot: bool
    accepted_record_hash: str | None
    normalization_version: int | None
    chunk_count: int
    chunks_truncated: bool
    provider_metadata: ProviderRecordMetadata | None


@dataclass(frozen=True)
class NewsProjectionStatus:
    """Expose bounded non-sensitive reasons that selected current evidence is incomplete."""

    incomplete_reasons: tuple[str, ...]


async def get_news_document_projection(
    session: AsyncSession,
    document_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_source_generation: int | None = None,
) -> NewsDocumentProjection | None:
    """Return a current, ready document projection only under its active source generation.

    The caller must first authorize the source through the Sources public contract.
    This read excludes deleted, paused, replaced, or incomplete versions and caps
    chunks at 100. Legacy/manual documents remain readable with explicitly
    non-snapshot metadata provenance rather than inferred historical metadata.
    """
    components = await _current_document_components(
        session,
        document_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_source_generation=expected_source_generation,
    )
    if components is None:
        return None
    document, version, source, provenance = components
    chunk_rows = list(
        (
            await session.execute(
                select(DocumentChunk.id, DocumentChunk.chunk_index, DocumentChunk.content)
                .where(DocumentChunk.document_version_id == version.id)
                .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
                .limit(101)
            )
        ).all()
    )
    if not chunk_rows:
        return None
    chunk_count = (
        len(chunk_rows)
        if len(chunk_rows) <= 100
        else int(
            await session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_version_id == version.id)
            )
            or 0
        )
    )
    chunks_truncated = len(chunk_rows) > 100
    chunk_rows = chunk_rows[:100]
    return NewsDocumentProjection(
        document_id=document.id,
        document_version_id=version.id,
        version_number=version.version_number,
        source_id=source.id,
        current_source_generation=source.generation,
        source_name=source.name,
        source_type=source.type,
        provider=source.provider,
        local_only=source.local_only,
        canonical_url=provenance.canonical_url if provenance else document.canonical_url,
        content_hash=version.content_hash,
        provider_item_id=(document.external_id[:512] if document.external_id else None),
        scope_discriminator=(
            cast("str | None", provenance.provenance_json.get("provider_scope_discriminator"))
            if provenance
            else None
        ),
        title=provenance.title if provenance else document.title,
        published_at=provenance.published_at if provenance else document.published_at,
        observed_at=provenance.selection_observed_at
        if provenance
        else (document.observed_at or version.observed_at),
        created_at=version.created_at,
        chunks=tuple(
            NewsChunkProjection(id=identifier, index=index, content=content[:20_000])
            for identifier, index, content in chunk_rows
        ),
        metadata_is_version_snapshot=provenance is not None,
        accepted_record_hash=provenance.accepted_record_hash if provenance else None,
        normalization_version=provenance.normalization_version if provenance else None,
        chunk_count=chunk_count,
        chunks_truncated=chunks_truncated,
        provider_metadata=(
            ProviderRecordMetadata.model_validate(provenance.provenance_json["provider_record"])
            if provenance and provenance.provenance_json.get("provider_record") is not None
            else None
        ),
    )


async def _current_document_components(
    session: AsyncSession,
    document_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_source_generation: int | None = None,
    admitted: bool = False,
) -> tuple[Document, DocumentVersion, Source, NormalizedVersionProvenance | None] | None:
    """Resolve one active current revision and verify its accepted provider scope without reading chunks.

    The Documents owner uses this shared gate for content projections and exact selections so a
    normalized record cannot inherit a new source generation or mutable provider configuration.
    """
    if not admitted:
        await _admit_document_scope(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    row = (
        await session.execute(
            select(Document, DocumentVersion, Source)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.id == document_id,
                *_document_scope(scope),
                Document.current_version == DocumentVersion.version_number,
                Document.extraction_status.in_(("ready", "succeeded")),
                Source.status == "active",
                *(
                    [Source.generation == expected_source_generation]
                    if expected_source_generation is not None
                    else []
                ),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    document, version, source = row
    provenance_rows = list(
        (
            await session.scalars(
                select(NormalizedVersionProvenance)
                .where(
                    NormalizedVersionProvenance.document_version_id == version.id,
                )
                .order_by(
                    NormalizedVersionProvenance.accepted_record_hash,
                    NormalizedVersionProvenance.normalization_version,
                )
                .limit(2)
            )
        ).all()
    )
    if provenance_rows and (
        len(provenance_rows) != 1 or provenance_rows[0].source_generation != source.generation
    ):
        # Normalized data cannot fall back to mutable legacy metadata when its
        # accepted generation is stale or its immutable identity is ambiguous.
        return None
    if len(provenance_rows) > 1:
        return None
    if provenance_rows:
        provenance = provenance_rows[0]
        if source.type in {"rss", "web", "api"}:
            from modules.connectors import public as connectors

            current_scope = await connectors.get_current_provider_scope(
                session,
                source.id,
                source.generation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            accepted_scope = provenance.provenance_json.get("provider_scope_discriminator")
            if (
                current_scope is None
                or not isinstance(accepted_scope, str)
                or len(accepted_scope) != 64
                or accepted_scope != current_scope.discriminator
            ):
                return None
    else:
        if source.type in {"rss", "web", "api"}:
            # Provider documents without immutable acceptance provenance cannot
            # establish their current scope and must not use mutable legacy fields.
            return None
        provenance = None
    return document, version, source, provenance


async def validate_gadget_document_selection_fences(
    session: AsyncSession,
    fences: tuple[GadgetDocumentSelectionFence, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock_rows: bool = True,
    max_documents: int = 32,
) -> bool:
    """Revalidate exact selected versions and provider policy before content or remote use.

    Selection count, source count, and lock order are bounded. The original admitted fence is
    compared while Source rows are share-locked in UUID order before document rows; accepted
    provenance is then checked against live provider scope. Locks last through caller transaction end.
    """
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return await _validate_gadget_document_selection_fences_admitted(
        session,
        fences,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
        lock_rows=lock_rows,
        max_documents=max_documents,
    )


async def news_retained_observation_allowed(
    session: AsyncSession,
    *,
    document_id: UUID,
    source_id: UUID,
    expected_source_generation: int,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Confirm a retained News count still belongs to the current active source scope.

    This boolean fence deliberately does not return historical or current text and
    does not require the observed historical version to remain current. It checks
    the document's present ready revision, active source generation, and current
    provider scope without loading chunks or exposing metadata to News.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    rows: list[Any] = list(
        (
            await session.execute(
                select(
                    Source.type,
                    Source.generation,
                    NormalizedVersionProvenance.source_generation,
                    NormalizedVersionProvenance.provenance_json,
                )
                .join(Document, Document.source_id == Source.id)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .outerjoin(
                    NormalizedVersionProvenance,
                    NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                )
                .where(
                    Document.id == document_id,
                    Document.source_id == source_id,
                    *_document_scope(scope),
                    Document.current_version == DocumentVersion.version_number,
                    Document.extraction_status.in_(("ready", "succeeded")),
                    Source.status == "active",
                    Source.generation == expected_source_generation,
                )
                .order_by(NormalizedVersionProvenance.id)
                .limit(2)
            )
        ).all()
    )
    if len(rows) != 1:
        return False
    source_type, generation, accepted_generation, provenance = rows[0]
    if provenance is not None and accepted_generation != generation:
        return False
    if source_type not in {"rss", "web", "api"}:
        return True
    if provenance is None:
        return False
    accepted_scope = provenance.get("provider_scope_discriminator")
    if not isinstance(accepted_scope, str) or len(accepted_scope) != 64:
        return False
    from modules.connectors import public as connectors

    current_scope = await connectors.get_current_provider_scope(
        session,
        source_id,
        expected_source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_scope is not None and current_scope.discriminator == accepted_scope


async def news_projection_scope_unavailable(
    session: AsyncSession,
    document_id: UUID,
    expected_source_generation: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Report only whether a supported provider scope fence prevents News evidence output.

    The result contains no source configuration, item identifiers, or counts. Deleted,
    replaced, inactive, or stale-generation documents are not classified as scope failures.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    row = (
        await session.execute(
            select(Document, DocumentVersion, Source)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.id == document_id,
                *_document_scope(scope),
                Document.current_version == DocumentVersion.version_number,
                Document.extraction_status.in_(("ready", "succeeded")),
                Source.status == "active",
                Source.generation == expected_source_generation,
            )
        )
    ).one_or_none()
    if row is None:
        return False
    _document, version, source = row
    if source.type not in {"rss", "web", "api"}:
        return False
    provenance_rows = list(
        (
            await session.scalars(
                select(NormalizedVersionProvenance)
                .where(
                    NormalizedVersionProvenance.document_version_id == version.id,
                )
                .order_by(
                    NormalizedVersionProvenance.accepted_record_hash,
                    NormalizedVersionProvenance.normalization_version,
                )
                .limit(2)
            )
        ).all()
    )
    if len(provenance_rows) != 1 or provenance_rows[0].source_generation != source.generation:
        return True
    accepted_scope = provenance_rows[0].provenance_json.get("provider_scope_discriminator")
    if not isinstance(accepted_scope, str) or len(accepted_scope) != 64:
        return True
    from modules.connectors import public as connectors

    current_scope = await connectors.get_current_provider_scope(
        session,
        source.id,
        source.generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_scope is None or accepted_scope != current_scope.discriminator


async def news_current_scope_status(
    session: AsyncSession,
    source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> NewsProjectionStatus:
    """Summarize current provider-scope omissions for at most 100 selected documents.

    Only fixed reason codes leave Documents; provider settings, credentials, item IDs and
    omission counts stay inside the owner module. A capped scan reports incompleteness.
    """
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids):
        raise ValueError("News scope status requires 1 to 32 unique sources")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    rows: list[Any] = list(
        (
            await session.execute(
                select(
                    Document.id,
                    Source.id,
                    Source.type,
                    Source.generation,
                    NormalizedVersionProvenance.source_generation,
                    NormalizedVersionProvenance.provenance_json,
                )
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(Source, Source.id == Document.source_id)
                .outerjoin(
                    NormalizedVersionProvenance,
                    NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                )
                .where(
                    Document.source_id.in_(source_ids),
                    *_document_scope(scope),
                    Document.current_version == DocumentVersion.version_number,
                    Document.extraction_status.in_(("ready", "succeeded")),
                    Source.status == "active",
                )
                .order_by(Document.id, NormalizedVersionProvenance.id)
                .limit(101)
            )
        ).all()
    )
    reasons: set[str] = set()
    if len(rows) > 100:
        reasons.add("candidate_scan_limit")
    source_scope: dict[UUID, str | None] = {}
    provider_types = {"rss", "web", "api"}
    for _document_id, source_id, source_type, generation, _accepted_generation, _provenance in rows[
        :100
    ]:
        if source_type in provider_types and source_id not in source_scope:
            from modules.connectors import public as connectors

            snapshot = await connectors.get_current_provider_scope(
                session,
                source_id,
                generation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            source_scope[source_id] = snapshot.discriminator if snapshot else None
    for _document_id, source_id, source_type, generation, accepted_generation, provenance in rows[
        :100
    ]:
        if source_type not in provider_types:
            continue
        accepted_scope = (
            provenance.get("provider_scope_discriminator") if isinstance(provenance, dict) else None
        )
        if (
            source_scope.get(source_id) is None
            or accepted_generation != generation
            or not isinstance(accepted_scope, str)
            or accepted_scope != source_scope[source_id]
        ):
            reasons.add("scope_unavailable")
    return NewsProjectionStatus(incomplete_reasons=tuple(sorted(reasons)))


_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")
_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")
_ARTWORK_ASPECTS = frozenset({"poster", "square", "video"})


def _validate_token_tuple(
    values: tuple[str, ...] | None,
    *,
    name: str,
    max_len: int,
    pattern: re.Pattern[str],
    max_item_len: int = 32,
) -> tuple[str, ...] | None:
    """Validate an optional bounded tuple of unique regex-matched filter tokens."""
    if values is None:
        return None
    if not values:
        return None
    if (
        len(values) > max_len
        or len(set(values)) != len(values)
        or any(
            not isinstance(item, str) or len(item) > max_item_len or pattern.fullmatch(item) is None
            for item in values
        )
    ):
        raise ValueError(f"Invalid {name} filter")
    return values


def _validate_media_filter_params(
    *,
    countries: tuple[str, ...] | None,
    genres: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
    media_types: tuple[str, ...] | None,
    formats: tuple[str, ...] | None,
    content_ratings: tuple[str, ...] | None,
    release_statuses: tuple[str, ...] | None,
    release_year: int | None,
    release_year_from: int | None,
    release_year_to: int | None,
    release_season: str | None,
    min_rating: float | None,
    release_window_days: int | None,
    sort: str,
) -> None:
    """Validate multi-dimensional media projection filters and sort bounds before SQL assembly."""
    _validate_token_tuple(countries, name="countries", max_len=24, pattern=_COUNTRY_CODE_RE, max_item_len=2)
    _validate_token_tuple(genres, name="genres", max_len=16, pattern=_SLUG_RE, max_item_len=48)
    _validate_token_tuple(languages, name="languages", max_len=16, pattern=_LANG_TAG_RE, max_item_len=16)
    _validate_token_tuple(media_types, name="media_types", max_len=13, pattern=_SLUG_RE)
    _validate_token_tuple(formats, name="formats", max_len=16, pattern=_SLUG_RE)
    _validate_token_tuple(content_ratings, name="content_ratings", max_len=8, pattern=_SLUG_RE)
    _validate_token_tuple(release_statuses, name="release_statuses", max_len=8, pattern=_SLUG_RE)
    for label, yr in (
        ("release_year", release_year),
        ("release_year_from", release_year_from),
        ("release_year_to", release_year_to),
    ):
        if yr is not None and (type(yr) is not int or not 1800 <= yr <= 2100):
            raise ValueError(f"Invalid {label} filter")
    if release_year_from is not None and release_year_to is not None and release_year_from > release_year_to:
        raise ValueError("release_year_from must not exceed release_year_to")
    if release_season is not None and (not isinstance(release_season, str) or not 1 <= len(release_season.strip()) <= 32):
        raise ValueError("Invalid release_season filter")
    if min_rating is not None and (
        isinstance(min_rating, bool)
        or not isinstance(min_rating, (int, float))
        or not math.isfinite(float(min_rating))
        or not 0.0 <= float(min_rating) <= 100.0
    ):
        raise ValueError("Invalid min_rating filter")
    if release_window_days is not None and (
        type(release_window_days) is not int or not 1 <= release_window_days <= 90
    ):
        raise ValueError("Invalid release_window_days filter")
    if sort not in {"created_at_desc", "release_date_asc"}:
        raise ValueError("Invalid sort order")


def _release_date_ts_expr(sf: Any) -> Any:
    """Parse supported release dates safely, falling back to publication or creation time."""
    rd = sf["release_date"].astext
    # Legacy values may match the shape but still contain invalid calendar dates.
    normalized = case(
        (
            rd.op("~")(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"),
            rd,
        ),
        (rd.op("~")(r"^\d{4}-\d{2}-\d{2}$"), rd + "T00:00:00Z"),
        (rd.op("~")(r"^\d{4}-\d{2}$"), rd + "-01T00:00:00Z"),
        (rd.op("~")(r"^\d{4}$"), rd + "-01-01T00:00:00Z"),
        else_=None,
    )
    parsed_rd = case(
        (func.pg_input_is_valid(normalized, "timestamp with time zone"), sa_cast(normalized, DateTime(timezone=True))),
        else_=None,
    )
    return func.coalesce(parsed_rd, Document.published_at, Document.created_at)


def _media_provenance_predicates(
    *,
    countries: tuple[str, ...] | None,
    genres: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
    media_types: tuple[str, ...] | None,
    formats: tuple[str, ...] | None,
    content_ratings: tuple[str, ...] | None,
    release_statuses: tuple[str, ...] | None,
    release_year: int | None,
    release_year_from: int | None,
    release_year_to: int | None,
    release_season: str | None,
    min_rating: float | None,
) -> list[Any]:
    """Assemble JSONB ``source_fields`` predicates on ``NormalizedVersionProvenance``."""
    sf = NormalizedVersionProvenance.provenance_json["provider_record"]["source_fields"]
    preds: list[Any] = []
    for key, values in (
        ("country", countries),
        ("language", languages),
        ("format", formats),
        ("content_rating", content_ratings),
        ("release_status", release_statuses),
    ):
        if values:
            preds.append(sf[key].astext.in_(values))
    if media_types:
        # Older YouTube records have no media_type; match the projection's fallback.
        media_type = case(
            (and_(func.jsonb_typeof(sf["media_type"]) == "string", sf["media_type"].astext != ""), sf["media_type"].astext),
            (NormalizedVersionProvenance.provenance_json["provider_record"]["provider"].astext == "youtube", "youtube"),
            else_=None,
        )
        preds.append(media_type.in_(media_types))
    if release_season:
        preds.append(func.lower(sf["release_season"].astext).like(f"{release_season.strip().lower()}%"))
    if genres:
        # Use @> containment OR instead of ?| to avoid asyncpg bind-placeholder conflicts.
        preds.append(or_(*(sf["genres"].contains([g]) for g in genres)))
    year_col = sf["release_year"]
    # CASE prevents PostgreSQL from casting non-numeric legacy JSON regardless of predicate order.
    year_num = case((func.jsonb_typeof(year_col) == "number", sa_cast(year_col.astext, Numeric)), else_=None)
    if release_year is not None:
        preds.append(year_num == release_year)
    if release_year_from is not None:
        preds.append(year_num >= release_year_from)
    if release_year_to is not None:
        preds.append(year_num <= release_year_to)
    if min_rating is not None:
        rating_col = sf["rating"]
        rating_num = case((func.jsonb_typeof(rating_col) == "number", sa_cast(rating_col.astext, Numeric)), else_=None)
        preds.append(rating_num >= float(min_rating))
    return preds


def _apply_media_statement_filters(
    statement: Select[Any],
    order_ts_expr: Any,
    sf: Any,
    *,
    sort: Literal["created_at_desc", "release_date_asc"],
    release_window_days: int | None,
    media_preds: list[Any],
) -> Select[Any]:
    """Attach Provenance JSONB predicates and release-date window bounds to ``statement``."""
    window_bounds: tuple[datetime, datetime] | None = None
    if release_window_days is not None:
        now = datetime.now(UTC)
        window_bounds = (
            now - timedelta(days=min(release_window_days, 7)),
            now + timedelta(days=release_window_days),
        )
    if sort == "release_date_asc":
        statement = statement.join(
            NormalizedVersionProvenance,
            and_(
                NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                NormalizedVersionProvenance.source_generation == Source.generation,
            ),
        )
        if media_preds:
            statement = statement.where(*media_preds)
        if window_bounds is not None:
            statement = statement.where(
                order_ts_expr >= window_bounds[0], order_ts_expr <= window_bounds[1]
            )
        return statement
    if not media_preds and window_bounds is None:
        return statement
    sub_preds: list[Any] = list(media_preds)
    if window_bounds is not None:
        sub_ts = _release_date_ts_expr(sf)
        sub_preds.extend((sub_ts >= window_bounds[0], sub_ts <= window_bounds[1]))
    return statement.where(
        select(NormalizedVersionProvenance.id)
        .where(
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
            NormalizedVersionProvenance.source_generation == Source.generation,
            *sub_preds,
        )
        .exists()
    )


def _news_projection_statement(
    scope: Scope,
    source_ids: tuple[UUID, ...],
    observed_since: datetime | None,
    channel_ids: tuple[str, ...] | None,
    language: str | None,
    *,
    countries: tuple[str, ...] | None = None,
    genres: tuple[str, ...] | None = None,
    languages: tuple[str, ...] | None = None,
    media_types: tuple[str, ...] | None = None,
    formats: tuple[str, ...] | None = None,
    content_ratings: tuple[str, ...] | None = None,
    release_statuses: tuple[str, ...] | None = None,
    release_year: int | None = None,
    release_year_from: int | None = None,
    release_year_to: int | None = None,
    release_season: str | None = None,
    min_rating: float | None = None,
    release_window_days: int | None = None,
    sort: Literal["created_at_desc", "release_date_asc"] = "created_at_desc",
) -> tuple[Select[Any], Any]:
    """Build the current-version projection statement and ordering timestamp expression."""
    sf = NormalizedVersionProvenance.provenance_json["provider_record"]["source_fields"]
    order_ts_expr: Any = (
        _release_date_ts_expr(sf) if sort == "release_date_asc" else Document.created_at
    )
    statement: Select[Any] = (
        select(Document.id, order_ts_expr)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids),
            Document.current_version == DocumentVersion.version_number,
            *_document_scope(scope),
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            select(DocumentChunk.id)
            .where(DocumentChunk.document_version_id == DocumentVersion.id)
            .exists(),
        )
    )
    if observed_since is not None:
        statement = statement.where(
            func.coalesce(Document.observed_at, DocumentVersion.observed_at) >= observed_since
        )
    if language is not None:
        statement = statement.where(Document.language == language)
    if channel_ids is not None:
        statement = statement.where(
            Source.provider == "telegram",
            select(NormalizedVersionProvenance.id)
            .where(
                NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                NormalizedVersionProvenance.source_generation == Source.generation,
                NormalizedVersionProvenance.provenance_json["provider_record"]["telegram"][
                    "channel_id"
                ].astext.in_(channel_ids),
            )
            .exists(),
        )
    media_preds = _media_provenance_predicates(
        countries=countries, genres=genres, languages=languages, media_types=media_types,
        formats=formats, content_ratings=content_ratings, release_statuses=release_statuses,
        release_year=release_year, release_year_from=release_year_from,
        release_year_to=release_year_to, release_season=release_season, min_rating=min_rating,
    )
    statement = _apply_media_statement_filters(
        statement, order_ts_expr, sf,
        sort=sort, release_window_days=release_window_days, media_preds=media_preds,
    )
    return statement, order_ts_expr


async def _load_filtered_news_projections(
    session: AsyncSession,
    rows: list[Any],
    *,
    source_ids: tuple[UUID, ...],
    channel_ids: tuple[str, ...] | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[NewsDocumentProjection]:
    """Hydrate ``NewsDocumentProjection`` rows while preserving active source and channel fences."""
    projections: list[NewsDocumentProjection] = []
    for document_id, _order_ts in rows:
        item = await get_news_document_projection(
            session, document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if item is None or item.source_id not in source_ids:
            continue
        if channel_ids is not None and (
            item.provider_metadata is None
            or item.provider_metadata.provider != "telegram"
            or item.provider_metadata.telegram is None
            or item.provider_metadata.telegram.channel_id not in channel_ids
        ):
            continue
        projections.append(item)
    return projections


async def list_news_document_projections(
    session: AsyncSession,
    *,
    source_ids: tuple[UUID, ...],
    limit: int = 50,
    cursor: str | None = None,
    observed_since: datetime | None = None,
    channel_ids: tuple[str, ...] | None = None,
    language: str | None = None,
    countries: tuple[str, ...] | None = None,
    genres: tuple[str, ...] | None = None,
    languages: tuple[str, ...] | None = None,
    media_types: tuple[str, ...] | None = None,
    formats: tuple[str, ...] | None = None,
    content_ratings: tuple[str, ...] | None = None,
    release_statuses: tuple[str, ...] | None = None,
    release_year: int | None = None,
    release_year_from: int | None = None,
    release_year_to: int | None = None,
    release_season: str | None = None,
    min_rating: float | None = None,
    release_window_days: int | None = None,
    sort: Literal["created_at_desc", "release_date_asc"] = "created_at_desc",
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[NewsDocumentProjection], str | None]:
    """Page bounded current ready versions from explicitly authorized active sources."""
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids) or not 1 <= limit <= 100:
        raise ValueError("News source page must contain 1 to 32 unique sources and a bounded limit")
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if language is not None and language not in FEED_LANGUAGES:
        raise ValueError("Language filter must be an allowlisted code")
    if channel_ids is not None and (
        len(channel_ids) > 32 or len(set(channel_ids)) != len(channel_ids)
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("Channel scope must contain at most 32 unique numeric identifiers")
    media_kw: dict[str, Any] = {
        "countries": countries, "genres": genres, "languages": languages,
        "media_types": media_types, "formats": formats, "content_ratings": content_ratings,
        "release_statuses": release_statuses, "release_year": release_year,
        "release_year_from": release_year_from, "release_year_to": release_year_to,
        "release_season": release_season, "min_rating": min_rating,
        "release_window_days": release_window_days, "sort": sort,
    }
    _validate_media_filter_params(**media_kw)
    fingerprint = _news_projection_cursor_fingerprint(
        access_fence=access_fence, source_ids=source_ids, observed_since=observed_since,
        channel_ids=channel_ids, language=language, **media_kw,
    )
    statement, order_ts_expr = _news_projection_statement(
        scope, source_ids, observed_since, channel_ids, language, **media_kw
    )
    if cursor:
        cursor_ts, document_cursor = _decode_news_projection_cursor(cursor, fingerprint)
        cmp = tuple_(order_ts_expr, Document.id) > (cursor_ts, document_cursor) if sort == "release_date_asc" else tuple_(Document.created_at, Document.id) < (cursor_ts, document_cursor)
        statement = statement.where(cmp)
    ordered = statement.order_by(order_ts_expr.asc(), Document.id.asc()) if sort == "release_date_asc" else statement.order_by(Document.created_at.desc(), Document.id.desc())
    rows = list((await session.execute(ordered.limit(limit + 1))).all())
    more, page_rows = len(rows) > limit, rows[:limit]
    projections = await _load_filtered_news_projections(
        session, page_rows, source_ids=source_ids, channel_ids=channel_ids,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    next_cursor = _encode_news_projection_cursor(page_rows[-1][1], page_rows[-1][0], fingerprint) if more and page_rows else None
    return projections, next_cursor


# (raw upstream image URL, record local_only) -> signed same-origin proxy path or None.
ImageSigner = Callable[[str, bool], str | None]

FEED_MAX_WINDOW = timedelta(days=366)


async def list_gadget_document_projections(
    session: AsyncSession,
    *,
    source_ids: tuple[UUID, ...],
    limit: int = 50,
    cursor: str | None = None,
    channel_ids: tuple[str, ...] | None = None,
    language: str | None = None,
    since: datetime | None = None,
    include_dismissed: bool = False,
    image_signer: ImageSigner | None = None,
    countries: tuple[str, ...] | None = None,
    genres: tuple[str, ...] | None = None,
    languages: tuple[str, ...] | None = None,
    media_types: tuple[str, ...] | None = None,
    formats: tuple[str, ...] | None = None,
    content_ratings: tuple[str, ...] | None = None,
    release_statuses: tuple[str, ...] | None = None,
    release_year: int | None = None,
    release_year_from: int | None = None,
    release_year_to: int | None = None,
    release_season: str | None = None,
    min_rating: float | None = None,
    release_window_days: int | None = None,
    sort: Literal["created_at_desc", "release_date_asc"] = "created_at_desc",
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDocumentProjectionList:
    """Return active, current, ready source records as a small dashboard projection page.

    ``image_signer`` (caller-supplied, owns the privacy flag and signing secret) turns a record's raw feed image
    URL into a signed proxy path or ``None``; without it ``image_url`` is always null.

    Interaction state is read for the admitted actor. Documents retains source-generation and
    provider-scope validation; only short excerpts and typed provider fields leave this boundary.
    Full text stays out of dashboard APIs.
    """
    access_fence = await _admit_document_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    actor_id = access_fence.user_id
    if since is not None:
        now = datetime.now(UTC)
        if since.tzinfo is None or not now - FEED_MAX_WINDOW <= since <= now + timedelta(minutes=5):
            raise ValueError("Time filter must be a timezone-aware instant within the last year")
    projections, next_cursor = await list_news_document_projections(
        session,
        source_ids=source_ids,
        limit=limit,
        cursor=cursor,
        channel_ids=channel_ids,
        language=language,
        observed_since=since,
        countries=countries,
        genres=genres,
        languages=languages,
        media_types=media_types,
        formats=formats,
        content_ratings=content_ratings,
        release_statuses=release_statuses,
        release_year=release_year,
        release_year_from=release_year_from,
        release_year_to=release_year_to,
        release_season=release_season,
        min_rating=min_rating,
        release_window_days=release_window_days,
        sort=sort,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    version_ids = [item.document_version_id for item in projections]
    interaction_rows = (
        (
            await session.scalars(
                select(DocumentInteraction).where(
                    DocumentInteraction.owner_id == actor_id,
                    DocumentInteraction.document_version_id.in_(version_ids),
                )
            )
        ).all()
        if version_ids
        else []
    )
    interactions = {row.document_version_id: row for row in interaction_rows}
    # Hide-only: filtered after the privacy-fenced projection page, so a page may be short (the cursor still advances).
    items = [
        _as_gadget_document_projection(
            item, interactions.get(item.document_version_id), image_signer
        )
        for item in projections
        if include_dismissed
        or not (
            interactions.get(item.document_version_id)
            and interactions[item.document_version_id].dismissed_at
        )
    ]
    return GadgetDocumentProjectionList(items=items, next_cursor=next_cursor)


def _extract_media_projection(provider_metadata: Any) -> MediaProjectionRead | None:
    """Build a typed ``MediaProjectionRead`` from ``source_fields`` or synthesize one for YouTube."""
    if provider_metadata is None:
        return None
    sf = getattr(provider_metadata, "source_fields", None)
    if not isinstance(sf, Mapping):
        sf = {}
    media_type = sf.get("media_type")
    if isinstance(media_type, str) and media_type:
        raw_aspect = sf.get("artwork_aspect")
        aspect: Literal["poster", "square", "video"] = (
            cast(Literal["poster", "square", "video"], raw_aspect)
            if raw_aspect in _ARTWORK_ASPECTS
            else "poster"
        )
        raw_genres = sf.get("genres")
        genres = [g for g in raw_genres if isinstance(g, str)] if isinstance(raw_genres, list) else []
        raw_ext = sf.get("external_ids")
        external_ids = (
            {str(k): str(v) for k, v in raw_ext.items() if isinstance(v, str)}
            if isinstance(raw_ext, Mapping)
            else {}
        )
        raw_rating = sf.get("rating")
        rating = (
            float(raw_rating)
            if isinstance(raw_rating, (int, float)) and not isinstance(raw_rating, bool)
            else None
        )
        return MediaProjectionRead(
            media_type=media_type,
            artwork_aspect=aspect,
            country=sf.get("country") if isinstance(sf.get("country"), str) else None,
            language=sf.get("language") if isinstance(sf.get("language"), str) else None,
            genres=genres,
            release_year=sf.get("release_year") if type(sf.get("release_year")) is int else None,
            release_season=sf.get("release_season") if isinstance(sf.get("release_season"), str) else None,
            format=sf.get("format") if isinstance(sf.get("format"), str) else None,
            content_rating=sf.get("content_rating") if isinstance(sf.get("content_rating"), str) else None,
            rating=rating,
            release_status=sf.get("release_status") if isinstance(sf.get("release_status"), str) else None,
            release_date=sf.get("release_date") if isinstance(sf.get("release_date"), str) else None,
            creator=sf.get("creator") if isinstance(sf.get("creator"), str) else None,
            season=sf.get("season") if isinstance(sf.get("season"), str) else None,
            episode=sf.get("episode") if isinstance(sf.get("episode"), str) else None,
            chapter=sf.get("chapter") if isinstance(sf.get("chapter"), str) else None,
            volume=sf.get("volume") if isinstance(sf.get("volume"), str) else None,
            duration_seconds=(
                sf.get("duration_seconds") if type(sf.get("duration_seconds")) is int else None
            ),
            page_count=sf.get("page_count") if type(sf.get("page_count")) is int else None,
            trailer_url=sf.get("trailer_url") if isinstance(sf.get("trailer_url"), str) else None,
            attribution=sf.get("attribution") if isinstance(sf.get("attribution"), str) else None,
            external_ids=external_ids,
        )
    if getattr(provider_metadata, "provider", None) == "youtube":
        author = sf.get("author")
        return MediaProjectionRead(
            media_type="youtube",
            artwork_aspect="video",
            creator=author[:240] if isinstance(author, str) and author else None,
        )
    return None


def _as_gadget_document_projection(
    item: NewsDocumentProjection,
    interaction: DocumentInteraction | None = None,
    image_signer: ImageSigner | None = None,
) -> GadgetDocumentProjectionRead:
    """Project typed current source fields while omitting provider secrets and raw media handles.

    ``image_url`` is the signer's result for ``source_fields["image_url"]`` (given the record's ``local_only``);
    the raw upstream URL is never the way a client loads an image.
    """
    image_url: str | None = None
    if image_signer is not None and item.provider_metadata is not None:
        raw_image = item.provider_metadata.source_fields.get("image_url")
        if isinstance(raw_image, str):
            image_url = image_signer(raw_image, item.local_only)
    provider_metadata = item.provider_metadata
    safe_provider_metadata = None
    if provider_metadata is not None:
        safe_telegram = None
        if provider_metadata.telegram is not None:
            detail = provider_metadata.telegram
            safe_telegram = GadgetTelegramRecordRead(
                channel_id=detail.channel_id,
                message_id=detail.message_id,
                thread_id=detail.thread_id,
                reply_to_message_id=detail.reply_to_message_id,
                channel_label=detail.channel_label,
                channel_username=detail.channel_username,
                edited_received=detail.edited_received,
                published_at=detail.published_at,
                edited_at=detail.edited_at,
                media=[
                    GadgetTelegramMediaRead(
                        kind=media.kind,
                        caption=media.caption,
                        count=media.count,
                    )
                    for media in detail.media
                ],
            )
        safe_provider_metadata = GadgetProviderMetadataRead(
            provider=provider_metadata.provider,
            source_fields=provider_metadata.source_fields,
            telegram=safe_telegram,
        )
    return GadgetDocumentProjectionRead(
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        version_number=item.version_number,
        source_id=item.source_id,
        title=item.title,
        canonical_url=item.canonical_url,
        published_at=item.published_at,
        observed_at=item.observed_at,
        excerpt="\n\n".join(chunk.content for chunk in item.chunks)[:2000],
        provider_metadata=safe_provider_metadata,
        metadata_is_version_snapshot=item.metadata_is_version_snapshot,
        read_at=interaction.read_at if interaction else None,
        bookmarked_at=interaction.bookmarked_at if interaction else None,
        dismissed_at=interaction.dismissed_at if interaction else None,
        image_url=image_url,
        media=_extract_media_projection(provider_metadata),
    )


async def list_gadget_highlight_projection_page(
    session: AsyncSession,
    *,
    source_ids: tuple[UUID, ...],
    limit: int = 100,
    cursor_created_at: datetime | None = None,
    cursor_version_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetHighlightProjectionPage:
    """Page current accepted evidence by immutable-version creation order for durable highlight scans.

    New revisions of old documents receive new version IDs/timestamps and enter the forward scan.
    The cursor advances over candidate versions even when current-scope validation rejects one.
    """
    if (
        not source_ids
        or len(source_ids) > 32
        or len(set(source_ids)) != len(source_ids)
        or not 1 <= limit <= 100
        or (cursor_created_at is None) != (cursor_version_id is None)
    ):
        raise ValueError(
            "Highlight projection page has an invalid source set, limit, or version cursor"
        )
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    statement = (
        select(Document.id, DocumentVersion.id, DocumentVersion.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids),
            *_document_scope(scope),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            select(DocumentChunk.id)
            .where(DocumentChunk.document_version_id == DocumentVersion.id)
            .exists(),
        )
    )
    if cursor_created_at is not None and cursor_version_id is not None:
        statement = statement.where(
            tuple_(DocumentVersion.created_at, DocumentVersion.id)
            > (cursor_created_at, cursor_version_id)
        )
    rows = list(
        (
            await session.execute(
                statement.order_by(DocumentVersion.created_at, DocumentVersion.id).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    candidates = rows[:limit]
    items: list[GadgetDocumentProjectionRead] = []
    fences: list[GadgetDocumentSelectionFence] = []
    for document_id, version_id, _created_at in candidates:
        projection = await get_news_document_projection(
            session,
            document_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            projection is None
            or projection.document_version_id != version_id
            or projection.source_id not in source_ids
        ):
            continue
        items.append(_as_gadget_document_projection(projection))
        fences.append(
            GadgetDocumentSelectionFence(
                document_id=projection.document_id,
                document_version_id=projection.document_version_id,
                source_id=projection.source_id,
                source_generation=projection.current_source_generation,
                source_type=projection.source_type,
                provider=projection.provider,
                local_only=projection.local_only,
                scope_discriminator=projection.scope_discriminator,
            )
        )
    last = candidates[-1] if candidates else None
    return GadgetHighlightProjectionPage(
        items=items,
        selection_fences=fences,
        cursor_created_at=last[2] if last else None,
        cursor_version_id=last[1] if last else None,
        has_more=has_more,
    )


async def set_gadget_document_interaction(
    session: AsyncSession,
    *,
    document_id: UUID,
    version_number: int,
    payload: GadgetDocumentInteractionPatch,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDocumentInteractionRead | None:
    """Persist exact-version state for the admitted actor and publish under the original access fence."""
    access_fence = await _admit_document_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    actor_id = access_fence.user_id
    source_id = await session.scalar(
        select(Document.source_id).where(
            Document.id == document_id,
            *_document_scope(scope),
        )
    )
    if source_id is None:
        return None
    await sources.lock_source_for_document(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
    )
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id, *_document_scope(scope))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if document is None:
        return None
    projection = await get_news_document_projection(
        session,
        document_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None or projection.version_number != version_number:
        return None
    row = await session.get(
        DocumentInteraction,
        (actor_id, projection.document_version_id),
    )
    now = datetime.now(UTC)
    read_at = (
        (now if payload.read else None)
        if payload.read is not None
        else (row.read_at if row else None)
    )
    bookmarked_at = (
        (now if payload.bookmarked else None)
        if payload.bookmarked is not None
        else (row.bookmarked_at if row else None)
    )
    dismissed_at = (
        (now if payload.dismissed else None)
        if payload.dismissed is not None
        else (row.dismissed_at if row else None)
    )
    if read_at is None and bookmarked_at is None and dismissed_at is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        row = DocumentInteraction(
            owner_id=actor_id,
            document_version_id=projection.document_version_id,
            read_at=read_at,
            bookmarked_at=bookmarked_at,
            dismissed_at=dismissed_at,
        )
        session.add(row)
    else:
        row.read_at = read_at
        row.bookmarked_at = bookmarked_at
        row.dismissed_at = dismissed_at
        row.updated_at = now
    await commit_with_replay(
        session,
        [],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return GadgetDocumentInteractionRead(
        document_version_id=projection.document_version_id,
        read_at=read_at,
        bookmarked_at=bookmarked_at,
        dismissed_at=dismissed_at,
    )


def _news_projection_cursor_fingerprint(
    *,
    access_fence: AccessFence,
    source_ids: tuple[UUID, ...],
    observed_since: datetime | None,
    channel_ids: tuple[str, ...] | None,
    language: str | None = None,
    countries: tuple[str, ...] | None = None,
    genres: tuple[str, ...] | None = None,
    languages: tuple[str, ...] | None = None,
    media_types: tuple[str, ...] | None = None,
    formats: tuple[str, ...] | None = None,
    content_ratings: tuple[str, ...] | None = None,
    release_statuses: tuple[str, ...] | None = None,
    release_year: int | None = None,
    release_year_from: int | None = None,
    release_year_to: int | None = None,
    release_season: str | None = None,
    min_rating: float | None = None,
    release_window_days: int | None = None,
    sort: Literal["created_at_desc", "release_date_asc"] = "created_at_desc",
) -> str:
    """Hash News and media projection filters with the admitted actor/workspace revisions."""
    context = {
        "workspace": str(access_fence.workspace_id),
        "actor": access_fence.user_id,
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "sources": sorted(str(item) for item in source_ids),
        "observed_since": observed_since.astimezone(UTC).isoformat() if observed_since else None,
        "channels": sorted(channel_ids) if channel_ids is not None else None,
        "language": language,
        "countries": sorted(countries) if countries is not None else None,
        "genres": sorted(genres) if genres is not None else None,
        "languages": sorted(languages) if languages is not None else None,
        "media_types": sorted(media_types) if media_types is not None else None,
        "formats": sorted(formats) if formats is not None else None,
        "content_ratings": sorted(content_ratings) if content_ratings is not None else None,
        "release_statuses": sorted(release_statuses) if release_statuses is not None else None,
        "release_year": release_year,
        "release_year_from": release_year_from,
        "release_year_to": release_year_to,
        "release_season": release_season.strip().lower() if release_season else None,
        "min_rating": float(min_rating) if min_rating is not None else None,
        "release_window_days": release_window_days,
        "sort": sort,
    }
    return fingerprint_payload(
        context
    )  # sorted-key compact JSON SHA-256: identical to the prior inline hash


def _encode_news_projection_cursor(
    created_at: datetime, document_id: UUID, fingerprint: str
) -> str:
    """Encode the bounded keyset and its exact owner/source/filter context."""
    raw = json.dumps(
        [created_at.astimezone(UTC).isoformat(), str(document_id), fingerprint],
        separators=(",", ":"),
    )
    return b64url_encode(raw.encode())


def _decode_news_projection_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Decode a canonical projection cursor bound to its owner/source/filter context."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        timestamp, identifier, actual_fingerprint = json.loads(raw)
        parsed = datetime.fromisoformat(timestamp)
        parsed_id = UUID(identifier)
        if (
            parsed.tzinfo is None
            or actual_fingerprint != fingerprint
            or _encode_news_projection_cursor(parsed, parsed_id, fingerprint) != cursor
        ):
            raise ValueError
        return parsed, parsed_id
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid News projection cursor") from exc


async def _validate_gadget_document_selection_fences_admitted(
    session: AsyncSession,
    fences: tuple[GadgetDocumentSelectionFence, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence,
    lock_rows: bool,
    max_documents: int,
) -> bool:
    """Validate an exact selection under its original admission fence without rereading access."""
    if (
        not fences
        or len(fences) > max_documents
        or max_documents > 100
        or len({item.document_id for item in fences}) != len(fences)
        or len({item.source_id for item in fences}) > 32
    ):
        raise ValueError("Selection fences exceed their bounded unique-document or source limit")
    source_ids = sorted({item.source_id for item in fences}, key=str)
    document_ids = sorted({item.document_id for item in fences}, key=str)
    if lock_rows:
        source_set = await sources.lock_source_set(
            session,
            source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=expected_access_fence,
        )
        if len(source_set.fences) != len(source_ids):
            return False
        locked_documents = (
            await session.scalars(
                select(Document)
                .where(Document.id.in_(document_ids), *_document_scope(scope))
                .order_by(Document.id)
                .with_for_update(read=True)
            )
        ).all()
        if len(locked_documents) != len(document_ids):
            return False
    for fence in fences:
        components = await _current_document_components(
            session,
            fence.document_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_source_generation=fence.source_generation,
            admitted=True,
        )
        if components is None:
            return False
        _document, version, source, provenance = components
        accepted_scope = (
            provenance.provenance_json.get("provider_scope_discriminator") if provenance else None
        )
        if (
            version.id != fence.document_version_id
            or source.id != fence.source_id
            or source.generation != fence.source_generation
            or source.type != fence.source_type
            or source.provider != fence.provider
            or source.local_only != fence.local_only
            or accepted_scope != fence.scope_discriminator
        ):
            return False
    return True
