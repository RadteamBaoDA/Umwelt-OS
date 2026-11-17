"""Source update, pause, archive and purge lifecycle."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.events import DomainEvent
from core.ids import uuid7
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.sources._fences import (
    _connector_source,
    _lock_source_row,
    _source_in_uow,
    source_fence_of,
)
from modules.sources._purge_jobs import (
    _fence_connector_source,
    _reconcile_github_hint_lifecycle,
    _transitioned_source_scope,
)
from modules.sources._scope import _admit_source_scope, _operation_scope
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import ConnectorSource, OperationRead, SourceFence, SourcePatch


async def update_source(
    session: AsyncSession,
    source: Source,
    payload: SourcePatch,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source | None:
    """Apply an admitted Source patch with ordered inactive cleanup or active hint rearm.

    ``graph_enabled`` / ``rag_index_enabled`` are plain owner switches applied without a generation bump.
    Pausing or archiving fences collection before pending hints are terminalized in this same
    transaction. Explicit reactivation rearms only hints whose current grant and provisioning
    binding match the new source generation; it never resumes a stale OAuth binding.
    Requery the supplied ORM identity in the owner's workspace after access CAS locks; retain
    this wrapper's scoped replay/commit and hand replay the captured locked access fence.
    """
    locked_source, access_fence = await _lock_source_row(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if locked_source is None:
        return None
    source = locked_source
    changed = False
    if source.status == "archived" and payload.status not in (None, "archived"):
        raise ValueError("Archived sources cannot be reactivated")
    if "name" in payload.model_fields_set:
        value = payload.name or ""
        changed = changed or source.name != value
        source.name = value
    if "status" in payload.model_fields_set:
        next_status = payload.status or ""
        if next_status != source.status:
            changed = True
            source.generation += 1
            source.status = next_status
            source.retired_at = datetime.now(UTC) if next_status in {"paused", "archived"} else None
            if next_status in {"paused", "archived"}:
                await _fence_connector_source(
                    session,
                    source,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence,
                )
            if source.provider == "github" and next_status == "active":
                await _reconcile_github_hint_lifecycle(
                    session,
                    source,
                    active=next_status == "active",
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
    # Toggle changes are not lifecycle transitions: no generation bump (that would fence in-flight
    # collection); the change event alone lets clients refresh. Workers re-read the flags per job.
    for flag in ("graph_enabled", "rag_index_enabled"):
        if flag in payload.model_fields_set and getattr(payload, flag) is not None:
            flag_value = bool(getattr(payload, flag))
            changed = changed or getattr(source, flag) != flag_value
            setattr(source, flag, flag_value)
    drafts = (
        [make_source_change(source.id, source.generation, source.status, scope=scope)]
        if changed
        else []
    )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def pause_source_for_connector(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> ConnectorSource | None:
    """Pause an admitted exact-generation Source and run ordered cleanup in caller transaction.

    Access CAS locks precede Source; one verified generation transition is forwarded to scoped
    cleanup callees. Flush only, no commit or provider I/O; caller releases before external work.
    """
    source, access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None or source.status == "archived":
        return None
    if source.status != "paused":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(
        session,
        source,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.flush()
    return _connector_source(source)


async def prepare_source_pause_for_connector_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Prepare exact Source pause cleanup rows without mutating or granting new authority.

    Internal acceptance caller already holds ordered account/workspace/Source/provisioning
    locks and calls before GitHub grant/state/run locks. Fresh active or paused G/access must
    match the complete captured fences; unavailable Source raises409. Both active G->paused
    G+1 and identical paused G cleanup prepare Connector slots/browser grant, Ingestion token
    rows, then Tools jobs/evidence in their owner-defined sorted order. No Source transition,
    revocation, receipt, hint/capacity change, commit or network occurs here. Preparation
    returns no token; caller retains this transaction through exact proof and late apply.
    """
    source = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused"}:
        raise ConflictError(detail="Source pause preparation is unavailable")
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import lock_source_browser_results_in_uow

    await connectors.prepare_source_pause_cleanup_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await ingestion.lock_source_credentials_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await lock_source_browser_results_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def pause_source_for_connector_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> ConnectorSource | None:
    """Apply prepared active G->paused G+1 or exact paused G cleanup with the same principal.

    Internal caller retains actual ordered Source/provisioning/cleanup locks plus the complete
    hint set before capacity; validated acceptance precedes apply. Fresh original G/access
    proof is checked before mutation; missing, archived or mismatching Source returns None.
    Active Source transitions once; already-paused Source retains generation, retired_at and
    the exact supplied cleanup scope. Only active-transition cleanup callees receive the
    narrowly transitioned scope; no collector readmission or successor principal escapes.
    Both cases repeat complete credential/browser/provisioning/hint effects using prepared-row
    owner mutations, never an acquiring lifecycle helper. Singular Source browser purge takes
    the original AccessFence and actual paused current fence with only that authorized scope
    transition; historical jobs gain no successor authority. Flush only; caller skips inactive
    health recording and commits atomically, with G+1 replay only for the actual transition.
    """
    source = await _source_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused"}:
        return None
    cleanup_scope = scope
    if source.status == "active":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
        cleanup_scope = _transitioned_source_scope(scope, source)
    paused_fence = source_fence_of(source)
    # Owner mutation hooks freshly read the paused row; persist it within the held transaction.
    await session.flush()
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import purge_source_browser_results_in_uow

    await ingestion.revoke_source_credentials(
        session,
        source.id,
        scope=cleanup_scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await purge_source_browser_results_in_uow(
        session,
        source.id,
        scope=cleanup_scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=paused_fence,
    )
    await connectors.apply_source_pause_cleanup_in_uow(
        session,
        source_fence=paused_fence,
        previous_source_fence=source_fence,
        scope=cleanup_scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.flush()
    return _connector_source(source)


async def archive_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source | None:
    """Archive in the owner workspace under ordered access CAS/Source locks and scoped replay.

    Connector/token/browser preparation precedes mutations, retirement and full hint release;
    the captured access fence is reused by replay without taking earlier locks again.
    """
    source, access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    changed = source.status != "archived"
    if source.status != "archived":
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(
        session,
        source,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    drafts = (
        [make_source_change(source.id, source.generation, source.status, scope=scope)]
        if changed
        else []
    )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def start_source_purge(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourcePurgeOperation | None:
    """Queue Source purge after ordered lifecycle cleanup, then own outbox/replay commit.

    New operations carry no raw URI snapshot: Documents creates durable per-document cleanup
    receipts under its publication identity locks before the canonical cascade. The legacy
    JSON column remains readable only for explicit compatibility repair.
    Capture workspace, actual actor and membership revision on the retained operation and
    event atomically; preserve this wrapper's scoped replay commit without provider I/O.
    """
    source, access_fence = await _lock_source_row(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    current = await session.scalar(
        select(SourcePurgeOperation)
        .where(
            SourcePurgeOperation.source_id == source_id,
            *_operation_scope(scope),
        )
        .order_by(SourcePurgeOperation.created_at.desc())
        .limit(1)
    )
    if current is not None and (
        source.status == "archived" or current.status in {"queued", "running"}
    ):
        return current

    changed = source.status != "archived"
    if changed:
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    operation = SourcePurgeOperation(
        workspace_id=scope.workspace_id,
        actor_user_id=scope.user_id,
        membership_revision=scope.membership_revision,
        configuration_revision=access_fence.configuration_revision,
        source_id=source_id,
        generation=source.generation,
        raw_uris=[],
        pending_owner_codes=["documents"],
    )
    session.add(operation)
    await session.flush()
    from modules.ingestion import public as ingestion

    await _fence_connector_source(
        session,
        source,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    now = datetime.now(UTC)
    event = DomainEvent(
        id=uuid7(),
        type="source.purge.requested",
        version=1,
        occurred_at=now,
        producer="modules.sources",
        payload={
            "operation_id": str(operation.id),
            "workspace_id": str(operation.workspace_id),
            "actor_user_id": operation.actor_user_id,
            "membership_revision": operation.membership_revision,
            "source_id": str(operation.source_id),
            "source_generation": operation.generation,
        },
    )
    await ingestion.publish_event(
        session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    drafts = [
        make_source_change(
            source.id, source.generation, source.status, operation_id=operation.id, scope=scope
        )
    ]
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(operation)
    return operation


# Source-local Memory coverage that can never improve without new evidence: durable unavailable.
SOURCE_MEMORY_TERMINAL_CODES = frozenset(
    {"evidence_identity_unavailable", "legacy_provenance_unresolved"}
)


def _open_coverage_operations() -> tuple[ColumnElement[bool], ...]:
    """Select canonical-complete operations whose full-copy status can still change.

    Terminal-unavailable Memory coverage is excluded so a permanent gap never starves queued work
    or hot-loops; it only reopens through a new, explicit purge decision.
    """
    return (
        SourcePurgeOperation.documents_status == "deleted",
        SourcePurgeOperation.status != "succeeded",
        ~and_(
            SourcePurgeOperation.memory_status == "failed",
            SourcePurgeOperation.memory_error_code.in_(SOURCE_MEMORY_TERMINAL_CODES),
        ),
    )


async def list_source_purge_observer_ids(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return <=100 exact unfinished purge operation IDs for one Source, keyset by ID.

    Documents uses this detached identity list to wake observers of a historical (unlinked)
    receipt without touching Source models. No ordering of unrelated operations is exposed.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Source observer page size must be between 1 and 100")
    statement = select(SourcePurgeOperation.id).where(
        SourcePurgeOperation.source_id == source_id,
        *_operation_scope(scope),
        *_open_coverage_operations(),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all()
    )


async def discover_source_purge_observer_ids(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    source_id: UUID,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Identity-only keyset discovery of open purge operation IDs for one Source (IDs only).

    Deliberately carries no membership/generation predicate or authority: a historical or
    other-epoch operation is only a wakeup candidate; every ID is admitted by its own exact
    resolver in a fresh session before any effect. No route exposes this.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Source observer page size must be between 1 and 100")
    statement = select(SourcePurgeOperation.id).where(
        SourcePurgeOperation.workspace_id == workspace_id,
        SourcePurgeOperation.source_id == source_id,
        *_open_coverage_operations(),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all()
    )


def coverage_settled_exclusion() -> ColumnElement[bool]:
    """Exclude a failed purge only when Memory is done AND every child coverage has settled."""
    return ~and_(
        SourcePurgeOperation.status == "failed",
        SourcePurgeOperation.memory_status == "succeeded",
        SourcePurgeOperation.memory_cache_pending.is_(False),
        SourcePurgeOperation.pending_child_count.is_not(
            None
        ),  # NULL = unsettled, never NULL-propagating
        SourcePurgeOperation.pending_child_count == 0,
        SourcePurgeOperation.pending_owner_codes == cast([], JSONB),
    )


async def pending_source_coverage_ids(
    session: AsyncSession,
    *,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return one bounded keyset page of purge operations needing coverage reconciliation."""
    if not isinstance(scope, InternalJobScope):
        raise TypeError("Source coverage reconciliation requires an internal scope")
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Source coverage reconciliation page size must be between 1 and 100")
    # A failed purge stays eligible while any child coverage is unsettled (wakeups are not crash-safe).
    statement = select(SourcePurgeOperation.id).where(
        *_open_coverage_operations(),
        *_operation_scope(scope),
        coverage_settled_exclusion(),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all()
    )


async def source_data_purge_exists(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> bool:
    """Report whether any data purge (in any state) ever fenced this Source.

    Export eligibility reopens when a purge succeeds; copied-evidence producers must not, so they
    use this durable fence instead. Callers hold the Source lock, which serializes this read with
    ``start_source_purge``.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    # A purge of another generation still fences copied evidence forever in this workspace.
    if isinstance(scope, InternalJobScope) and scope.source_id not in (None, source_id):
        return False
    return (
        await session.scalar(
            select(SourcePurgeOperation.id)
            .where(
                SourcePurgeOperation.source_id == source_id,
                SourcePurgeOperation.workspace_id == scope.workspace_id,
            )
            .limit(1)
        )
        is not None
    )


async def read_source_purge_operation(
    session: AsyncSession, operation_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> OperationRead | None:
    """Return the allowlisted public progress projection for one Source purge receipt.

    The exact Source-owned ID lookup exposes aggregate stage/count fields only; it never returns
    legacy URI JSON, child receipt IDs, event payloads, or owner-private cleanup data.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    operation = await session.scalar(
        select(SourcePurgeOperation)
        .where(
            SourcePurgeOperation.id == operation_id,
            *_operation_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if operation is None:
        return None
    return OperationRead(
        operation_id=operation.id,
        workspace_id=operation.workspace_id,
        source_id=operation.source_id,
        status=operation.status,
        error_code=operation.error_code,
        documents_status=operation.documents_status,
        pending_child_count=operation.pending_child_count,
        failed_child_count=operation.failed_child_count,
        pending_owner_codes=list(operation.pending_owner_codes),
        memory_status=operation.memory_status,
        memory_error_code=operation.memory_error_code,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )
