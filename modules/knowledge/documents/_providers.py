"""Provider snapshot reads for the typed provider-record evidence surface."""

import binascii
import json
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    desc,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    PROVIDER_IDS,
    ProviderDocumentSnapshotList,
    ProviderDocumentSnapshotRead,
    ProviderRecordMetadata,
)
from modules.sources.public import SourceReadProjection as Source


def _provider_snapshot(
    document: Document,
    version: DocumentVersion,
    source: Source,
    provenance: NormalizedVersionProvenance | None,
) -> ProviderDocumentSnapshotRead:
    """Project immutable version provenance, marking legacy mutable fallback clearly."""
    metadata = None
    if provenance is not None:
        raw_metadata = dict(provenance.provenance_json)
        raw_provider = raw_metadata.get("provider_record")
        if raw_provider is not None:
            metadata = ProviderRecordMetadata.model_validate(raw_provider)
        title = provenance.title
        canonical_url = provenance.canonical_url
        published_at = provenance.published_at
        provider_id = provenance.provider_id
        provider_version = provenance.provider_version
        observed_at = provenance.selection_observed_at
        received_at = provenance.received_at
        collected_at = provenance.collected_at
        snapshot = True
    else:
        title = document.title
        canonical_url = document.canonical_url
        published_at = document.published_at
        provider_id = document.external_id or ""
        provider_version = None
        observed_at = version.observed_at
        received_at = collected_at = None
        snapshot = False
    return ProviderDocumentSnapshotRead(
        document_id=document.id,
        document_version_id=version.id,
        version_number=version.version_number,
        source_id=source.id,
        source_status=source.status,
        provider_id=provider_id,
        provider_version=provider_version,
        title=title,
        canonical_url=canonical_url,
        published_at=published_at,
        observed_at=observed_at,
        received_at=received_at,
        collected_at=collected_at,
        excerpt=version.content[:1000],
        provider_metadata=metadata,
        metadata_is_version_snapshot=snapshot,
    )


def _encode_provider_cursor(created_at: datetime, document_id: UUID) -> str:
    """Encode the stable descending document creation keyset as bounded base64 JSON."""
    encoded = json.dumps(
        [created_at.astimezone(UTC).isoformat(), str(document_id)], separators=(",", ":")
    ).encode("utf-8")
    return b64url_encode(encoded)


def _decode_provider_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Strictly decode one timestamp/UUID keyset cursor without accepting junk."""
    if not cursor or len(cursor) > 1024:
        raise ValueError("Invalid provider snapshot cursor")
    try:
        raw = b64url_decode(cursor)
        values = json.loads(raw)
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(isinstance(x, str) for x in values)
        ):
            raise ValueError
        created_at = datetime.fromisoformat(values[0])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        document_id = UUID(values[1])
        if _encode_provider_cursor(created_at, document_id) != cursor:
            raise ValueError
        return created_at.astimezone(UTC), document_id
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid provider snapshot cursor") from exc


async def read_provider_snapshots(
    session: AsyncSession,
    version_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ProviderDocumentSnapshotRead]:
    """Read exact immutable provider versions for an already owner-authenticated route.

    This query is intentionally absent from agent, tool, and collector entry points;
    a future agent surface must apply P07 grants before calling a suitable owner API.
    Missing, deleted, archived, or duplicate versions fail as a whole request.
    """
    if not 1 <= len(version_ids) <= 100 or len(version_ids) != len(set(version_ids)):
        raise ValueError("version_ids must contain 1 to 100 unique values")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    rows = list(
        (
            await session.execute(
                select(Document, DocumentVersion, Source, NormalizedVersionProvenance)
                .select_from(Document)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(Source, Source.id == Document.source_id)
                .outerjoin(
                    NormalizedVersionProvenance,
                    NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                )
                .where(
                    DocumentVersion.id.in_(version_ids),
                    Source.provider.in_(PROVIDER_IDS),
                    *_document_scope(scope),
                )
            )
        ).all()
    )
    by_id = {
        version.id: _provider_snapshot(document, version, source, provenance)
        for document, version, source, provenance in rows
        if source.status in {"active", "paused"}
    }
    if len(by_id) != len(version_ids):
        raise ValueError("One or more provider document versions are unavailable")
    return [by_id[version_id] for version_id in version_ids]


async def list_provider_snapshots(
    session: AsyncSession,
    *,
    source_ids: list[UUID],
    scope: Scope,
    multi_workspace_enabled: bool,
    channel_ids: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> ProviderDocumentSnapshotList:
    """List current exact versions for an already owner-authenticated route.

    Pagination is a bounded created-at/ID keyset; only typed source/channel scope
    is read, and no agent/tool authorization is implied by this owner projection.
    """
    if not 1 <= len(source_ids) <= 100 or len(source_ids) != len(set(source_ids)):
        raise ValueError("source_ids must contain 1 to 100 unique values")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if channel_ids is not None and (
        len(channel_ids) > 100
        or len(channel_ids) != len(set(channel_ids))
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("channel_ids must contain at most 100 unique values")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    statement = (
        select(Document, DocumentVersion, Source, NormalizedVersionProvenance)
        .join(Source, Source.id == Document.source_id)
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == Document.id)
            & (DocumentVersion.version_number == Document.current_version),
        )
        .outerjoin(
            NormalizedVersionProvenance,
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
        )
        .where(
            Document.source_id.in_(source_ids),
            Source.status.in_(("active", "paused")),
            Source.provider.in_(PROVIDER_IDS),
            *_document_scope(scope),
        )
    )
    if cursor is not None:
        created_at, document_id = _decode_provider_cursor(cursor)
        statement = statement.where(
            tuple_(Document.created_at, Document.id) < tuple_(created_at, document_id)
        )
    if channel_ids is not None:
        statement = statement.where(
            NormalizedVersionProvenance.provenance_json["provider_record"]["telegram"][
                "channel_id"
            ].astext.in_(channel_ids)
        )
    rows = list(
        (
            await session.execute(
                statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    next_cursor = (
        _encode_provider_cursor(page[-1][0].created_at, page[-1][0].id)
        if len(rows) > limit and page
        else None
    )
    return ProviderDocumentSnapshotList(
        items=[
            _provider_snapshot(document, version, source, provenance)
            for document, version, source, provenance in page
        ],
        next_cursor=next_cursor,
    )
