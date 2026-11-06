"""Owner-scoped observation export paging and export-fence validation behind ``modules.knowledge.observations.public``."""

# isort: split
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import (
    b64url_decode,
    b64url_encode,
)
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.knowledge.observations._common import (
    _admit,
)
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import (
    ObservationExportFence,
    ObservationExportFenceValidation,
    ObservationExportPage,
    ObservationExportRead,
)
from modules.sources import public as sources
from modules.sources.public import SourceExportFence

OBSERVATION_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _observation_export_cursor(
    owner_id: int,
    workspace_id: UUID,
    snapshot_at: datetime,
    accepted_at: datetime,
    row_id: UUID,
    access_fence: AccessFence,
) -> str:
    """Encode owner/workspace, admission revisions, cutoff and accepted-time/ID position."""
    if owner_id != access_fence.user_id or workspace_id != access_fence.workspace_id:
        raise ValueError("Observation export cursor identity does not match its access fence")
    value = {
        "v": 3,
        "owner": owner_id,
        "workspace": str(workspace_id),
        "kind": "observations",
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "snapshot": snapshot_at.astimezone(UTC).isoformat(),
        "at": accepted_at.astimezone(UTC).isoformat(),
        "id": str(row_id),
    }
    return b64url_encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _decode_observation_export_cursor(
    cursor: str,
    owner_id: int,
    workspace_id: UUID,
    access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Validate owner, workspace and admission revisions for the canonical export keyset."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        value = json.loads(b64url_decode(cursor))
        if not isinstance(value, dict) or set(value) != {
            "v",
            "owner",
            "workspace",
            "kind",
            "membership_revision",
            "configuration_revision",
            "snapshot",
            "at",
            "id",
        }:
            raise ValueError
        if (
            value["v"] != 3
            or value["owner"] != owner_id
            or value["workspace"] != str(workspace_id)
            or value["kind"] != "observations"
            or owner_id != access_fence.user_id
            or workspace_id != access_fence.workspace_id
            or value["membership_revision"] != access_fence.membership_revision
            or value["configuration_revision"] != access_fence.configuration_revision
        ):
            raise ValueError
        snapshot, position = (
            datetime.fromisoformat(value["snapshot"]),
            datetime.fromisoformat(value["at"]),
        )
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot, position)):
            raise ValueError
        snapshot, position = snapshot.astimezone(UTC), position.astimezone(UTC)
        row_id = UUID(value["id"])
        if (
            snapshot > datetime.now(UTC)
            or _observation_export_cursor(
                owner_id,
                workspace_id,
                snapshot,
                position,
                row_id,
                access_fence,
            )
            != cursor
        ):
            raise ValueError
        return snapshot, position, row_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid observation export cursor") from exc


def _observation_export_digest(item: ObservationExportRead) -> str:
    """Hash the exact allowlisted portable record rather than internal ORM state."""
    return hashlib.sha256(
        json.dumps(
            item.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


async def _observation_export_count(
    session: AsyncSession,
    snapshot_at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Count only rows whose current source scope and exact document version remain valid."""
    # ponytail: exact public evidence filtering scans O(N) per page; replace with a Documents-owned
    # SQL eligibility count only when capacity evidence shows this bounded-memory path is too slow.
    base = select(Observation).where(
        Observation.workspace_id == scope.workspace_id,
        Observation.is_current.is_(True),
        Observation.accepted_at <= snapshot_at,
        Observation.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )
    count, position = 0, None
    while True:
        statement = base
        if position is not None:
            statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
        rows = list(
            (
                await session.scalars(
                    statement.order_by(Observation.accepted_at, Observation.id)
                    .limit(256)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not rows:
            return count
        scope_by_source: dict[UUID, Any] = {}
        candidates = []
        for row in rows:
            provider_scope = scope_by_source.get(row.source_id)
            if row.source_id not in scope_by_source:
                source = await sources.get_connector_source(
                    session,
                    row.source_id,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                provider_scope = (
                    await connectors.export_provider_scope(
                        session,
                        row.source_id,
                        source.generation,
                        scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                    if source is not None
                    else None
                )
                scope_by_source[row.source_id] = provider_scope
            provider_scope = scope_by_source[row.source_id]
            if provider_scope is not None and provider_scope.provider_id == row.provider:
                candidates.append(
                    (
                        documents.ObservationExportEvidenceCandidate(
                            observation_id=row.id,
                            source_id=row.source_id,
                            accepted_source_generation=row.source_generation,
                            provider=row.provider,
                            provider_scope_discriminator=row.provider_scope_discriminator,
                            external_id=row.external_id,
                            document_id=row.document_id,
                            document_version_id=row.document_version_id,
                        ),
                        provider_scope,
                    )
                )
        for candidate, provider_scope in candidates:
            count += int(
                await documents.export_observation_evidence(
                    session,
                    candidate,
                    provider_scope,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                is not None
            )
        position = (rows[-1].accepted_at, rows[-1].id)


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ObservationExportPage:
    """Export bounded actor/workspace measurements with current Source and exact retained Documents evidence."""
    if record_kind != "observations" or not 1 <= limit <= 100:
        raise ValueError("Observation export kind or limit is invalid")
    if owner_id != _actor(scope):
        raise ValueError("Observation export actor does not match the admitted scope")
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if cursor is None:
        snapshot, position = datetime.now(UTC), None
    else:
        snapshot, position_at, position_id = _decode_observation_export_cursor(
            cursor,
            owner_id,
            scope.workspace_id,
            access_fence,
        )
        position = (position_at, position_id)
    count = await _observation_export_count(
        session,
        snapshot,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    statement = select(Observation).where(
        Observation.workspace_id == scope.workspace_id,
        Observation.is_current.is_(True),
        Observation.accepted_at <= snapshot,
        Observation.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )
    if position is not None:
        statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
    rows = list(
        (
            await session.scalars(
                statement.order_by(Observation.accepted_at, Observation.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    more = len(rows) > limit
    items: list[ObservationExportRead] = []
    fences: list[ObservationExportFence] = []
    last_examined: tuple[datetime, UUID] | None = None
    for row in rows[:limit]:
        source = await sources.get_connector_source(
            session,
            row.source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if source is None:
            last_examined = (row.accepted_at, row.id)
            continue
        provider_scope = await connectors.export_provider_scope(
            session,
            row.source_id,
            source.generation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if provider_scope is None or provider_scope.provider_id != row.provider:
            last_examined = (row.accepted_at, row.id)
            continue
        evidence = await documents.export_observation_evidence(
            session,
            documents.ObservationExportEvidenceCandidate(
                observation_id=row.id,
                source_id=row.source_id,
                accepted_source_generation=row.source_generation,
                provider=row.provider,
                provider_scope_discriminator=row.provider_scope_discriminator,
                external_id=row.external_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
            ),
            provider_scope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if evidence is None:
            last_examined = (row.accepted_at, row.id)
            continue
        item = ObservationExportRead(
            id=row.id,
            source_id=row.source_id,
            source_generation=row.source_generation,
            provider=row.provider,
            external_id=row.external_id,
            revision=row.revision,
            metric=row.metric,
            symbol=row.symbol,
            region=row.region,
            latitude=row.latitude,
            longitude=row.longitude,
            observed_at=row.observed_at,
            published_at=row.published_at,
            collected_at=row.collected_at,
            accepted_at=row.accepted_at,
            value=row.value,
            unit=row.unit,
            currency=row.currency,
            timezone=row.timezone,
            quality=row.quality,
            missing_reason=row.missing_reason,
            document_id=row.document_id,
            document_version_id=row.document_version_id,
        )
        proposed = items + [item]
        size = len(
            json.dumps(
                [value.model_dump(mode="json") for value in proposed],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        if size > OBSERVATION_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An observation export record exceeds the page byte budget")
            more = True
            break
        items.append(item)
        last_examined = (row.accepted_at, row.id)
        fences.append(
            ObservationExportFence(
                id=row.id,
                source_id=row.source_id,
                accepted_source_generation=row.source_generation,
                current_source_generation=evidence.current_source_generation,
                provider_scope_digest=hashlib.sha256(
                    provider_scope.discriminator.encode()
                ).hexdigest(),
                record_digest=_observation_export_digest(item),
            )
        )
    next_cursor = (
        _observation_export_cursor(
            owner_id,
            scope.workspace_id,
            snapshot,
            last_examined[0],
            last_examined[1],
            access_fence,
        )
        if more and last_examined is not None
        else None
    )
    payload = len(
        json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    return ObservationExportPage(
        owner_id=owner_id,
        snapshot_at=snapshot,
        snapshot_count=count,
        items=items,
        fences=fences,
        payload_bytes=payload,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[ObservationExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ObservationExportFenceValidation:
    """Recheck one admitted actor/workspace snapshot, provider scope, generation and exact exported fields."""
    if record_kind != "observations" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Observation export validation input is invalid")
    if owner_id != _actor(scope):
        raise ValueError("Observation export actor does not match the admitted scope")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    observed = await _observation_export_count(
        session,
        snapshot_at,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if observed != expected_snapshot_count:
        return ObservationExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        row = await session.scalar(
            select(Observation)
            .where(
                Observation.id == fence.id,
                Observation.workspace_id == scope.workspace_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.source_id != fence.source_id
            or row.source_generation != fence.accepted_source_generation
        ):
            return ObservationExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        source = await sources.get_connector_source(
            session,
            row.source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        provider_scope = (
            await connectors.export_provider_scope(
                session,
                row.source_id,
                source.generation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if source is not None
            else None
        )
        if (
            source is None
            or source.generation != fence.current_source_generation
            or provider_scope is None
            or provider_scope.provider_id != row.provider
            or hashlib.sha256(provider_scope.discriminator.encode()).hexdigest()
            != fence.provider_scope_digest
        ):
            return ObservationExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        item = ObservationExportRead(
            id=row.id,
            source_id=row.source_id,
            source_generation=row.source_generation,
            provider=row.provider,
            external_id=row.external_id,
            revision=row.revision,
            metric=row.metric,
            symbol=row.symbol,
            region=row.region,
            latitude=row.latitude,
            longitude=row.longitude,
            observed_at=row.observed_at,
            published_at=row.published_at,
            collected_at=row.collected_at,
            accepted_at=row.accepted_at,
            value=row.value,
            unit=row.unit,
            currency=row.currency,
            timezone=row.timezone,
            quality=row.quality,
            missing_reason=row.missing_reason,
            document_id=row.document_id,
            document_version_id=row.document_version_id,
        )
        evidence = await documents.export_observation_evidence(
            session,
            documents.ObservationExportEvidenceCandidate(
                observation_id=row.id,
                source_id=row.source_id,
                accepted_source_generation=row.source_generation,
                provider=row.provider,
                provider_scope_discriminator=row.provider_scope_discriminator,
                external_id=row.external_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
            ),
            provider_scope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        eligible = await sources.filter_export_eligible_sources(
            session,
            [
                SourceExportFence(
                    source_id=fence.source_id,
                    workspace_id=scope.workspace_id,
                    generation=fence.current_source_generation,
                )
            ],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            not row.is_current
            or row.accepted_at > snapshot_at
            or row.source_id not in eligible
            or evidence is None
            or evidence.current_source_generation != fence.current_source_generation
        ):
            return ObservationExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        if _observation_export_digest(item) != fence.record_digest:
            return ObservationExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return ObservationExportFenceValidation(
        valid=True, reason="valid", observed_snapshot_count=observed
    )
