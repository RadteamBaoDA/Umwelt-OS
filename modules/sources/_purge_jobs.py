"""Connector fencing, retained-effect anchors, job identity and impact reads."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.sources._export import export_eligible_source_ids
from modules.sources._fences import source_fence_of
from modules.sources._scope import (
    _admit_source_scope,
    _operation_scope,
    _require_source_owner,
    _source_scope,
)
from modules.sources._tools import get_source
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import SourceFence, SourcePurgeJobIdentity


async def _fence_connector_source(
    session: AsyncSession,
    source: Source,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Prepare then retire exact-source work with the original admitted access, flush only.

    Caller holds admission and exact Source, including its authorized transition once.
    Flush that transition and use only Source's constrained successor scope/current fence.
    Connector provisioning/all slots/browser/archived native precede tokens, Tools jobs and
    pages. Prepared token revoke and singular browser purge precede Connector continuation,
    which alone owns browser revision, provisioning/retirement and late hints/capacity.
    Missing optional rows never skip other cleanup. No earlier reentry, intermediate commit
    or provider I/O occurs; caller owns final replay with the original AccessFence.
    """
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import (
        lock_source_browser_results_in_uow,
        purge_source_browser_results_in_uow,
    )

    scope = _transitioned_source_scope(scope, source)
    await session.flush()
    source_fence = source_fence_of(source)
    await connectors.prepare_source_lifecycle_cleanup_in_uow(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await ingestion.lock_source_credentials_in_uow(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await lock_source_browser_results_in_uow(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await ingestion.revoke_source_credentials(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await purge_source_browser_results_in_uow(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await connectors.apply_source_lifecycle_cleanup_in_uow(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def _reconcile_github_hint_lifecycle(
    session: AsyncSession,
    source: Source,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    active: bool,
) -> None:
    """Continue the held source/provisioning lifecycle transaction into GitHub hint ownership.

    The connector owner acquires the GitHub grant before hints and capacity. A source is rearmed
    only for an explicit activation carrying a current verified grant; access-loss pauses stay
    paused until the owner reconnects and explicitly resumes the source.
    """
    from modules.connectors import public as connectors

    await connectors.reconcile_github_source_hints_lifecycle(
        session,
        source_id=source.id,
        source_generation=source.generation,
        active=active,
        scope=_transitioned_source_scope(scope, source),
        multi_workspace_enabled=multi_workspace_enabled,
    )


def _transitioned_source_scope(scope: Scope, source: Source) -> Scope:
    """Carry one verified in-transaction Source generation transition into cleanup callees.

    This never changes actor/workspace/membership. Entry points first lock the exact captured
    generation; only the same or next generation is permitted, never revival of a stale job.
    """
    _require_source_owner(scope)
    if scope.workspace_id != source.workspace_id:
        raise ValueError("Source workspace changed")
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        if scope.source_generation is None:
            raise RuntimeError("invariant violated: scope.source_generation must not be None")
        if scope.source_id != source.id or source.generation not in (
            scope.source_generation,
            scope.source_generation + 1,
        ):
            raise ValueError("Source lifecycle transition does not match its captured generation")
        return replace(scope, source_generation=source.generation)
    return scope


async def get_connector_retained_effect_anchor_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> SourceFence | None:
    """Read lifecycle metadata solely to settle Connector's exact retained remote effect.

    Caller holds original admission/Source before Connector operation locks. Require the
    unchanged original AccessFence and, for a bound worker, this exact Source/original G.
    A later generation of that same Source is visible only as lifecycle fence metadata:
    no current configuration, upgraded Scope, collection or activation authority. Connector
    must separately prove its immutable retained operation/step/target. No locks/commit/I/O;
    revoked access raises, missing/foreign/earlier-generation anchor returns None.
    """
    current_access = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence:
        raise ConflictError(detail="Retained Connector access fence changed")
    if type(source_generation) is not int or source_generation <= 0:
        raise ValueError("Original Connector Source generation is required")
    if (
        isinstance(scope, InternalJobScope)
        and scope.source_id is not None
        and (scope.source_id != source_id or scope.source_generation != source_generation)
    ):
        raise ValueError("Retained effect does not match the original bound Source")
    row = (
        await session.execute(
            select(
                Source.id,
                Source.workspace_id,
                Source.status,
                Source.generation,
                Source.local_only,
                Source.graph_enabled,
                Source.rag_index_enabled,
            ).where(
                Source.id == source_id,
                Source.workspace_id == scope.workspace_id,
                Source.generation >= source_generation,
            )
        )
    ).one_or_none()
    return SourceFence(**row._mapping) if row is not None else None


async def lock_connector_retained_effect_anchor(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> SourceFence | None:
    """Acquire original admission then this Source for exact Connector effect settlement.

    Enter before domain locks; require original access/config epoch without renewal.
    Lock only the original workspace/Source identity even when its generation advanced.
    Return lifecycle metadata only through the held owner contract; caller retains locks
    through cleanup/disposition commit and releases them before network. No commit/I/O.
    """
    await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=access_fence,
    )
    if type(source_generation) is not int or source_generation <= 0:
        raise ValueError("Original Connector Source generation is required")
    if (
        isinstance(scope, InternalJobScope)
        and scope.source_id is not None
        and (scope.source_id != source_id or scope.source_generation != source_generation)
    ):
        raise ValueError("Retained effect does not match the original bound Source")
    identifier = await session.scalar(
        select(Source.id)
        .where(
            Source.id == source_id,
            Source.workspace_id == scope.workspace_id,
            Source.generation >= source_generation,
        )
        .with_for_update()
    )
    if identifier is None:
        return None
    return await get_connector_retained_effect_anchor_in_uow(
        session,
        source_id,
        source_generation=source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


async def discover_source_job_identity(
    session: AsyncSession,
    source_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Discover one real owned-default scheduler identity without acquiring row locks.

    Global fanout pages may call this repeatedly in one read transaction. Fresh owner,
    account/default-workspace/membership checks and an exact Source identity reread
    establish an identity snapshot only; no authorize_internal_job, commit or network.
    Each eventual binding must independently lock/admit its captured original access,
    Source generation and configuration before any effect. No metadata is returned.
    """
    identity = (
        await session.execute(
            select(Source.workspace_id, Source.generation).where(
                Source.id == source_id,
            )
        )
    ).one_or_none()
    if identity is None:
        return None
    owner = await workspaces.resolve_workspace_owner_context(
        session,
        identity.workspace_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    scope = InternalJobScope(
        workspace_id=identity.workspace_id,
        actor_user_id=owner.user_id,
        membership_revision=owner.membership_revision,
        source_id=source_id,
        source_generation=identity.generation,
    )
    current = await session.scalar(
        select(Source.id).where(
            Source.id == source_id,
            *_source_scope(scope),
        )
    )
    return scope if current is not None else None


async def resolve_source_job_scope(
    session: AsyncSession,
    source_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve internal scheduler lineage only, then lock admission before Source reread.

    Discovery returns no metadata/configuration and never borrows owner authority for HTTP.
    Deferred jobs compare their original captured claim to this result; they cannot upgrade it.
    Caller owns rollback/commit and releases locks before collection or other external work.
    """
    identity = (
        await session.execute(
            select(Source.workspace_id, Source.generation).where(
                Source.id == source_id,
            )
        )
    ).one_or_none()
    if identity is None:
        return None
    owner = await workspaces.resolve_workspace_owner_context(
        session,
        identity.workspace_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    try:
        scope = InternalJobScope(
            workspace_id=identity.workspace_id,
            actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
            source_id=source_id,
            source_generation=identity.generation,
        )
    except ValueError:
        return None
    await workspaces.authorize_internal_job(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    current = await session.scalar(
        select(Source.id).where(
            Source.id == source_id,
            *_source_scope(scope),
        )
    )
    return scope if current is not None else None


async def read_source_purge_job_capture(
    session: AsyncSession,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> SourcePurgeJobIdentity | None:
    """Read the exact captured purge authority tuple without locks, live Source or raw URIs.

    NULL (legacy) or stale configuration, a different membership epoch or an invalid tuple
    returns None; the caller never upgrades an old job to the current epoch.
    """
    fence = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    row = (
        await session.execute(
            select(
                SourcePurgeOperation.id,
                SourcePurgeOperation.workspace_id,
                SourcePurgeOperation.actor_user_id,
                SourcePurgeOperation.membership_revision,
                SourcePurgeOperation.configuration_revision,
                SourcePurgeOperation.source_id,
                SourcePurgeOperation.generation,
            ).where(
                SourcePurgeOperation.id == operation_id,
                *_operation_scope(scope),
                SourcePurgeOperation.membership_revision == scope.membership_revision,
            )
        )
    ).one_or_none()
    if row is None or row.configuration_revision is None:
        return None
    try:
        capture = SourcePurgeJobIdentity(
            operation_id=row.id,
            workspace_id=row.workspace_id,
            actor_user_id=row.actor_user_id,
            membership_revision=row.membership_revision,
            configuration_revision=row.configuration_revision,
            source_id=row.source_id,
            source_generation=row.generation,
        )
    except ValueError:
        return None
    return capture if fence.configuration_revision == capture.configuration_revision else None


async def read_source_purge_job_identity(
    session: AsyncSession,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Project the exact scoped retained purge capture to an InternalJobScope, nonlocking.

    Internal publication callers retain their ordered admission/domain transaction and compare
    operation ID plus workspace/actor/membership/source/generation to the event payload. Current
    caller admission and the captured membership+configuration epoch must match; legacy NULL or
    stale configuration returns None, never an upgraded job. No URI/content is exposed.
    """
    capture = await read_source_purge_job_capture(
        session,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if capture is None:
        return None
    try:
        return InternalJobScope(
            workspace_id=capture.workspace_id,
            actor_user_id=capture.actor_user_id,
            membership_revision=capture.membership_revision,
            source_id=capture.source_id,
            source_generation=capture.source_generation,
        )
    except ValueError:
        return None


async def resolve_source_purge_job_scope(
    session: AsyncSession,
    operation_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Admit one retained purge's exact durable actor/membership/configuration/source identity.

    Identity-only discovery (no live Source); a NULL captured configuration returns None
    before any authorize call. Ordered auth/workspace/membership locks precede the nonlocking
    exact capture reread; an admitted fence that differs from the captured tuple is a stale job
    (None, never rebased). Current permission loss propagates admission failure. No domain lock.
    """
    row = (
        await session.execute(
            select(
                SourcePurgeOperation.workspace_id,
                SourcePurgeOperation.actor_user_id,
                SourcePurgeOperation.membership_revision,
                SourcePurgeOperation.configuration_revision,
                SourcePurgeOperation.source_id,
                SourcePurgeOperation.generation,
            ).where(SourcePurgeOperation.id == operation_id)
        )
    ).one_or_none()
    if row is None or row.configuration_revision is None:
        return None
    try:
        scope = InternalJobScope(
            workspace_id=row.workspace_id,
            actor_user_id=row.actor_user_id,
            membership_revision=row.membership_revision,
            source_id=row.source_id,
            source_generation=row.generation,
        )
        expected = AccessFence(
            workspace_id=row.workspace_id,
            user_id=row.actor_user_id,
            membership_revision=row.membership_revision,
            configuration_revision=row.configuration_revision,
        )
    except ValueError:
        return None
    fence = await workspaces.authorize_internal_job(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if fence != expected:
        return None
    capture = await read_source_purge_job_capture(
        session,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if capture is None or (
        capture.workspace_id,
        capture.actor_user_id,
        capture.membership_revision,
        capture.configuration_revision,
        capture.source_id,
        capture.source_generation,
    ) != (
        row.workspace_id,
        row.actor_user_id,
        row.membership_revision,
        row.configuration_revision,
        row.source_id,
        row.generation,
    ):
        return None
    return scope


@dataclass(frozen=True)
class SourceImpact:
    """Owner-UI-only counts of what a disconnect/purge would touch; each saturates at 1000."""

    document_count: int
    gadget_definition_count: int
    gadget_placement_count: int
    conversation_count: int


async def get_source_impact(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> SourceImpact | None:
    """Count dependents of a source through public APIs; None when foreign/absent, 409 when purge-pending."""
    if (
        await get_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        is None
    ):
        return None
    if (
        await session.scalar(export_eligible_source_ids(scope=scope).where(Source.id == source_id))
        is None
    ):
        raise ConflictError(detail="Source purge is pending")
    from modules.chat import public as chat
    from modules.dashboard import public as dashboard
    from modules.knowledge.documents import public as documents

    definitions, placements = await dashboard.count_source_gadgets(session, source_id, scope=scope)
    return SourceImpact(
        document_count=await documents.count_source_documents(session, source_id, scope=scope),
        gadget_definition_count=definitions,
        gadget_placement_count=placements,
        conversation_count=await chat.count_source_conversations(session, source_id, scope=scope),
    )


# User-authored content: uploads are type "file", notes are "manual". They must never be
# aged out by the connector retention cron (there is no "upload" SourceType).
RETENTION_EXEMPT_SOURCE_TYPES = ("file", "manual")


async def list_retention_cutoffs(
    session: AsyncSession,
    *,
    now: datetime,
) -> list[tuple[UUID, datetime]]:
    """Return ``(source_id, cutoff)`` for every connector Source with a bounded retention window.

    ``file`` (uploads) and ``manual`` (user notes) sources are excluded so user-authored
    documents are never aged out. A per-source
    ``configuration["retention_days"]`` wins only when it is a real int (not bool) in 1..3650;
    anything else falls back to the connector provider default. Sources with no positive window
    are omitted. Read-only; spans all workspaces because it feeds the system retention cron.
    """
    from datetime import timedelta

    from modules.connectors import public as connectors

    rows = (
        await session.execute(
            select(Source.id, Source.provider, Source.configuration).where(
                Source.type.not_in(RETENTION_EXEMPT_SOURCE_TYPES)
            )
        )
    ).all()
    cutoffs: list[tuple[UUID, datetime]] = []
    for row in rows:
        cfg = row.configuration if isinstance(row.configuration, dict) else {}
        raw_days = cfg.get("retention_days")
        if isinstance(raw_days, int) and not isinstance(raw_days, bool) and 1 <= raw_days <= 3650:
            days: int | None = raw_days
        else:
            days = connectors.default_retention_days(row.provider)
        if days is not None and days >= 1:
            cutoffs.append((row.id, now - timedelta(days=days)))
    return cutoffs
