"""News/gadget projections of current ready versions, interactions and selection fences."""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    Select,
    func,
    select,
    tuple_,
)
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


def _news_projection_statement(
    scope: Scope,
    source_ids: tuple[UUID, ...],
    observed_since: datetime | None,
    channel_ids: tuple[str, ...] | None,
    language: str | None,
) -> Select[UUID, datetime]:
    """Build the id/created_at selection of current ready versions for the given news filters.

    Pure statement construction (no I/O): active sources only, current version with at least one
    chunk, optional observed-since, language (stored normalized) and Telegram channel filters.
    """
    statement = (
        select(Document.id, Document.created_at)
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
        # Unknown (NULL) language never matches a concrete language; only "Any" (None) returns it.
        # Stored normalized (primary subtag, lowercase) so this equality can use ix_documents_workspace_language_created_at_id.
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
    return statement


async def list_news_document_projections(
    session: AsyncSession,
    *,
    source_ids: tuple[UUID, ...],
    limit: int = 50,
    cursor: str | None = None,
    observed_since: datetime | None = None,
    channel_ids: tuple[str, ...] | None = None,
    language: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[NewsDocumentProjection], str | None]:
    """Page bounded current ready versions from explicitly authorized active sources."""
    if (
        not source_ids
        or len(source_ids) > 32
        or len(set(source_ids)) != len(source_ids)
        or not 1 <= limit <= 100
    ):
        raise ValueError("News source page must contain 1 to 32 unique sources and a bounded limit")
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if language is not None and language not in FEED_LANGUAGES:
        raise ValueError("Language filter must be an allowlisted code")
    if channel_ids is not None and (
        len(channel_ids) > 32
        or len(set(channel_ids)) != len(channel_ids)
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("Channel scope must contain at most 32 unique numeric identifiers")
    fingerprint = _news_projection_cursor_fingerprint(
        access_fence=access_fence,
        source_ids=source_ids,
        observed_since=observed_since,
        channel_ids=channel_ids,
        language=language,
    )
    statement = _news_projection_statement(scope, source_ids, observed_since, channel_ids, language)
    if cursor:
        created_at, document_cursor = _decode_news_projection_cursor(cursor, fingerprint)
        statement = statement.where(
            tuple_(Document.created_at, Document.id) < (created_at, document_cursor)
        )
    rows = list(
        (
            await session.execute(
                statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    projections = []
    for document_id, _created_at in rows:
        item = await get_news_document_projection(
            session,
            document_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if item is not None and item.source_id in source_ids:
            if channel_ids is not None and (
                item.provider_metadata is None
                or item.provider_metadata.provider != "telegram"
                or item.provider_metadata.telegram is None
                or item.provider_metadata.telegram.channel_id not in channel_ids
            ):
                continue
            projections.append(item)
    next_cursor = (
        _encode_news_projection_cursor(
            rows[-1][1],
            rows[-1][0],
            fingerprint,
        )
        if more and rows
        else None
    )
    return projections, next_cursor


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
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDocumentProjectionList:
    """Return active, current, ready source records as a small dashboard projection page.

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
        _as_gadget_document_projection(item, interactions.get(item.document_version_id))
        for item in projections
        if include_dismissed
        or not (
            interactions.get(item.document_version_id)
            and interactions[item.document_version_id].dismissed_at
        )
    ]
    return GadgetDocumentProjectionList(items=items, next_cursor=next_cursor)


def _as_gadget_document_projection(
    item: NewsDocumentProjection,
    interaction: DocumentInteraction | None = None,
) -> GadgetDocumentProjectionRead:
    """Project typed current source fields while omitting provider secrets and raw media handles."""
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
) -> str:
    """Hash News filters with the admitted actor/workspace and membership/configuration revisions."""
    context = {
        "workspace": str(access_fence.workspace_id),
        "actor": access_fence.user_id,
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "sources": sorted(str(item) for item in source_ids),
        "observed_since": observed_since.astimezone(UTC).isoformat() if observed_since else None,
        "channels": sorted(channel_ids) if channel_ids is not None else None,
        "language": language,
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
