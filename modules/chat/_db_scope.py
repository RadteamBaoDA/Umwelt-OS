"""Current-scope fences for the chat collected-data tool (observations, documents, evidence).

Each readable source is bound to the discriminator of its CURRENT configuration scope; rows collected
under another scope (a replaced configuration) are hidden. Pure SQLAlchemy expression builders, no I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, false, or_, select, tuple_
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.workspaces.schemas import Scope
from modules.connectors import public as connectors
from modules.knowledge.documents.public import (
    DocumentReadProjection as Document,
)
from modules.knowledge.documents.public import (
    DocumentVersionReadProjection as DocumentVersion,
)
from modules.knowledge.documents.public import (
    NormalizedVersionProvenanceReadProjection as NormalizedVersionProvenance,
)
from modules.knowledge.observations.public import ObservationReadProjection as Observation

SourceScopes = dict[UUID, str | None]
"""Per allowed source: discriminator of its CURRENT configuration scope (None = no verifiable scope)."""


def version_scope_discriminator() -> Any:
    """Scalar subquery: the accepted provider-scope discriminator in the provenance of the enclosing version.

    Correlates to the ``DocumentVersion`` of the enclosing query; yields NULL when the version has no
    provenance row or no discriminator (generic feeds without a verifiable scope).
    """
    return (
        select(NormalizedVersionProvenance.provenance_json["provider_scope_discriminator"].astext)
        .where(NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .limit(1)
        .scalar_subquery()
    )


def document_scope_filter(source_scopes: SourceScopes) -> Any:
    """Keep only documents whose version provenance discriminator equals their source's current one.

    ``IS NOT DISTINCT FROM`` so a source without a verifiable scope (None) matches only versions that carry
    no discriminator. An empty mapping matches nothing (fail closed). Requires ``Document`` and
    ``DocumentVersion`` in the enclosing FROM.

    One ``CASE source_id`` expected value and ONE provenance lookup per row: an OR of per-source pairs made
    the planner evaluate a correlated subquery per source and seq-scan every version (measured 0.8 s on 50k).
    """
    if not source_scopes:
        return false()
    expected = case(
        {source_id: discriminator for source_id, discriminator in source_scopes.items()},
        value=Document.source_id,
    )
    return and_(
        Document.source_id.in_(list(source_scopes)),
        version_scope_discriminator().is_not_distinct_from(expected),
    )


def observation_scope_predicates(
    model: Any,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    source_scopes: SourceScopes | None,
) -> list[Any]:
    """Workspace, allowed-source and current-scope predicates for ``model`` (``Observation`` or an alias).

    Pure. With ``source_scopes`` an observation must carry the discriminator of ITS source's current
    configuration; a source whose scope is None has no verifiable scope, and every observation row stores a
    non-NULL discriminator, so such a source contributes no observations (fail closed).
    """
    preds: list[Any] = [
        model.workspace_id == workspace_id,
        model.source_id.in_(allowed_source_ids),
    ]
    if source_scopes is not None:
        pairs = [
            and_(model.source_id == source_id, model.provider_scope_discriminator == discriminator)
            for source_id, discriminator in source_scopes.items()
            if discriminator is not None
        ]
        preds.append(or_(*pairs) if pairs else false())
    return preds


def not_superseded_across_sources(
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    source_scopes: SourceScopes | None,
    observed_from: datetime | None = None,
    observed_to: datetime | None = None,
    providers: list[str] | None = None,
) -> Any:
    """``NOT EXISTS`` clause keeping one current row per cross-source series point.

    The series key is (provider, metric, symbol, region, unit, currency, latitude, longitude, observed_at),
    mirroring the dashboard key; only rows of a DIFFERENT source compete (same-source revisions are settled
    by ``is_current``).

    Two sources can collect the same series point; the row with the latest ``accepted_at`` wins, ties broken
    by ``str(ingestion_observation_id)`` (the same key the dashboard history dedup and ``is_current`` use).
    Competing rows must themselves be current and inside the readable scope, so a hidden row never hides a
    visible one.

    ``observed_from`` / ``observed_to`` (exclusive) and ``providers`` repeat the caller's own window and
    provider filters on the competing side. They change no result (the key already equates ``observed_at``
    and ``provider``) but bound the anti-join's inner side: without them an aggregate hashed the whole
    table (1.8 s on 2M rows) whatever its window.
    """
    # ponytail: the null-safe key columns are not hashable, so an unfiltered all-source aggregate still runs
    # ~0.9 s SQL on 2M rows (default work_mem); coalesce-based hash keys or a dedup column if that matters.
    newer = aliased(Observation)
    bounds: list[Any] = []
    if observed_from is not None:
        bounds.append(newer.observed_at >= observed_from)
    if observed_to is not None:
        bounds.append(newer.observed_at < observed_to)
    if providers:
        bounds.append(newer.provider.in_(providers))
    return ~(
        select(1)
        .where(
            *observation_scope_predicates(
                newer,
                workspace_id=workspace_id,
                allowed_source_ids=allowed_source_ids,
                source_scopes=source_scopes,
            ),
            newer.is_current.is_(True),
            *bounds,
            newer.provider == Observation.provider,
            newer.metric == Observation.metric,
            newer.observed_at == Observation.observed_at,
            newer.symbol.is_not_distinct_from(Observation.symbol),
            newer.region.is_not_distinct_from(Observation.region),
            # Same-source revisions are already resolved by is_current; only OTHER sources compete, and only
            # when the whole dashboard series key matches so distinct series are never merged.
            newer.source_id != Observation.source_id,
            newer.unit.is_not_distinct_from(Observation.unit),
            newer.currency.is_not_distinct_from(Observation.currency),
            newer.latitude.is_not_distinct_from(Observation.latitude),
            newer.longitude.is_not_distinct_from(Observation.longitude),
            tuple_(newer.accepted_at, sql_cast(newer.ingestion_observation_id, String))
            > tuple_(
                Observation.accepted_at, sql_cast(Observation.ingestion_observation_id, String)
            ),
        )
        .exists()
    )


async def resolve_source_scopes(
    session: AsyncSession,
    allowed: list[tuple[UUID, str | None, str]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[tuple[UUID, str | None, str]], SourceScopes]:
    """Bind each allowed ``(id, provider, name)`` source to its CURRENT configuration scope.

    Chat filters by the discriminator of the current configuration, not by generation (a pause bumps it).
    One read-only connectors call; sources it does not report (archived or removed meanwhile) are dropped
    from the returned list. The mapping may hold None for a provider without a verifiable scope.
    """
    scopes = await connectors.chat_scope_discriminators(
        session,
        [source_id for source_id, _prov, _name in allowed],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return [row for row in allowed if row[0] in scopes], scopes
