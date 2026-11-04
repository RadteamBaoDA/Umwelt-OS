"""Process durable News readiness receipts under source and document fences."""

import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import worker_cursors
from core.config import Settings
from core.errors import DomainError
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.news.models import NewsRecoveryCheckpoint
from modules.news.stories import cluster_observation
from modules.settings import public as settings_public
from modules.sources import public as sources

RECOVERY_CURSOR_KEY = "news:recovery:workspace-cursor"
RECOVERY_INIT_CURSOR_KEY = "news:recovery:initialization-cursor"


_log = logging.getLogger(__name__)
CURSOR_STATE_KEY = worker_cursors.STATE_KEY
_CURSOR_KEYS = frozenset({RECOVERY_CURSOR_KEY, RECOVERY_INIT_CURSOR_KEY})


async def _read_cursor(ctx: dict[str, object], key: str) -> UUID | None:
    """Read a fixed recovery cursor through the shared stale-write-guarded cursor store."""
    return await worker_cursors.read_cursor(ctx, key, _CURSOR_KEYS)


async def _write_cursor(ctx: dict[str, object], key: str, cursor: UUID | None) -> None:
    """Write a fixed recovery cursor through the shared stale-write-guarded cursor store."""
    await worker_cursors.write_cursor(ctx, key, cursor, _CURSOR_KEYS)


async def _ensure_recovery_checkpoint(session: AsyncSession, workspace_id: UUID) -> None:
    """Flush a missing workspace checkpoint after current owner admission and before commit."""
    checkpoint = await session.scalar(select(NewsRecoveryCheckpoint).where(
        NewsRecoveryCheckpoint.workspace_id == workspace_id,
    ).with_for_update())
    if checkpoint is None:
        session.add(NewsRecoveryCheckpoint(workspace_id=workspace_id))
        await session.flush()


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Read the worker-owned async database session factory from ARQ context."""
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


@dataclass(frozen=True)
class _PreparedEvidence:
    """Source/Document evidence prepared (and locked) for one ready-event receipt."""

    provenance: Any
    refreshed: Any
    source_fence: Any
    document_locked: bool


async def _prepare_ready_evidence(
    session: AsyncSession, identifier: UUID, delivery: Any, access_fence: AccessFence, *,
    scope: InternalJobScope, flag: bool,
) -> _PreparedEvidence:
    """Lock the receipt's Source then Document and re-read its provenance and envelope.

    Order: provenance lookup, ``lock_source`` (sorted-first in the domain lock order), then
    ``lock_document_ids`` only for an active source, then a projection/provenance/delivery
    recheck. ``document_locked`` stays true only when every recheck still matches the original
    receipt. The Ingestion outbox row is NOT locked here (it is the final lock). Caller owns the
    transaction.
    """
    provenance = await ingestion.resolve_ready_event_provenance(
        session, identifier, scope=scope, multi_workspace_enabled=flag,
    )
    source_fence = None
    document_locked = False
    refreshed = None
    if provenance is not None and (
        provenance.workspace_id == scope.workspace_id
        and provenance.actor_user_id == scope.actor_user_id
        and provenance.membership_revision == scope.membership_revision
    ):
        source_fence = await sources.lock_source(
            session, provenance.source_id, scope=scope,
            multi_workspace_enabled=flag, expected_access_fence=access_fence,
        )
        if source_fence is not None and source_fence.status == "active":
            locked = await documents.lock_document_ids(
                session, [provenance.document_id], scope=scope,
                multi_workspace_enabled=flag,
            )
            document_locked = provenance.document_id in locked
        if document_locked:
            current = await documents.get_news_document_projection(
                session, provenance.document_id,
                expected_source_generation=provenance.source_generation,
                scope=scope, multi_workspace_enabled=flag,
            )
            refreshed = await ingestion.resolve_ready_event_provenance(
                session, identifier, scope=scope, multi_workspace_enabled=flag,
            )
            current_delivery = await ingestion.get_event_delivery(
                session, identifier, scope=scope, multi_workspace_enabled=flag,
            )
            document_locked = (
                current is not None
                and current.source_id == provenance.source_id
                and current.document_version_id == provenance.document_version_id
                and current.version_number == provenance.version_number
                and current.current_source_generation == provenance.source_generation
                and refreshed == provenance
                and current_delivery == delivery
            )
    return _PreparedEvidence(provenance, refreshed, source_fence, document_locked)


def _prepared_matches(prepared: _PreparedEvidence, scope: InternalJobScope) -> bool:
    """Return whether the prepared locks still prove the receipt's exact original authority. Pure."""
    provenance, source_fence = prepared.provenance, prepared.source_fence
    return bool(
        provenance is not None and prepared.refreshed == provenance
        and provenance.workspace_id == scope.workspace_id
        and provenance.actor_user_id == scope.actor_user_id
        and provenance.membership_revision == scope.membership_revision
        and source_fence is not None and source_fence.status == "active"
        and source_fence.generation == provenance.source_generation
        and prepared.document_locked
    )


def _event_matches_provenance(event: Any, delivery: Any, provenance: Any) -> bool:
    """Return whether the locked outbox row and its unlocked envelope both agree with ``provenance``. Pure."""
    return bool(
        event.workspace_id == provenance.workspace_id
        and event.actor_user_id == provenance.actor_user_id
        and event.membership_revision == provenance.membership_revision
        and event.payload.get("source_id") == str(provenance.source_id)
        and event.payload.get("document_id") == str(provenance.document_id)
        and event.payload.get("document_version_id") == str(provenance.document_version_id)
        and event.payload.get("source_generation") == provenance.source_generation
        and event.payload.get("version_number") == provenance.version_number
        and delivery.type == "news.document.ready"
        and delivery.version == 1
        and delivery.payload.get("source_id") == str(provenance.source_id)
        and delivery.payload.get("document_id") == str(provenance.document_id)
        and delivery.payload.get("document_version_id") == str(provenance.document_version_id)
        and delivery.payload.get("source_generation") == provenance.source_generation
        and delivery.payload.get("version_number") == provenance.version_number
    )


async def _process_ready_event(
    session: AsyncSession, identifier: UUID, scope: InternalJobScope, flag: bool,
) -> None:
    """Run the admitted receipt transaction: lock order, cluster, then ACK or fail the outbox row.

    Lock order: original admission, Source, Document, checkpoint, then the outbox row last.
    Every early ``return`` leaves the session uncommitted for the caller (rollback happens where
    the receipt changed). Admission-denial exceptions propagate to the caller's handler.
    """
    access_fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=flag,
    )
    if not await settings_public.module_is_enabled(
        session, "news", scope=scope, multi_workspace_enabled=flag,
    ):
        return
    # Lock original admission (account -> workspace -> membership) on every
    # branch, including malformed/stale receipts, before any Source,
    # checkpoint or outbox lock. The captured fence stays the original one.
    await workspaces.lock_access_fence(
        session, scope=scope, expected=access_fence, multi_workspace_enabled=flag,
    )

    # Discover the bounded outbox envelope without a lock. The row itself is
    # acquired only after the exact Source and Document evidence is prepared.
    delivery = await ingestion.get_event_delivery(
        session, identifier, scope=scope, multi_workspace_enabled=flag,
    )
    if delivery is None or delivery.status in ("delivered", "failed"):
        return
    prepared = await _prepare_ready_evidence(
        session, identifier, delivery, access_fence, scope=scope, flag=flag,
    )
    provenance = prepared.provenance
    prepared_match = _prepared_matches(prepared, scope)
    if prepared_match and provenance is not None:  # second clause narrows for mypy
        await cluster_observation(
            session, document_id=provenance.document_id,
            expected_source_generation=provenance.source_generation,
            scope=scope, multi_workspace_enabled=flag,
        )

    # Checkpoint and News writes follow the prepared Source/Document locks;
    # the outbox row is the final lock in this transaction's evidence path.
    await _ensure_recovery_checkpoint(session, scope.workspace_id)
    event = await ingestion.lock_news_document_ready_event(
        session, identifier, scope=scope, multi_workspace_enabled=flag,
    )
    if event is None or event.status in ("delivered", "failed"):
        return
    locked_delivery = await ingestion.get_event_delivery(
        session, identifier, scope=scope, multi_workspace_enabled=flag,
    )
    if locked_delivery != delivery:
        # The exact envelope changed while Source/Document evidence was prepared.
        await session.rollback()
        return
    if (delivery.type != "news.document.ready" or delivery.version != 1
            or event.version != 1 or not event.valid_payload):
        await ingestion.fail_news_document_ready_event(
            session, identifier, scope=scope, multi_workspace_enabled=flag,
        )
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=flag,
            access_fence=access_fence,
        )
        return

    event_matches = (
        prepared_match and provenance is not None  # narrows for mypy; implied by prepared_match
        and _event_matches_provenance(event, delivery, provenance)
    )
    if not event_matches and prepared_match:
        # A changed receipt invalidates the uncommitted News writes above.
        await session.rollback()
        return
    await ingestion.mark_news_document_ready_event_delivered(
        session, identifier, scope=scope, multi_workspace_enabled=flag,
    )
    await commit_with_replay(
        session, [], scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence,
    )


async def process_news_document_ready(ctx: dict[str, object], event_id: str) -> None:
    """Cluster one immutable ready version and ACK its outbox event atomically.

    Admit the retained event principal and lock its original admission first on
    every branch (including malformed or stale receipts), then lock and recheck Source and
    Document evidence before locking the Ingestion outbox row. Exact provenance
    must still match after those domain locks; stale receipts are terminally ACKed
    without recreating a story. No external I/O occurs in this transaction.
    Admission denials (401/403/404/409, HTTPException or DomainError) roll back and end the job.
    """
    try:
        identifier = UUID(event_id)
    except ValueError:
        return
    settings = cast(Settings, ctx["settings"])
    flag = settings.multi_workspace_enabled
    async with _factory(ctx)() as session:
        try:
            scope = await ingestion.resolve_ingestion_event_scope(
                session, identifier, multi_workspace_enabled=flag,
            )
        except (HTTPException, DomainError):
            # Denied retained principal: no mutation and no retry; the dispatcher quarantines the event.
            await session.rollback()
            return
        if scope is None:
            return
        try:
            await _process_ready_event(session, identifier, scope, flag)
        except (HTTPException, DomainError) as exc:
            if exc.status_code in {401, 403, 404, 409}:
                await session.rollback()
                return
            raise


_DENIAL_STATUSES = {401, 403, 404, 409}


async def _initialize_workspace_checkpoint(
    factory: async_sessionmaker[AsyncSession], workspace_id: UUID, flag: bool,
) -> None:
    """Create one legacy workspace's missing checkpoint under the original admission lock.

    Order: owner resolution, read fence, module gate, ``lock_access_fence``, the per-workspace
    legacy catch-up advisory lock, checkpoint flush, then one commit with replay. Denial
    statuses (401/403/404/409) roll back and skip the workspace; other errors propagate.
    """
    async with factory() as session:
        try:
            owner = await workspaces.resolve_workspace_owner_context(
                session, workspace_id, multi_workspace_enabled=flag,
            )
            if owner is None:
                return
            scope = InternalJobScope(
                workspace_id=workspace_id, actor_user_id=owner.user_id,
                membership_revision=owner.membership_revision,
            )
            access_fence = await workspaces.read_access_fence(
                session, scope=scope, multi_workspace_enabled=flag,
            )
            if not await settings_public.module_is_enabled(
                session, "news", scope=scope, multi_workspace_enabled=flag,
            ):
                return
            access_fence = await workspaces.lock_access_fence(
                session, scope=scope, expected=access_fence,
                multi_workspace_enabled=flag,
            )
            await session.execute(text(
                "SELECT pg_advisory_xact_lock(hashtextextended('news:legacy-catchup:' || :workspace_id, 0))"
            ), {"workspace_id": str(workspace_id)})
            await _ensure_recovery_checkpoint(session, workspace_id)
            await commit_with_replay(
                session, [], scope=scope, multi_workspace_enabled=flag,
                access_fence=access_fence,
            )
        except (HTTPException, DomainError) as exc:
            if exc.status_code in _DENIAL_STATUSES:
                await session.rollback()
                return
            raise


async def _initialize_legacy_checkpoints(
    ctx: dict[str, object], factory: async_sessionmaker[AsyncSession], flag: bool,
) -> None:
    """Scan one bounded page of ready-document workspaces and initialize missing checkpoints.

    The initialization cursor advances independently of the recovery cursor: it moves to the
    last scanned workspace, wraps to the start when the tail is empty, and is cleared when no
    workspace has ready documents.
    """
    init_cursor = await _read_cursor(ctx, RECOVERY_INIT_CURSOR_KEY)
    async with factory() as session:
        initialization_ids = await documents.list_ready_document_workspace_ids(
            session, after=init_cursor, limit=100,
        )
        if not initialization_ids and init_cursor is not None:
            initialization_ids = await documents.list_ready_document_workspace_ids(
                session, after=None, limit=100,
            )
    if initialization_ids:
        for workspace_id in initialization_ids:
            await _initialize_workspace_checkpoint(factory, workspace_id, flag)
        await _write_cursor(ctx, RECOVERY_INIT_CURSOR_KEY, initialization_ids[-1])
    else:
        await _write_cursor(ctx, RECOVERY_INIT_CURSOR_KEY, None)


async def _list_document_projections(
    session: AsyncSession, checkpoint: NewsRecoveryCheckpoint, source_ids: tuple[UUID, ...], *,
    scope: InternalJobScope, flag: bool,
) -> tuple[list[documents.NewsDocumentProjection], str | None]:
    """Read the next ready-document page for the checkpoint's source page.

    The stored cursor is bound to a fingerprint of the access fence and source page; a
    revision bump or source change makes it permanently undecodable. Only that cursor
    ``ValueError`` restarts the source page (clustering is idempotent); anything else raises.
    """
    if not source_ids:
        return [], None
    try:
        return await documents.list_news_document_projections(
            session, source_ids=source_ids, limit=10, cursor=checkpoint.document_cursor,
            scope=scope, multi_workspace_enabled=flag,
        )
    except ValueError as exc:
        if checkpoint.document_cursor is None or "cursor" not in str(exc):
            raise
        return await documents.list_news_document_projections(
            session, source_ids=source_ids, limit=10, cursor=None,
            scope=scope, multi_workspace_enabled=flag,
        )


async def _cluster_recovery_page(
    session: AsyncSession, projections: list[documents.NewsDocumentProjection],
    source_ids: tuple[UUID, ...], access_fence: AccessFence, *, scope: InternalJobScope, flag: bool,
) -> int:
    """Lock sorted Sources, then sorted Documents, and cluster each still-current projection.

    Documents identity locks follow every Source fence (domain lock order). Each projection is
    re-read and must match generation, version and source before ``cluster_observation``.
    Returns the number clustered; caller owns the transaction.
    """
    projections.sort(key=lambda item: (str(item.source_id), str(item.document_id)))
    fences = {}
    for source_id in sorted(source_ids, key=str):
        fences[source_id] = await sources.lock_source(session, source_id, scope=scope,
            multi_workspace_enabled=flag, expected_access_fence=access_fence)
    processed = 0
    # Documents identity locks are acquired only after every sorted Source
    # fence. Titles are display metadata and never serve as raw URI identities.
    document_ids = sorted({item.document_id for item in projections}, key=str)
    locked_documents = set(await documents.lock_document_ids(
        session, document_ids, scope=scope, multi_workspace_enabled=flag,
    ))
    for projection in projections:
        source = fences.get(projection.source_id)
        if (source is None or source.status != "active"
                or source.generation != projection.current_source_generation
                or projection.document_id not in locked_documents):
            continue
        current = await documents.get_news_document_projection(
            session, projection.document_id,
            expected_source_generation=source.generation,
            scope=scope, multi_workspace_enabled=flag,
        )
        if (current is None
                or current.document_version_id != projection.document_version_id
                or current.version_number != projection.version_number
                or current.source_id != projection.source_id
                or current.current_source_generation != projection.current_source_generation):
            continue
        await cluster_observation(session, document_id=current.document_id,
            expected_source_generation=current.current_source_generation, scope=scope,
            multi_workspace_enabled=flag)
        processed += 1
    return processed


async def _recover_checkpoint_page(
    session: AsyncSession, workspace_id: UUID, access_fence: AccessFence, *,
    scope: InternalJobScope, flag: bool,
) -> int:
    """Cluster one source/document page for the workspace checkpoint and advance its cursors.

    Cursor publication is serialized behind the Source and Document locks (matching the domain
    lock order) by the legacy catch-up advisory lock; if another worker advanced the checkpoint
    meanwhile, this page is rolled back and retried later. Returns the count clustered (0 for a
    missing checkpoint or a lost race). Commits only on success.
    """
    checkpoint = await session.scalar(select(NewsRecoveryCheckpoint).where(
        NewsRecoveryCheckpoint.workspace_id == workspace_id,
    ))
    if checkpoint is None:
        return 0
    initial_source_cursor = checkpoint.source_cursor
    initial_document_cursor = checkpoint.document_cursor
    source_page = await sources.list_active_gadget_sources(session, scope=scope,
        multi_workspace_enabled=flag, limit=32, cursor=checkpoint.source_cursor)
    source_ids = tuple(item.id for item in source_page.items)
    projections, document_cursor = await _list_document_projections(
        session, checkpoint, source_ids, scope=scope, flag=flag,
    )
    workspace_processed = await _cluster_recovery_page(
        session, projections, source_ids, access_fence, scope=scope, flag=flag,
    )
    # Serialize cursor publication only after Source and Document locks, matching the
    # domain lock order. If another worker advanced meanwhile, retry from its cursor.
    await session.execute(text(
        "SELECT pg_advisory_xact_lock(hashtextextended('news:legacy-catchup:' || :workspace_id, 0))"
    ), {"workspace_id": str(workspace_id)})
    checkpoint = await session.scalar(select(NewsRecoveryCheckpoint).where(
        NewsRecoveryCheckpoint.workspace_id == workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (checkpoint is None or checkpoint.source_cursor != initial_source_cursor
            or checkpoint.document_cursor != initial_document_cursor):
        await session.rollback()
        return 0
    if document_cursor is None:
        checkpoint.source_cursor = source_page.next_cursor
        checkpoint.document_cursor = None
    else:
        checkpoint.document_cursor = document_cursor
    await commit_with_replay(session, [], scope=scope,
        multi_workspace_enabled=flag, access_fence=access_fence)
    return workspace_processed


async def _recover_workspace(
    factory: async_sessionmaker[AsyncSession], workspace_id: UUID, flag: bool,
) -> int:
    """Admit one checkpointed workspace owner and recover a page; returns documents clustered.

    Owner resolution happens outside the denial handler (its failures propagate). Inside it:
    read fence, module gate, then ``lock_access_fence`` before the page. Denial statuses roll
    back and count as zero; other errors propagate.
    """
    async with factory() as session:
        owner = await workspaces.resolve_workspace_owner_context(
            session, workspace_id, multi_workspace_enabled=flag,
        )
        if owner is None:
            return 0
        scope = InternalJobScope(workspace_id=workspace_id, actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision)
        try:
            access_fence = await workspaces.read_access_fence(session, scope=scope,
                multi_workspace_enabled=flag)
            if not await settings_public.module_is_enabled(session, "news", scope=scope,
                    multi_workspace_enabled=flag):
                return 0
            access_fence = await workspaces.lock_access_fence(session, scope=scope,
                expected=access_fence, multi_workspace_enabled=flag)
            return await _recover_checkpoint_page(
                session, workspace_id, access_fence, scope=scope, flag=flag,
            )
        except (HTTPException, DomainError) as exc:
            if exc.status_code in _DENIAL_STATUSES:
                await session.rollback()
                return 0
            raise


async def recover_news_work(ctx: dict[str, object]) -> int:
    """Initialize missing News checkpoints and catch up one finite ready-document page.

    PostgreSQL checkpoint and observation writes commit together. The source
    facade exposes only detached active identities; each page then locks sorted
    sources before sorted documents and revalidates current generation/version
    through Documents before clustering. A separate bounded identity scan
    initializes legacy workspaces with no checkpoint; it advances independently
    from the existing per-workspace recovery cursor. Returns documents clustered.
    """
    factory = _factory(ctx)
    settings = cast(Settings, ctx["settings"])
    await _initialize_legacy_checkpoints(ctx, factory, settings.multi_workspace_enabled)

    cursor = await _read_cursor(ctx, RECOVERY_CURSOR_KEY)
    async with factory() as session:
        statement = select(NewsRecoveryCheckpoint.workspace_id)
        if cursor is not None:
            statement = statement.where(NewsRecoveryCheckpoint.workspace_id > cursor)
        workspace_ids = list((await session.scalars(
            statement.order_by(NewsRecoveryCheckpoint.workspace_id).limit(100)
        )).all())
    if not workspace_ids:
        await _write_cursor(ctx, RECOVERY_CURSOR_KEY, None)
        return 0
    total_processed = 0
    flag = settings.multi_workspace_enabled
    for workspace_id in workspace_ids:
        total_processed += await _recover_workspace(factory, workspace_id, flag)
    await _write_cursor(ctx, RECOVERY_CURSOR_KEY, workspace_ids[-1])
    return total_processed
