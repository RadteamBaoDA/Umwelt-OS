"""Process durable News readiness receipts under source and document fences."""

import logging
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.news.models import NewsRecoveryCheckpoint
from modules.news.stories import cluster_observation
from modules.settings import public as settings_public
from modules.sources import public as sources

RECOVERY_CURSOR_KEY = "news:recovery:workspace-cursor"
RECOVERY_INIT_CURSOR_KEY = "news:recovery:initialization-cursor"


_log = logging.getLogger(__name__)
CURSOR_STATE_KEY = "w2_cursor_state"
_CURSOR_KEYS = frozenset({RECOVERY_CURSOR_KEY, RECOVERY_INIT_CURSOR_KEY})
_UNSYNCED = "unsynced"


async def _read_cursor(ctx: dict[str, object], key: str) -> UUID | None:
    """Read a fixed cursor from Redis, falling back to the startup-installed shared state.

    ARQ copies ``ctx`` per job, so progress lives in the shared ``w2_cursor_state`` object. A
    cursor whose last Redis write failed wins over the stale remote value until a write succeeds.
    """
    if key not in _CURSOR_KEYS:
        raise KeyError(key)
    state = cast(dict[str, object], ctx[CURSOR_STATE_KEY])
    unsynced = cast(set[str], state.setdefault(_UNSYNCED, set()))
    raw = state.get(key)
    if key not in unsynced:
        try:
            remote = await cast(Redis, ctx["redis"]).get(key)
            if remote is not None:
                raw = remote
        except Exception:  # noqa: BLE001 - best-effort cursor store
            _log.debug("cursor read failed", exc_info=True)
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    try:
        cursor = UUID(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        cursor = None
    state[key] = str(cursor) if cursor is not None else ""
    return cursor


async def _write_cursor(ctx: dict[str, object], key: str, cursor: UUID | None) -> None:
    """Persist a fixed cursor to the shared state and opportunistically to Redis."""
    if key not in _CURSOR_KEYS:
        raise KeyError(key)
    state = cast(dict[str, object], ctx[CURSOR_STATE_KEY])
    unsynced = cast(set[str], state.setdefault(_UNSYNCED, set()))
    state[key] = str(cursor) if cursor is not None else ""
    redis = cast(Redis, ctx["redis"])
    try:
        if cursor is None:
            await redis.delete(key)
        else:
            await redis.set(key, str(cursor))
        unsynced.discard(key)
    except Exception:  # noqa: BLE001 - best-effort cursor store
        _log.debug("cursor write failed", exc_info=True)
        unsynced.add(key)


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


async def process_news_document_ready(ctx: dict[str, object], event_id: str) -> None:
    """Cluster one immutable ready version and ACK its outbox event atomically.

    Admit the retained event principal and lock its original admission first on
    every branch (including malformed or stale receipts), then lock and recheck Source and
    Document evidence before locking the Ingestion outbox row. Exact provenance
    must still match after those domain locks; stale receipts are terminally ACKed
    without recreating a story. No external I/O occurs in this transaction.
    """
    try:
        identifier = UUID(event_id)
    except ValueError:
        return
    settings = cast(Settings, ctx["settings"])
    flag = settings.multi_workspace_enabled
    async with _factory(ctx)() as session:
        scope = await ingestion.resolve_ingestion_event_scope(
            session, identifier, multi_workspace_enabled=flag,
        )
        if scope is None:
            return
        try:
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
            provenance = await ingestion.resolve_ready_event_provenance(
                session, identifier, scope=scope, multi_workspace_enabled=flag,
            )
            source_fence = None
            document_locked = False
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
                else:
                    refreshed = None
            else:
                refreshed = None

            prepared_match = (
                provenance is not None and refreshed == provenance
                and provenance.workspace_id == scope.workspace_id
                and provenance.actor_user_id == scope.actor_user_id
                and provenance.membership_revision == scope.membership_revision
                and source_fence is not None and source_fence.status == "active"
                and source_fence.generation == provenance.source_generation
                and document_locked
            )
            if prepared_match:
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
                prepared_match
                and event.workspace_id == provenance.workspace_id
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
        except HTTPException as exc:
            if exc.status_code in {401, 403, 404, 409}:
                await session.rollback()
                return
            raise


async def recover_news_work(ctx: dict[str, object]) -> int:
    """Initialize missing News checkpoints and catch up one finite ready-document page.

    PostgreSQL checkpoint and observation writes commit together. The source
    facade exposes only detached active identities; each page then locks sorted
    sources before sorted documents and revalidates current generation/version
    through Documents before clustering. A separate bounded identity scan
    initializes legacy workspaces with no checkpoint; it advances independently
    from the existing per-workspace recovery cursor.
    """
    factory = _factory(ctx)
    settings = cast(Settings, ctx["settings"])
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
        flag = settings.multi_workspace_enabled
        for workspace_id in initialization_ids:
            async with factory() as session:
                try:
                    owner = await workspaces.resolve_workspace_owner_context(
                        session, workspace_id, multi_workspace_enabled=flag,
                    )
                    if owner is None:
                        continue
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
                        continue
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
                except HTTPException as exc:
                    if exc.status_code in {401, 403, 404, 409}:
                        await session.rollback()
                        continue
                    raise
        await _write_cursor(ctx, RECOVERY_INIT_CURSOR_KEY, initialization_ids[-1])
    else:
        await _write_cursor(ctx, RECOVERY_INIT_CURSOR_KEY, None)

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
        async with factory() as session:
            owner = await workspaces.resolve_workspace_owner_context(
                session, workspace_id, multi_workspace_enabled=flag,
            )
            if owner is None:
                continue
            scope = InternalJobScope(workspace_id=workspace_id, actor_user_id=owner.user_id,
                membership_revision=owner.membership_revision)
            try:
                access_fence = await workspaces.read_access_fence(session, scope=scope,
                    multi_workspace_enabled=flag)
                if not await settings_public.module_is_enabled(session, "news", scope=scope,
                        multi_workspace_enabled=flag):
                    continue
                checkpoint = await session.scalar(select(NewsRecoveryCheckpoint).where(
                    NewsRecoveryCheckpoint.workspace_id == workspace_id,
                ))
                if checkpoint is None:
                    continue
                initial_source_cursor = checkpoint.source_cursor
                initial_document_cursor = checkpoint.document_cursor
                source_page = await sources.list_active_gadget_sources(session, scope=scope,
                    multi_workspace_enabled=flag, limit=32, cursor=checkpoint.source_cursor)
                source_ids = tuple(item.id for item in source_page.items)
                projections, document_cursor = await documents.list_news_document_projections(
                    session, source_ids=source_ids, limit=10, cursor=checkpoint.document_cursor,
                    scope=scope, multi_workspace_enabled=flag,
                ) if source_ids else ([], None)
                projections.sort(key=lambda item: (str(item.source_id), str(item.document_id)))
                fences = {}
                for source_id in sorted(source_ids, key=str):
                    fences[source_id] = await sources.lock_source(session, source_id, scope=scope,
                        multi_workspace_enabled=flag, expected_access_fence=access_fence)
                workspace_processed = 0
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
                    workspace_processed += 1
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
                    continue
                if document_cursor is None:
                    checkpoint.source_cursor = source_page.next_cursor
                    checkpoint.document_cursor = None
                else:
                    checkpoint.document_cursor = document_cursor
                await commit_with_replay(session, [], scope=scope,
                    multi_workspace_enabled=flag, access_fence=access_fence)
                total_processed += workspace_processed
            except HTTPException as exc:
                if exc.status_code in {401, 403, 404, 409}:
                    await session.rollback()
                    continue
                raise
    await _write_cursor(ctx, RECOVERY_CURSOR_KEY, workspace_ids[-1])
    return total_processed
