"""Source fences, row/set locks, lifecycle projections and connector result recording."""

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, ForbiddenError, NotFoundError
from core.workspaces.schemas import AccessFence, Scope
from modules.sources._export import export_eligible_source_ids
from modules.sources._scope import _admit_source_scope, _source_scope
from modules.sources.models import Source
from modules.sources.schemas import ConnectorSource, SourceFence, SourceFenceSet


def _connector_source(source: Source) -> ConnectorSource:
    """Build a defensive public connector projection from a source record."""
    return ConnectorSource(
        id=source.id,
        workspace_id=source.workspace_id,
        type=source.type,
        status=source.status,
        generation=source.generation,
        configuration=deepcopy(source.configuration or {}),
        local_only=source.local_only,
        provider=source.provider,
        graph_enabled=source.graph_enabled,
        rag_index_enabled=source.rag_index_enabled,
    )


async def get_connector_source(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> ConnectorSource | None:
    """Read fresh retained source configuration and project it for connector consumers.

    populate_existing keeps generation, status and provider scope checks current even when the
    caller's identity map already holds this source; this remains a read with no row lock.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await session.scalar(
        select(Source)
        .where(Source.id == source_id, *_source_scope(scope))
        .execution_options(populate_existing=True)
    )
    return _connector_source(source) if source is not None else None


async def _lock_source_row(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> tuple[Source | None, AccessFence]:
    """Return the refreshed Source and access fence under auth->workspace->Source locks.

    Entry requires no previously held domain locks. Optional expected fence performs deferred
    configuration CAS before Source locking; caller owns commit and release before external I/O.
    """
    access_fence = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=expected_access_fence,
    )
    source = await session.scalar(
        select(Source)
        .where(Source.id == source_id, *_source_scope(scope))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return source, access_fence


def source_fence_of(source: Source) -> SourceFence:
    """Detach a complete fence (including graph/RAG toggles) from a Source row.

    Connector fence checks compare every field with the actual ``get_source_fence`` value, so
    omitting a toggle makes non-default Sources fail 409 forever. Pure; no I/O or authority.
    """
    return SourceFence(
        id=source.id,
        workspace_id=source.workspace_id,
        status=source.status,
        generation=source.generation,
        local_only=source.local_only,
        graph_enabled=source.graph_enabled,
        rag_index_enabled=source.rag_index_enabled,
    )


async def get_source_fence(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> SourceFence | None:
    """Read a detached source eligibility fence without acquiring a database row lock.

    Owner: modules/sources
    Fields: id, workspace_id, status, generation, local_only, graph_enabled, rag_index_enabled.
    Current owner admission and any internal exact generation are required; absent/invisible
    Source returns None. It is read-only and grants no authority to fetch provider items.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await session.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if source is None:
        return None
    return source_fence_of(source)


async def get_source_fences(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, SourceFence]:
    """Read detached eligibility fences for many Sources with one admission and one ``IN`` query.

    Batch form of ``get_source_fence`` (same projection, same workspace/internal-generation scoping,
    no row lock, ``populate_existing``). Ids that are absent or outside the scope are simply missing
    from the result, exactly where the single read returns ``None``. At most 500 distinct ids.
    """
    unique = list(dict.fromkeys(source_ids))
    if len(unique) > 500:
        raise ValueError("At most 500 source fences may be read at once")
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not unique:
        return {}
    rows = await session.scalars(
        select(Source)
        .where(
            Source.id.in_(unique),
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    return {row.id: source_fence_of(row) for row in rows}


async def github_retirement_peer_projection_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> Select[UUID]:
    """Return sibling IDs solely for exact archived GitHub provider-revoke safety.

    Internal retirement caller holds ordered account/workspace/membership, anchor Source,
    provisioning and relevant grant locks in this transaction. Fresh real owner/default-
    workspace admission and complete access/Source fences must match the unchanged scope,
    including any bound source generation; member, absent, stale, foreign, non-GitHub or
    nonarchived anchors are denied. Canonical deletion cannot authorize this live-anchor
    query from a bare UUID. No locks, mutations, commit or provider I/O occur here.

    The purpose-bound sibling relation selects only one column named id in the verified
    anchor workspace, excluding the anchor and archived peers. It exposes no rows, ORM,
    configuration, content or credentials and does not replace/strip the bound principal.
    These IDs grant no peer read/write authority or general Source enumeration. Connector
    joins its token-bearing same-remote-user grants before ordering and LIMIT101; this
    projection has no provider filter, order or limit, preserving conservative peer safety.
    Execute only in this held transaction: the retained workspace lock serializes lifecycle
    and grant publication by converted writers, so no later peer Source locks are acquired.
    """
    anchor = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if anchor is None or anchor.status != "archived" or anchor.provider != "github":
        raise ConflictError(detail="GitHub retirement anchor is unavailable")
    return select(Source.id).where(
        Source.workspace_id == anchor.workspace_id,
        Source.id != anchor.id,
        Source.status != "archived",
    )


def ingestion_lifecycle_projection(*, scope: Scope) -> Select[UUID, str, int]:
    """Return the minimal source lifecycle read projection for ingestion retry aggregation.

    Cross-module callers may correlate ingestion-owned retry records against the
    current source identity, status, and generation. Configuration and content
    remain private. Caller admits owner scope before execution; this projection is already
    workspace/source-generation filtered and grants no write authority.
    """
    return select(Source.id, Source.status, Source.generation).where(*_source_scope(scope))


def ingestion_instance_lifecycle_projection(*, instance_operator: bool) -> Select[UUID, str, int]:
    """Expose only instance Source lifecycle columns to an already-admitted bootstrap operator.

    Internal ingestion observability callers prove real operator authority before this pure
    projection. Never derive the capability from an HTTP body or workspace owner role; no
    metadata/configuration/credentials/content or implicit workspace grant is returned.
    """
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    return select(Source.id, Source.status, Source.generation)


async def lock_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourceFence | None:
    """Lock access before one Source and return its scoped lifecycle; missing returns None.

    Called before any domain locks; use lock_source_set for multiple Sources in a transaction.
    """
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    return source_fence_of(source)


async def lock_retained_evidence_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourceFence | None:
    """Share-lock a source while confirming retained data remains eligible for owner reads/effects.

    Paused and connector-only archived sources remain eligible. A queued, running, or failed
    with-data purge revokes eligibility immediately, even while immutable document rows linger.
    The PostgreSQL ``FOR SHARE`` lock is compatible with other evidence readers but conflicts with
    Source lifecycle ``FOR UPDATE`` mutations. Use lock_source_set(retained_evidence=True)
    for multiple Sources before Documents/domain locks; hold until publication commit.
    """
    await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=expected_access_fence,
    )
    fences = await _lock_source_fence_rows(
        session, (source_id,), scope=scope, retained_evidence=True
    )
    return fences[0] if fences is not None else None


async def _lock_source_fence_rows(
    session: AsyncSession,
    source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    retained_evidence: bool,
) -> tuple[SourceFence, ...] | None:
    """Lock an admitted sorted Source set and return nothing unless the entire set matches.

    Caller already holds current auth/workspace/membership locks. This helper takes no earlier
    locks, grants no admission, performs no commit, and constructs DTOs only after completeness
    and retained purge predicates hold. Single and batch retained reads use the same logic.
    """
    if not source_ids:
        return ()
    rows = list(
        await session.scalars(
            select(Source)
            .where(
                Source.id.in_(source_ids),
                *_source_scope(scope),
            )
            .order_by(Source.id)
            .with_for_update(read=retained_evidence)
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) != len(source_ids):
        return None
    if retained_evidence:
        eligible_ids = set(
            await session.scalars(
                export_eligible_source_ids(scope=scope).where(
                    Source.id.in_(source_ids),
                )
            )
        )
        if eligible_ids != set(source_ids):
            return None
    return tuple(source_fence_of(source) for source in rows)


async def lock_source_set(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    retained_evidence: bool = False,
    expected_access_fence: AccessFence | None = None,
) -> SourceFenceSet:
    """Admit once, then lock a complete deduplicated Source set in UUID order, bounded to 500.

    Ordinary mode preserves lifecycle snapshots; retained mode uses share locks and denies
    queued/running/failed data purges while allowing pause/connector-only archive. Any missing,
    foreign or exact-generation mismatch fails the whole set with 404, without partial DTOs.
    Caller holds no prior domain locks and owns transaction release, publication and commit;
    pass the captured fence for deferred CAS and use the returned fence for scoped replay.
    """
    if len(source_ids) > 500 or any(not isinstance(identifier, UUID) for identifier in source_ids):
        raise ValueError("Source lock set requires at most 500 UUIDs")
    if type(retained_evidence) is not bool:
        raise TypeError("Source lock mode must be a boolean")
    ordered = tuple(sorted(set(source_ids), key=str))
    access_fence = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=expected_access_fence,
    )
    fences = await _lock_source_fence_rows(
        session, ordered, scope=scope, retained_evidence=retained_evidence
    )
    if fences is None:
        raise NotFoundError(detail="Source set is unavailable")
    return SourceFenceSet(fences=fences, access_fence=access_fence)


async def filter_active_source_ids(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[UUID, ...]:
    """Return only owner sources that are active and still eligible for current reads."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not source_ids or len(source_ids) > 32:
        return ()
    rows = await session.scalars(
        select(Source.id).where(
            Source.id.in_(source_ids),
            Source.status == "active",
            Source.retired_at.is_(None),
            *_source_scope(scope),
            Source.id.in_(export_eligible_source_ids(scope=scope)),
        )
    )
    return tuple(rows.all())


async def lock_source_for_document(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> None:
    """Validate the source and hold its lock until the caller's transaction ends."""
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        raise LookupError("Source not found")
    if source.status != "active":
        raise ValueError("Cannot add documents to an inactive source")


async def set_connector_configuration(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    configuration: dict[str, object],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    allow_paused: bool = False,
    expected_access_fence: AccessFence | None = None,
) -> ConnectorSource | None:
    """Replace only admitted workspace configuration under matching generation/lifecycle locks.

    Optional captured access fence is rechecked before Source; deepcopy and flush only, no commit.
    """
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None or source.status == "archived" or source.generation != expected_generation:
        return None
    if source.status != "active" and not (allow_paused and source.status == "paused"):
        return None
    source.generation += 1
    source.configuration = deepcopy(configuration)
    await session.flush()
    return _connector_source(source)


async def record_collection_started(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush collection-start timestamps after ordered access/Source and exact-generation checks.

    No external work or commit; caller releases these locks before fetching provider content.
    """
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_collection_started(session, source, expected_generation, at)


async def _record_collection_started(
    session: AsyncSession,
    source: Source | None,
    expected_generation: int,
    at: datetime,
) -> bool:
    """Flush start timestamps for the current active exact Source generation; acquire no locks.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    source.last_sync_at = at
    source.collected_at = at
    await session.flush()
    return True


async def record_collection_result(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    no_changes: bool = False,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush collection outcome only under current access and active exact-generation locks.

    Deferred provider publishers pass their captured access fence for configuration CAS, in a
    fresh transaction after network work. This helper introduces no network I/O or commit.
    """
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_collection_result(
        session, source, expected_generation, at, error_code, no_changes=no_changes
    )


async def _record_collection_result(
    session: AsyncSession,
    source: Source | None,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    no_changes: bool = False,
) -> bool:
    """Flush the unchanged success/error/no-change health rules for an active exact generation.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    if error_code is None:
        source.last_success_at = at
        source.last_error_code = None
        source.collection_error_code = None
        if no_changes:
            source.last_sync_at = at
            source.collected_at = at
            source.last_error_at = None
    else:
        source.collection_error_code = error_code
        source.last_error_code = error_code
        source.last_error_at = at
    await session.flush()
    return True


async def record_processing_result(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush processing outcome under ordered access/Source and active generation fences.

    Deferred publishers pass captured access fence; no commit or model/provider I/O occurs here.
    """
    source, _access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_processing_result(session, source, expected_generation, at, error_code)


async def _record_processing_result(
    session: AsyncSession,
    source: Source | None,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
) -> bool:
    """Flush existing processing success/error health fields for an active exact generation.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    if error_code is None:
        source.last_success_at = at
        source.last_error_code = None
        source.processing_error_code = None
    else:
        source.processing_error_code = error_code
        source.last_error_code = error_code
        source.last_error_at = at
    await session.flush()
    return True


async def _source_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> Source | None:
    """Revalidate fresh scope and exact Source under the caller's existing ordered locks.

    Caller acquired auth/workspace/membership then Source before run/state/domain locks and
    retains that transaction. Detached values are required snapshots, never lock proof; this
    internal owner path cannot be exposed as HTTP authority. Current access disagreement409
    and unavailable/mismatching Source returns None. No locks, commit or remote I/O acquired.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise TypeError("Captured access and Source fences are required")
    current_access = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence:
        raise ConflictError(detail="Workspace access fence changed")
    if source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id:
        return None
    source = await session.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if source is None or (
        source.generation != source_fence.generation
        or source.status != source_fence.status
        or source.local_only != source_fence.local_only
    ):
        return None
    return source


async def record_collection_started_in_uow(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> bool:
    """Flush a collection start using exact caller-held access/Source snapshots, without locking.

    Internal composition only: caller retains ordered admission/Source locks before run/state
    locks. Fresh access is revalidated; inactive/missing/stale Source returns False. Same health
    behavior as the acquiring wrapper, no commit or network and no authority from DTO creation.
    """
    source = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    return await _record_collection_started(session, source, expected_generation, at)


async def record_collection_result_in_uow(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    no_changes: bool = False,
) -> bool:
    """Flush existing success/error/no-change rules under fresh caller-held Source/access proof.

    Caller retains ordered admission/Source locks before run/state/domain locks; DTOs cannot
    create that authority and this seam is never HTTP admission. Missing/inactive/stale Source
    returns False; fresh access mismatch409. No earlier lock, commit or provider I/O occurs.
    """
    source = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    return await _record_collection_result(
        session, source, expected_generation, at, error_code, no_changes=no_changes
    )


async def record_processing_result_in_uow(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> bool:
    """Flush processing success/error through fresh proof of the caller's locked exact Source.

    Internal composition requires actual ordered admission/Source locks before downstream
    domain locks, retained until commit. Missing/inactive/stale returns False; current access
    mismatch409. This only revalidates nonlocking snapshots and flushes existing health rules.
    """
    source = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    return await _record_processing_result(session, source, expected_generation, at, error_code)
