"""Identity-only outbox discovery and admitted durable claims, with Redis outside SQL locks."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid5

from arq.connections import ArqRedis
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import InternalJobScope, Scope
from modules.ingestion import public
from modules.ingestion.models import EventOutbox, IngestionRun, IngestionStage
from modules.ingestion.schemas import EventDelivery
from modules.knowledge.documents import public as documents
from modules.settings.public import admit_write
from modules.sources import public as sources

DISPATCH_STALE_AFTER = timedelta(seconds=30)
DISPATCH_SCAN_STATE = "ingestion_dispatch_scan_state"
WORKER_BY_EVENT = {
    "document.file.uploaded": "process_uploaded_file",
    "document.cleanup.requested": "process_document_cleanup",
    "source.purge.requested": "process_source_purge",
    "source.purge.progressed": "process_source_purge",
    "source.purge.coverage": "process_source_memory_coverage",
    "ingestion.stage.requested": "process_ingestion_event",
    "connector.crawl.requested": "process_ingestion_event",
    "ingestion.normalize.requested": "process_normalize_event",
    "document.version.ready": "process_document_ready",
    "news.document.ready": "process_news_document_ready",
}
PRODUCERS_BY_EVENT = {
    "document.file.uploaded": {"modules.ingestion"},
    "document.cleanup.requested": {"modules.knowledge.documents"},
    "source.purge.requested": {"modules.sources"},
    "source.purge.progressed": {"modules.knowledge.documents"},
    "source.purge.coverage": {"modules.sources"},
    "ingestion.stage.requested": {"modules.ingestion"},
    "connector.crawl.requested": {"modules.connectors"},
    "ingestion.normalize.requested": {"modules.ingestion"},
    "document.version.ready": {"modules.ingestion", "modules.knowledge.documents"},
    "news.document.ready": {"modules.knowledge.documents"},
}


def valid_event_envelope(event: EventOutbox | EventDelivery, scope: InternalJobScope) -> bool:
    """Validate exact retained principal/type/version/producer before worker content use.

    Ready/purge shapes are finite owner contracts. Other supported events retain their
    owner payload but must carry canonical Source/principal and stage/run IDs where used.
    No coercion, metadata promotion, default actor or current membership upgrade occurs.
    """
    payload = event.payload
    if (event.type not in PRODUCERS_BY_EVENT or type(event.version) is not int or event.version != 1
            or event.producer not in PRODUCERS_BY_EVENT[event.type] or not isinstance(payload, dict)):
        return False
    if event.type == "document.cleanup.requested":
        # Operation-only envelope: principal is proven on the outbox columns (and by the receipt
        # owner), never by payload keys; legacy identity-in-payload shapes are rejected.
        if (event.workspace_id != scope.workspace_id or event.actor_user_id != scope.actor_user_id
                or event.membership_revision != scope.membership_revision or scope.source_id is None
                or set(payload) != {"operation_id"} or not isinstance(payload["operation_id"], str)):
            return False
        try:
            operation_id = UUID(payload["operation_id"])
        except ValueError:
            return False
        return (str(operation_id) == payload["operation_id"]
                and event.id == uuid5(operation_id, "document-cleanup-requested"))
    principal = {"workspace_id": str(scope.workspace_id), "actor_user_id": scope.actor_user_id,
                 "membership_revision": scope.membership_revision,
                 "source_id": str(scope.source_id), "source_generation": scope.source_generation}
    if (event.workspace_id != scope.workspace_id or event.actor_user_id != scope.actor_user_id
            or event.membership_revision != scope.membership_revision or scope.source_id is None):
        return False
    if any(type(payload.get(key)) is not type(value) or payload.get(key) != value
           for key, value in principal.items()):
        return False
    uuid_fields = []
    if event.type.startswith("source.purge."):
        if set(payload) != set(principal) | {"operation_id"}:
            return False
        uuid_fields = ["operation_id"]
    elif event.type in {"document.version.ready", "news.document.ready"}:
        if set(payload) != set(principal) | {"document_id", "document_version_id", "version_number"}:
            return False
        if (type(payload.get("version_number")) is not int or not 1 <= payload["version_number"] <= 2_147_483_647
                or not 1 <= payload["source_generation"] <= 2_147_483_647):
            return False
        if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 1024:
            return False
        uuid_fields = ["document_id", "document_version_id"]
    else:
        uuid_fields = ["run_id", "stage_id"]
        if event.type == "document.file.uploaded":
            uuid_fields.append("document_id")
        if event.type == "ingestion.normalize.requested" and (
            type(payload.get("normalization_version")) is not int or payload["normalization_version"] != 1
        ):
            return False
        if event.type == "connector.crawl.requested" and (
            type(payload.get("connector_revision")) is not int or payload["connector_revision"] < 1
        ):
            return False
    try:
        return all(isinstance(payload.get(key), str) and str(UUID(payload[key])) == payload[key]
                   for key in uuid_fields)
    except (ValueError, TypeError, AttributeError):
        return False


async def _quarantine_event_identity(
    session: AsyncSession, event_id: UUID, status: str, dispatched_at: datetime | None,
) -> None:
    """Fail one unresolved identity using original status/time CAS without exposing its body.

    This internal dispatcher repair grants no read/domain authority and cannot overwrite
    an intervening claim. A malformed retained principal never becomes owner1.
    """
    await admit_write(session, "ingestion_dispatch_quarantine", str(event_id))
    await session.execute(update(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.status == status,
        EventOutbox.dispatched_at == dispatched_at,
    ).values(status="failed"))
    await session.commit()


def _dispatch_scan_cursor(value: object) -> tuple[datetime, UUID, datetime] | None:
    """Decode a scheduling-only full-key checkpoint; malformed values restart a sweep.

    The fixed creation-time ceiling makes each sweep finite despite newer arrivals.
    This checkpoint carries no payload, principal, claim or authorization.
    """
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    if any(not isinstance(item, str) or len(item) > 64 for item in value):
        return None
    try:
        stamp, identifier, ceiling = datetime.fromisoformat(value[0]), UUID(value[1]), datetime.fromisoformat(value[2])
        if (stamp.tzinfo is None or stamp.utcoffset() is None or ceiling.tzinfo is None
                or ceiling.utcoffset() is None or str(identifier) != value[1] or stamp > ceiling
                or ceiling > datetime.now(UTC)):
            return None
        return stamp.astimezone(UTC), identifier, ceiling.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


async def dispatch_pending_work(ctx: dict[str, object]) -> int:
    """Scan at most100 identities, admit each original subject and commit before Redis.

    The committed queued timestamp is the dispatch claim. Recovery and failure compare
    that exact timestamp under current original admission, so successor attempts survive.
    No SQL locks span enqueue; absent Redis acknowledgements remain recoverable durable rows.
    Malformed envelopes quarantine; lost access never acquires another actor or epoch.
    A shared nested ctx full-key checkpoint advances even on denied/skipped identities.
    ARQ copies invocation ctx, so startup owns this shared scheduling dict and lock.
    The lock only reserves one bounded identity page and advances its checkpoint before
    per-job admission; overlapping cron calls cannot regress or race a sweep wrap.
    Each finite creation-time sweep wraps once after exhaustion, revisiting its prefix;
    worker restart resets scheduling only, with no durability or admission claim.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(ArqRedis, ctx["redis"])
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    scan_state = ctx.get(DISPATCH_SCAN_STATE)
    if (not isinstance(scan_state, dict) or set(scan_state) != {"cursor", "lock"}
            or not isinstance(scan_state["lock"], asyncio.Lock)):
        raise TypeError("Worker startup must supply shared ingestion dispatch scheduling state")
    async with scan_state["lock"]:
        now = datetime.now(UTC)
        cursor = _dispatch_scan_cursor(scan_state["cursor"])
        if cursor is None:
            scan_state["cursor"] = None
        ceiling = cursor[2] if cursor is not None else now
        async with factory() as session:
            statement = select(
                EventOutbox.id, EventOutbox.status, EventOutbox.dispatched_at, EventOutbox.created_at,
            ).where(EventOutbox.type.in_(WORKER_BY_EVENT), or_(
                and_(EventOutbox.status == "pending", EventOutbox.next_attempt_at <= now),
                and_(EventOutbox.status == "queued", or_(EventOutbox.dispatched_at.is_(None),
                    EventOutbox.dispatched_at < now - DISPATCH_STALE_AFTER)),
            ))
            page = statement.where(EventOutbox.created_at <= ceiling)
            if cursor is not None:
                page = page.where(or_(EventOutbox.created_at > cursor[0], and_(
                    EventOutbox.created_at == cursor[0], EventOutbox.id > cursor[1],
                )))
            identities = (await session.execute(page.order_by(EventOutbox.created_at, EventOutbox.id).limit(100))).all()
            if not identities and cursor is not None:
                # Only an empty page wraps; at most100 candidate identities across both reads.
                ceiling = now
                scan_state["cursor"] = None
                identities = (await session.execute(statement.where(EventOutbox.created_at <= ceiling)
                    .order_by(EventOutbox.created_at, EventOutbox.id).limit(100))).all()
        if identities:
            last_identity = identities[-1]
            scan_state["cursor"] = (
                last_identity.created_at.astimezone(UTC).isoformat(), str(last_identity.id), ceiling.isoformat(),
            )
    # Scheduling lock is released before any original-subject admission, mutation or Redis.
    enqueued = 0
    for identifier, original_status, original_dispatch, created_at in identities:
        claimed_at = None
        try:
            async with factory() as session:
                await admit_write(session, "ingestion_dispatch", str(identifier))
                scope = await public.resolve_ingestion_event_scope(session, identifier, multi_workspace_enabled=enabled)
                if scope is None:
                    await session.rollback()
                    await _quarantine_event_identity(session, identifier, original_status, original_dispatch)
                    continue
                access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=enabled)
                event = await session.scalar(select(EventOutbox).where(
                    EventOutbox.id == identifier, EventOutbox.workspace_id == scope.workspace_id,
                    EventOutbox.actor_user_id == scope.actor_user_id,
                    EventOutbox.membership_revision == scope.membership_revision,
                    EventOutbox.status == original_status, EventOutbox.dispatched_at == original_dispatch,
                ).with_for_update(skip_locked=True).execution_options(populate_existing=True))
                if event is None:
                    continue
                if not valid_event_envelope(event, scope):
                    event.status = "failed"
                    await session.commit()
                    continue
                if "run_id" in event.payload and "stage_id" in event.payload:
                    stage = (await session.execute(select(IngestionStage.status, IngestionStage.lease_expires_at)
                        .join(IngestionRun, IngestionRun.id == IngestionStage.run_id).where(
                            IngestionStage.id == UUID(event.payload["stage_id"]),
                            IngestionRun.id == UUID(event.payload["run_id"]),
                            IngestionRun.workspace_id == scope.workspace_id,
                            IngestionRun.actor_user_id == scope.actor_user_id,
                            IngestionRun.membership_revision == scope.membership_revision,
                            IngestionRun.source_id == scope.source_id,
                        ))).one_or_none()
                    if stage is None:
                        event.status = "failed"
                        await session.commit()
                        continue
                    # Preserve an in-flight worker's original timestamp until its stage lease expires.
                    if stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now:
                        continue
                if event.type == "document.cleanup.requested":
                    receipt = await documents.read_document_cleanup_job_identity(
                        session, UUID(event.payload["operation_id"]), scope=scope, multi_workspace_enabled=enabled,
                    )
                    if receipt is None or (receipt.source_id, receipt.source_generation) != (
                        scope.source_id, scope.source_generation,
                    ):
                        event.status = "failed"
                        await session.commit()
                        continue
                if event.type.startswith("source.purge."):
                    retained = await sources.read_source_purge_job_identity(
                        session, UUID(event.payload["operation_id"]), scope=scope, multi_workspace_enabled=enabled,
                    )
                    if retained != scope:
                        event.status = "failed"
                        await session.commit()
                        continue
                claimed_at = datetime.now(UTC)
                event.status = "queued"
                event.dispatched_at = claimed_at
                job_name = WORKER_BY_EVENT[event.type]
                await session.commit()
            # Redis carries only identity; each consumer re-resolves the durable envelope.
            job = await redis.enqueue_job(job_name, str(identifier),
                _job_id=f"ingestion:{identifier}:{claimed_at.isoformat()}", _defer_until=claimed_at)
            if job is not None:
                enqueued += 1
        except HTTPException:
            # Lost original authorization cannot expose payload or repair a successor claim.
            continue
        except Exception:
            # The queued row remains durable after a lost enqueue reply; the stale scan recovers it.
            # Only a successfully admitted exact original attempt can return to pending now.
            if claimed_at is None:
                raise
            try:
                async with factory() as session:
                    await admit_write(session, "ingestion_dispatch_recovery", str(identifier))
                    current_scope = await public.resolve_ingestion_event_scope(session, identifier,
                                                                             multi_workspace_enabled=enabled)
                    if current_scope != scope:
                        continue
                    from core.workspaces.public import lock_access_fence
                    await lock_access_fence(session, scope=scope, expected=access_fence,
                                            multi_workspace_enabled=enabled)
                    await session.execute(update(EventOutbox).where(
                        EventOutbox.id == identifier, EventOutbox.workspace_id == scope.workspace_id,
                        EventOutbox.actor_user_id == scope.actor_user_id,
                        EventOutbox.membership_revision == scope.membership_revision,
                        EventOutbox.status == "queued", EventOutbox.dispatched_at == claimed_at,
                    ).values(status="pending", next_attempt_at=datetime.now(UTC) + DISPATCH_STALE_AFTER))
                    await session.commit()
            except HTTPException:
                continue
    return enqueued


async def mark_event_delivered(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Forward the owner flush-only acknowledgement; caller holds ordered locks and commits.

    Entities' old dispatcher import remains an explicit required-scope caller migration.
    This compatibility module path introduces no commit or earlier-lock acquisition.
    """
    return await public.mark_event_delivered(session, event_id, scope=scope,
                                             multi_workspace_enabled=multi_workspace_enabled)
