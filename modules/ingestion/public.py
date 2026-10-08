"""Workspace-bound ingestion contracts; caller-held helpers never reacquire admission locks.

Standalone collection wrappers own their existing commits. Retained jobs derive their
principal from durable owner rows, and every publication carries the original epoch.
"""

import base64
import hashlib
import json
import secrets
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from typing import cast as typing_cast
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, delete, func, not_, or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.events import DomainEvent
from core.pagination import decode_cursor, encode_cursor
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
    make_knowledge_change,
    make_source_change,
)
from core.telemetry import RunMeta as _RunMeta
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.ingestion.models import (
    COLLECTION_LEASE,
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.schemas import (
    ConnectorCollectionLease,
    CrawlReceipt,
    EventDelivery,
    IngestionRecord,
    NativeCollectionBatch,
    NativeCollectionReceipt,
    Receipt,
    ReceiveBatch,
    RunRead,
    SourceIngestionRead,
    StageRead,
    TelegramCursor,
    TelegramDeliveryProof,
    TelegramProbeClassification,
    TelegramRawDelivery,
    classify_telegram_probe,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.schemas import DocumentCleanupPreparationLimitError
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

_RUN_RETRY_ORDER = {"receive": 0, "collect_web": 1, "normalize": 2, "parse_file": 3}
_SOURCE_PURGE_PRODUCERS = {
    "source.purge.requested": "modules.sources",
    "source.purge.coverage": "modules.sources",
    "source.purge.progressed": "modules.knowledge.documents",
}
_READY_EVENT_PRODUCERS = {
    "document.version.ready": {"modules.ingestion", "modules.knowledge.documents"},
    "news.document.ready": {"modules.knowledge.documents"},
}


def _actor_id(scope: Scope) -> int:
    """Extract the validated principal identity without granting resource authority."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit_ingestion_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> AccessFence:
    """Read current owner admission without locks; safe under caller-held earlier locks.

    Members cannot inspect ingestion metadata. Publication/mutation callers must already
    hold ordered admission/Source locks or use a locking entrypoint before domain locks.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise HTTPException(status_code=401, detail="Authentication required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _run_scope(scope: Scope) -> tuple[Any, ...]:
    """Constrain retained runs to the exact workspace/actor/epoch and optional Source pair."""
    predicates = [IngestionRun.workspace_id == scope.workspace_id,
                  IngestionRun.actor_user_id == _actor_id(scope),
                  IngestionRun.membership_revision == scope.membership_revision]
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates.extend((IngestionRun.source_id == scope.source_id,
                           select(IngestionBatch.id).where(
                               IngestionBatch.id == IngestionRun.batch_id,
                               IngestionBatch.source_id == scope.source_id,
                               IngestionBatch.source_generation == scope.source_generation,
                           ).correlate(IngestionRun).exists()))
    return tuple(predicates)


def _event_scope(scope: Scope) -> tuple[Any, ...]:
    """Filter outbox roots before count/LIMIT; a source-bound job cannot read sibling events."""
    predicates = [EventOutbox.workspace_id == scope.workspace_id,
                  EventOutbox.actor_user_id == _actor_id(scope),
                  EventOutbox.membership_revision == scope.membership_revision]
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates.extend((EventOutbox.payload["source_id"].astext == str(scope.source_id),
                           EventOutbox.payload["source_generation"].astext == str(scope.source_generation)))
    return tuple(predicates)


def _materialization_scope(scope: Scope) -> tuple[Any, ...]:
    """Join materializations to their exact retained run and optional Source generation."""
    return (ObservationNormalization.workspace_id == scope.workspace_id,
            select(IngestionRun.id).where(
                IngestionRun.id == ObservationNormalization.run_id,
                IngestionRun.source_id == ObservationNormalization.source_id,
                *_run_scope(scope),
            ).exists(),
            select(IngestionStage.id).where(
                IngestionStage.id == ObservationNormalization.stage_id,
                IngestionStage.run_id == ObservationNormalization.run_id,
            ).exists(),
            select(IngestionBatch.id).join(IngestionRun, IngestionRun.batch_id == IngestionBatch.id).where(
                IngestionRun.id == ObservationNormalization.run_id,
                IngestionBatch.source_id == ObservationNormalization.source_id,
                IngestionBatch.source_generation == ObservationNormalization.source_generation,
            ).exists())


async def _source_in_scope(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorSource | None:
    """Read a current Source-owner DTO without introducing any earlier locks."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _lease_scope(lease: ConnectorCollectionLease, scope: Scope) -> None:
    """Reject detached reservation identities that differ from the admitted original epoch."""
    if (lease.workspace_id != scope.workspace_id or lease.actor_user_id != _actor_id(scope)
            or lease.membership_revision != scope.membership_revision
            or isinstance(scope, InternalJobScope) and scope.source_id is not None
            and (lease.source_id != scope.source_id or lease.source_generation != scope.source_generation)):
        raise HTTPException(status_code=404, detail="Collection reservation not found")


def _retained_scope(
    workspace_id: UUID, actor_user_id: int, membership_revision: int,
    source_id: UUID | None = None, source_generation: int | None = None,
) -> InternalJobScope:
    """Build a strict retained owner subject; missing/corrupt durable identity never rebases."""
    return InternalJobScope(workspace_id=workspace_id, actor_user_id=actor_user_id,
                            membership_revision=membership_revision, source_id=source_id,
                            source_generation=source_generation)


def _lease_access_fence(lease: ConnectorCollectionLease) -> AccessFence:
    """Reconstruct the original owner-issued lease fence without current-state rebasing.

    Accept only the detached owner DTO. This snapshot is still subject to ordered current
    admission and exact scope/payload/reservation comparison; it creates no authority.
    """
    if not isinstance(lease, ConnectorCollectionLease):
        raise HTTPException(status_code=409, detail="Original collection lease required")
    return AccessFence(lease.workspace_id, lease.actor_user_id,
                       lease.membership_revision, lease.configuration_revision)


def _source_purge_event_subject(
    event_type: str, version: int, producer: str, payload: object, *, scope: Scope,
) -> tuple[UUID, InternalJobScope] | None:
    """Parse only the three finite purge envelopes and compare exact original principal.

    All version1 payloads have canonical six-field UUID/principal/Source identity. Invalid
    types, fields or caller restrictions return None, never malformed content. The caller
    still compares this detached subject with the Source owner's retained operation.
    """
    fields = {"operation_id", "workspace_id", "actor_user_id", "membership_revision",
              "source_id", "source_generation"}
    if (event_type not in _SOURCE_PURGE_PRODUCERS or type(version) is not int or version != 1
            or producer != _SOURCE_PURGE_PRODUCERS[event_type]
            or not isinstance(payload, dict) or set(payload) != fields):
        return None
    try:
        if any(not isinstance(payload[key], str) or str(UUID(payload[key])) != payload[key]
               for key in ("operation_id", "workspace_id", "source_id")):
            return None
        retained = _retained_scope(UUID(payload["workspace_id"]), payload["actor_user_id"],
                                   payload["membership_revision"], UUID(payload["source_id"]),
                                   payload["source_generation"])
    except (TypeError, ValueError):
        return None
    if (retained.workspace_id != scope.workspace_id or retained.actor_user_id != _actor_id(scope)
            or retained.membership_revision != scope.membership_revision
            or isinstance(scope, InternalJobScope) and scope.source_id is not None
            and (retained.source_id != scope.source_id
                 or retained.source_generation != scope.source_generation)):
        return None
    return UUID(payload["operation_id"]), retained


def _document_cleanup_operation_id(event_id: UUID, version: int, producer: str, payload: object) -> UUID:
    """Parse the exact operation-only Documents cleanup envelope; any deviation is a ValueError."""
    message = "Document cleanup event requires exact operation-only envelope"
    if (type(version) is not int or version != 1 or producer != "modules.knowledge.documents"
            or not isinstance(payload, dict) or set(payload) != {"operation_id"}):
        raise ValueError(message)
    raw = payload["operation_id"]
    try:
        operation_id = UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        operation_id = None
    if (operation_id is None or str(operation_id) != raw
            or event_id != uuid5(operation_id, "document-cleanup-requested")):
        raise ValueError(message)
    return operation_id


async def resolve_collector_job_scope(
    session: AsyncSession, token: str, *, source_id: UUID, credential_scope: str = "ingestion:write",
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve a hashed capability/path Source to real owner identity before ordered admission.

    The Source owner resolves its durable workspace/generation and active default owner.
    Recheck this credential after its ordered admission; intake must lock/recheck again.
    This does not authorize a managed request UUID, which C2 binds separately.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    statement = select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash, CollectorCredential.source_id == source_id,
        CollectorCredential.scope == credential_scope, CollectorCredential.revoked_at.is_(None),
    )
    if await session.scalar(statement) is None:
        return None
    scope = await sources.resolve_source_job_scope(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope is None or await session.scalar(statement.execution_options(populate_existing=True)) is None:
        return None
    return scope


async def resolve_ingestion_event_scope(
    session: AsyncSession, event_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve a retained outbox principal before domain locks, then compare its exact reread.

    Corrupt/missing scope is unresolved, never an owner/default upgrade. The caller owns
    quarantine and transaction release; Source/claim/lease checks remain owner-specific.
    """
    # Identity discovery never loads the event body/config/raw URI before admission.
    # Bound textual scalar fields so corrupt JSON cannot turn discovery into a content read.
    row = (await session.execute(select(
        EventOutbox.workspace_id, EventOutbox.actor_user_id, EventOutbox.membership_revision,
        func.left(EventOutbox.payload["workspace_id"].astext, 37).label("payload_workspace"),
        func.left(EventOutbox.payload["actor_user_id"].astext, 21).label("payload_actor"),
        func.left(EventOutbox.payload["membership_revision"].astext, 21).label("payload_membership"),
        func.left(EventOutbox.payload["source_id"].astext, 37).label("payload_source"),
        func.left(EventOutbox.payload["source_generation"].astext, 21).label("payload_generation"),
        func.jsonb_typeof(EventOutbox.payload["actor_user_id"]).label("actor_type"),
        func.jsonb_typeof(EventOutbox.payload["membership_revision"]).label("membership_type"),
        func.jsonb_typeof(EventOutbox.payload["source_id"]).label("source_type"),
        func.jsonb_typeof(EventOutbox.payload["source_generation"]).label("generation_type"),
    ).where(EventOutbox.id == event_id))).one_or_none()
    if row is None:
        return None
    try:
        if (row.payload_workspace != str(row.workspace_id)
                or row.actor_type != "number" or row.payload_actor != str(row.actor_user_id)
                or row.membership_type != "number"
                or row.payload_membership != str(row.membership_revision)):
            return None
        if row.source_type is None and row.generation_type is None:
            source_id, generation = None, None
        elif row.source_type == "string" and row.generation_type == "number":
            source_id, generation = UUID(row.payload_source), int(row.payload_generation)
        else:
            return None
        scope = _retained_scope(row.workspace_id, row.actor_user_id, row.membership_revision,
                                source_id, generation)
    except (KeyError, TypeError, ValueError):
        return None
    await workspaces.authorize_internal_job(session, scope=scope,
                                           multi_workspace_enabled=multi_workspace_enabled)
    current = await session.scalar(select(EventOutbox.id).where(EventOutbox.id == event_id,
                                                            *_event_scope(scope))
                                   .execution_options(populate_existing=True))
    return scope if current is not None else None


async def resolve_ingestion_run_scope(
    session: AsyncSession, run_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Discover the retained run/batch principal, admit it, and compare exact lineage again."""
    row = (await session.execute(select(IngestionRun.workspace_id, IngestionRun.actor_user_id,
                                       IngestionRun.membership_revision, IngestionRun.source_id,
                                       IngestionBatch.source_generation)
                                 .join(IngestionBatch, and_(IngestionBatch.id == IngestionRun.batch_id,
                                                            IngestionBatch.source_id == IngestionRun.source_id))
                                 .where(IngestionRun.id == run_id))).one_or_none()
    if row is None:
        return None
    try:
        scope = _retained_scope(*row)
    except (TypeError, ValueError):
        return None
    await workspaces.authorize_internal_job(session, scope=scope,
                                           multi_workspace_enabled=multi_workspace_enabled)
    current = await session.scalar(select(IngestionRun.id).where(IngestionRun.id == run_id,
                                                                *_run_scope(scope)))
    return scope if current is not None else None


@dataclass(frozen=True)
class NewsDocumentReadyEvent:
    """Detached fixed-shape News readiness event owned by Ingestion."""
    id: UUID
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    version: int
    status: str
    payload: dict[str, Any]
    valid_payload: bool


@dataclass(frozen=True)
class ReadyDocumentProvenance:
    """Detached proof joining one retained ready event to its exact Document version."""

    event_id: UUID
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    source_id: UUID
    document_id: UUID
    document_version_id: UUID
    source_generation: int
    version_number: int


async def lock_news_document_ready_event(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> NewsDocumentReadyEvent | None:
    """Lock one News readiness outbox row and return only its bounded event payload.

    The event table remains private to Ingestion. Invalid or oversized payloads
    return a detached DTO with valid_payload false so the consumer can terminally
    fail the receipt without parsing arbitrary or unbounded JSON fields.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready", *_event_scope(scope),
    ).with_for_update())
    if event is None:
        return None
    payload = event.payload
    validated = _ready_document_payload(event)
    valid = validated is not None
    detached = validated if validated is not None else {}
    return NewsDocumentReadyEvent(
        workspace_id=event.workspace_id, actor_user_id=event.actor_user_id, membership_revision=event.membership_revision,
        id=event.id, version=event.version, status=event.status,
        payload=detached, valid_payload=bool(valid),
    )


async def mark_news_document_ready_event_delivered(session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Flush News event acknowledgement without committing the caller's transaction."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready", *_event_scope(scope),
    ).with_for_update())
    if event is None:
        return False
    event.status = "delivered"
    await session.flush()
    return True


async def fail_news_document_ready_event(session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Flush a terminal invalid News receipt state while leaving commit to the caller."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready", *_event_scope(scope),
    ).with_for_update())
    if event is None:
        return False
    event.status = "failed"
    await session.flush()
    return True

def _digest(value: object) -> str:
    """Hash canonical JSON so equivalent payload mappings share an identity."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def create_collector_credential(
    session: AsyncSession, source_id: UUID, *,
    credential_scope: Literal["ingestion:write", "mcp:collect"] = "ingestion:write",
    scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Acquire real admission/Source then rotate one literal collector capability, flush only.

    Enter without domain locks. Missing/archived Sources retain LookupError compatibility;
    active/paused nonlocal issuance is eligible. Local-only/stale issuance fails409 and an
    invalid capability raises ValueError.
    Genuine locked access/Source fences are passed to the held writer, never fabricated.
    Return the bearer once; only its hash persists. Caller owns commit/rollback, no I/O.
    """
    if credential_scope not in ("ingestion:write", "mcp:collect"):
        raise ValueError("Unsupported collector credential capability")
    source = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.status == "archived":
        raise LookupError("Source not found")
    access_fence = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return await create_collector_credential_in_uow(
        session, source_id, credential_scope=credential_scope, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source,
    )


async def create_collector_credential_in_uow(
    session: AsyncSession, source_id: UUID, *,
    credential_scope: Literal["ingestion:write", "mcp:collect"] = "ingestion:write",
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> str:
    """Rotate an exact active/paused nonlocal Source capability under held earlier parents.

    Caller retains original admission/Source and applicable provisioning/slots/native/world;
    enter before Tools/GitHub/state. Complete access/Source proof is freshly compared without
    parent locks. Missing Source fails404, stale/archived/local-only fails409, invalid literal
    raises ValueError. Lock only active exact-capability rows by token hash, revoke with one
    timestamp, insert one new hash/literal and return plaintext once after flush. Sibling
    capabilities survive. No readiness, activation, commit/replay or network authority is
    granted; failures after mutation require the caller to roll back the whole transaction.
    """
    if credential_scope not in ("ingestion:write", "mcp:collect"):
        raise ValueError("Unsupported collector credential capability")
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if (current_access != access_fence or current_source != source_fence
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or current_source.status not in {"active", "paused"} or current_source.local_only):
        raise HTTPException(status_code=409, detail="Collector credential issuance fence is stale")
    now = datetime.now(UTC)
    credentials = list(
        (
            await session.scalars(
                select(CollectorCredential)
                .where(
                    CollectorCredential.source_id == source_id,
                    CollectorCredential.scope == credential_scope,
                    CollectorCredential.revoked_at.is_(None),
                )
                .order_by(CollectorCredential.token_hash).with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    for credential in credentials:
        credential.revoked_at = now
    token = secrets.token_urlsafe(32)
    session.add(CollectorCredential(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), source_id=source_id, scope=credential_scope,
    ))
    await session.flush()
    return token


async def revoke_collector_credential(session: AsyncSession, token: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Discover a token Source, acquire ordered admission/Source, then revoke its exact row.

    Entry holds no prior domain locks. A foreign Source fails scoped admission; no raw
    token persists and this helper leaves commit to its caller.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    source_id = await session.scalar(
        select(CollectorCredential.source_id).where(CollectorCredential.token_hash == token_hash)
    )
    if source_id is None:
        return
    await sources.lock_source_set(
        session, (source_id,), scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(
        select(CollectorCredential)
        .where(CollectorCredential.token_hash == token_hash, CollectorCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is not None:
        row.revoked_at = datetime.now(UTC)


async def lock_source_credentials_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare exact Source token rows, sorted by hash, without revoking or committing.

    Source lifecycle preparation holds admission/Source and optional provisioning plus
    Connector cleanup rows before this call. Fresh nonlocking owner proof for active,
    paused or archived G must match both complete captured fences; stale/unavailable
    proof raises409. Archived eligibility is destructive cleanup only, not issuance or
    collection authority. Visibility active G->paused G+1 and identical paused G preparation
    remain supported without changing Source or tokens.
    Only credential rows are locked, before Tools and GitHub state/hints/capacity; no earlier
    parent lock or authority token is acquired.
    """
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (current_access != access_fence or current_source is None
            or current_source != source_fence or current_source.status not in {"active", "paused", "archived"}):
        raise HTTPException(status_code=409, detail="Source credential preparation is stale")
    (await session.scalars(select(CollectorCredential).where(
        CollectorCredential.source_id == source_id,
    ).order_by(CollectorCredential.token_hash).with_for_update()
                          .execution_options(populate_existing=True))).all()


async def revoke_source_credentials(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Revoke tokens under caller-held admission/Source locks; no earlier lock acquisition.

    Source lifecycle callers own ordered locks and commit. Nonlocking owner DTO proof
    checks exact scope/generation; this helper never recursively locks Source or auth.
    """
    if await _source_in_scope(session, source_id, scope=scope,
                              multi_workspace_enabled=multi_workspace_enabled) is None:
        return
    await session.execute(
        update(CollectorCredential)
        .where(CollectorCredential.source_id == source_id, CollectorCredential.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def publish_event(session: AsyncSession, event: DomainEvent, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Add scoped durable identity to the caller's already-admitted transaction outbox.

    Caller holds admission and all required domain locks; only nonlocking owner reads
    occur here. Exact retained purge identity survives canonical deletion. Existing
    claimed scope fields must match, never be silently overwritten. No commit/I/O.
    Purge event types have exact Source/Documents producer contracts; normalized Ingestion
    version-ready publication additionally proves its scoped journal/run/batch lineage.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    payload = deepcopy(event.payload)
    identity = {"workspace_id": str(scope.workspace_id), "actor_user_id": _actor_id(scope),
                "membership_revision": scope.membership_revision}
    for key, value in identity.items():
        if key in payload and (type(payload[key]) is not type(value) or payload[key] != value):
            raise HTTPException(status_code=404, detail="Event identity not found")
    if event.type == "document.cleanup.requested":
        operation_id = _document_cleanup_operation_id(event.id, event.version, event.producer, payload)
        receipt = await documents.read_document_cleanup_job_identity(
            session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        # The operation-only payload is preserved: principal columns come from ``scope`` below.
        if (
            receipt is None or receipt.workspace_id != scope.workspace_id
            or receipt.actor_user_id != _actor_id(scope)
            or receipt.membership_revision != scope.membership_revision
            or isinstance(scope, InternalJobScope) and scope.source_id is not None
            and (receipt.source_id != scope.source_id or receipt.source_generation != scope.source_generation)
        ):
            raise HTTPException(status_code=404, detail="Document cleanup receipt not found")
    elif event.type in _SOURCE_PURGE_PRODUCERS:
        subject = _source_purge_event_subject(
            event.type, event.version, event.producer, payload, scope=scope,
        )
        if subject is None:
            raise ValueError("Source purge event requires exact retained producer identity")
        operation_id, retained = subject
        receipt_scope = await sources.read_source_purge_job_identity(
            session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if receipt_scope != retained:
            raise HTTPException(status_code=404, detail="Source purge receipt not found")
    else:
        if "run_id" in payload:
            try:
                run_id = UUID(payload["run_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Event run identity is invalid") from exc
            run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id,
                                                                  *_run_scope(scope)))
            if run is None:
                raise HTTPException(status_code=404, detail="Event run not found")
            batch = await session.scalar(select(IngestionBatch).where(
                IngestionBatch.id == run.batch_id, IngestionBatch.source_id == run.source_id,
            ))
            if batch is None or batch.source_generation != payload.get("source_generation"):
                raise HTTPException(status_code=404, detail="Event batch lineage not found")
            if "source_id" in payload and payload["source_id"] != str(run.source_id):
                raise HTTPException(status_code=404, detail="Event source not found")
            payload["source_id"] = str(run.source_id)
            if "stage_id" in payload:
                try:
                    stage_id = UUID(payload["stage_id"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("Event stage identity is invalid") from exc
                if await session.scalar(select(IngestionStage.id).where(
                    IngestionStage.id == stage_id, IngestionStage.run_id == run.id,
                )) is None:
                    raise HTTPException(status_code=404, detail="Event stage not found")
        if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
            payload.get("source_id") != str(scope.source_id)
            or payload.get("source_generation") != scope.source_generation
        ):
            raise HTTPException(status_code=404, detail="Event Source identity not found")
        payload.update(identity)
    outbox = EventOutbox(
        workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope),
        membership_revision=scope.membership_revision,
        id=event.id,
        type=event.type,
        version=event.version,
        occurred_at=event.occurred_at,
        producer=event.producer,
        payload=payload,
        status="pending",
    )
    if event.type in {"document.version.ready", "news.document.ready"}:
        if _ready_document_payload(outbox) is None:
            raise ValueError("Ready event requires exact scoped producer payload")
        locator = await documents.review_version_locator(
            session, UUID(payload["document_version_id"]), scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if locator != (UUID(payload["document_id"]), UUID(payload["source_id"])):
            raise HTTPException(status_code=404, detail="Ready document lineage not found")
        if event.producer == "modules.ingestion":
            # Normalization owns this producer. Its already-written journal must link
            # the exact created version through the admitted run/stage/batch/observation.
            normalized = await session.scalar(select(ObservationNormalization.id).where(
                *_materialization_scope(scope),
                ObservationNormalization.source_id == UUID(payload["source_id"]),
                ObservationNormalization.source_generation == payload["source_generation"],
                ObservationNormalization.document_id == UUID(payload["document_id"]),
                ObservationNormalization.document_version_id == UUID(payload["document_version_id"]),
                ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                ObservationNormalization.disposition == "normalized",
                ObservationNormalization.chunk_count > 0,
                select(IngestionStage.id).where(
                    IngestionStage.id == ObservationNormalization.stage_id,
                    IngestionStage.run_id == ObservationNormalization.run_id,
                    IngestionStage.stage_key == "normalize",
                ).correlate(ObservationNormalization).exists(),
                select(SourceObservation.id).join(IngestionRun, and_(
                    IngestionRun.batch_id == SourceObservation.batch_id,
                    IngestionRun.source_id == SourceObservation.source_id,
                )).where(
                    SourceObservation.id == ObservationNormalization.observation_id,
                    SourceObservation.source_id == ObservationNormalization.source_id,
                    IngestionRun.id == ObservationNormalization.run_id,
                ).correlate(ObservationNormalization).exists(),
            ).limit(1))
            if normalized is None:
                raise HTTPException(status_code=404, detail="Ready normalization lineage not found")
    session.add(outbox)


NORMALIZATION_VERSION = 1


async def schedule_normalization(
    session: AsyncSession, run: IngestionRun, batch: IngestionBatch, source_generation: int,
    received_at: datetime, *, scope: Scope, multi_workspace_enabled: bool,
) -> IngestionStage | None:
    """Create or reuse normalization work and idempotent observation progress.

    Compare supplied run/batch/source/generation against scoped owner rows before any
    observation query. Empty batches produce no stage; a new stage emits one durable
    request event. Caller holds earlier admission/Source/run locks and owns commit.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    authorized_run = await session.scalar(select(IngestionRun).where(
        IngestionRun.id == run.id, IngestionRun.batch_id == batch.id,
        IngestionRun.source_id == batch.source_id, *_run_scope(scope),
    ))
    authorized_batch = await session.scalar(select(IngestionBatch.id).where(
        IngestionBatch.id == batch.id, IngestionBatch.source_id == run.source_id,
        IngestionBatch.source_generation == source_generation,
    ))
    if authorized_run is None or authorized_batch is None:
        raise HTTPException(status_code=404, detail="Normalization lineage not found")
    observations = list((await session.scalars(
        select(SourceObservation).where(SourceObservation.batch_id == batch.id,
                                        SourceObservation.source_id == run.source_id)
        .order_by(SourceObservation.id)
    )).all())
    if not observations:
        return None
    stage = await session.scalar(
        select(IngestionStage).where(IngestionStage.run_id == run.id, IngestionStage.stage_key == "normalize")
    )
    if stage is None:
        stage = IngestionStage(run_id=run.id, stage_key="normalize", status="pending")
        session.add(stage)
        await session.flush()
        event = DomainEvent(
            id=uuid4(), type="ingestion.normalize.requested", version=1,
            occurred_at=received_at, producer="modules.ingestion",
            payload={
                "run_id": str(run.id), "stage_id": str(stage.id),
                "source_generation": source_generation, "normalization_version": NORMALIZATION_VERSION,
            },
        )
        await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    existing_ids = set((await session.scalars(
        select(ObservationNormalization.observation_id).where(
            ObservationNormalization.stage_id == stage.id,
            *_materialization_scope(scope),
            ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
        )
    )).all())
    for observation in observations:
        if observation.id not in existing_ids:
            session.add(ObservationNormalization(
                workspace_id=scope.workspace_id, observation_id=observation.id, source_id=observation.source_id,
                run_id=run.id, stage_id=stage.id, source_generation=source_generation,
                normalization_version=NORMALIZATION_VERSION,
            ))
        if observation.received_at is None:
            observation.received_at = received_at
    await session.flush()
    return stage


async def prepare_document_materializations_in_uow(
    session: AsyncSession, document_id: UUID, *, source_id: UUID, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Lock the Document's materializations in id order before ``tombstone_document_materializations``.

    Individual-delete path only, so an oversized set raises before any mutation. The Document
    owner holds admission, Source and earlier owner locks; this commits nothing.
    """
    actual = await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (actual != access_fence or source_fence.id != source_id
            or source_fence.workspace_id != scope.workspace_id):
        raise HTTPException(status_code=409, detail="Cleanup authority changed")
    ids = list((await session.scalars(
        select(ObservationNormalization.id)
        .where(ObservationNormalization.document_id == document_id, *_materialization_scope(scope))
        .order_by(ObservationNormalization.id).limit(10_001).with_for_update(of=ObservationNormalization)
    )).all())
    if len(ids) > 10_000:
        raise DocumentCleanupPreparationLimitError("ingestion")


async def tombstone_document_materializations(session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Tombstone materializations through exact retained run/stage/batch/workspace lineage.

    The Document owner holds earlier admission/Source/deletion locks. No earlier lock
    acquisition or commit occurs; nullable document/version IDs retain owner scope. Rows were
    locked by prepare_document_materializations_in_uow.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await session.execute(
        update(ObservationNormalization)
        .where(ObservationNormalization.document_id == document_id, *_materialization_scope(scope))
        .values(disposition="skipped", error_code="document_deleted", document_id=None, document_version_id=None)
    )


async def get_event_delivery(session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> EventDelivery | None:
    """Read scoped outbox identity with a defensive payload copy and no row locks.

    Current owner admission and retained actor/membership must match; foreign IDs
    return None. Event type/version/producer and nullable dispatch timestamp are captured,
    but this DTO does not prove held locks, a current dispatch claim or a still-live Source.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(EventOutbox).where(EventOutbox.id == event_id, *_event_scope(scope)))
    if event is None:
        return None
    return EventDelivery(id=event.id, workspace_id=event.workspace_id, actor_user_id=event.actor_user_id,
                         membership_revision=event.membership_revision, type=event.type,
                         version=event.version, producer=event.producer, dispatched_at=event.dispatched_at,
                         status=event.status, payload=deepcopy(event.payload))


async def lock_source_purge_event_in_uow(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    event_types: tuple[str, ...],
) -> EventDelivery | None:
    """Lock the complete existing purge/run/coverage event union in one ascending UUID order.

    Caller holds admission, optional Memory privacy, Source and exact retained operation;
    canonical cancellation also holds its seven prepared Ingestion row sets. Validate the
    requested six-field subject nonlocking before acquiring any outbox lock. Historical run
    events include all epochs/generations but only this workspace/actor and Source lineage;
    their bodies are never loaded into Python. Include an existing deterministic coverage
    row so later arming acquires no omitted lower UUID. Fresh requested/coverage validation
    rejects collisions or a changed subject. Caller still compares original queued status/
    dispatched_at before effects. No early locks, mutation, commit or I/O; total SQL work
    grows with Source history while Python discovery stays bounded.
    """
    if (not isinstance(event_types, tuple) or not event_types
            or any(not isinstance(event_type, str) or event_type not in _SOURCE_PURGE_PRODUCERS
                   for event_type in event_types)):
        raise ValueError("Only explicit Source purge event types are allowed")
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type.in_(event_types), *_event_scope(scope),
    ).execution_options(populate_existing=True))
    if event is None:
        return None
    subject = _source_purge_event_subject(
        event.type, event.version, event.producer, event.payload, scope=scope,
    )
    if subject is None:
        return None
    operation_id, retained = subject
    actual = await sources.read_source_purge_job_identity(
        session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if actual != retained:
        return None
    captured_envelope = (event.type, event.version, event.producer)
    coverage_id = uuid5(operation_id, "source-memory-coverage")
    coverage_exists = await session.scalar(select(EventOutbox.id).where(EventOutbox.id == coverage_id)) is not None
    if coverage_exists:
        coverage = await session.scalar(select(EventOutbox).where(
            EventOutbox.id == coverage_id, *_event_scope(retained),
        ).execution_options(populate_existing=True))
        if (coverage is None or coverage.type != "source.purge.coverage"
                or _source_purge_event_subject(coverage.type, coverage.version, coverage.producer,
                                               coverage.payload, scope=retained) != subject):
            raise HTTPException(status_code=409, detail="Source coverage event identity changed")
    run_ids = select(cast(IngestionRun.id, String)).where(
        IngestionRun.source_id == retained.source_id, IngestionRun.workspace_id == retained.workspace_id,
    )
    # Sort and lock actual rows inside PostgreSQL; COUNT consumes the complete lock CTE.
    # Do not lock the requested row first, or materialize historical event bodies/IDs in Python.
    locked_events = select(EventOutbox.id).where(
        EventOutbox.workspace_id == retained.workspace_id,
        EventOutbox.actor_user_id == retained.actor_user_id,
        or_(EventOutbox.id == event_id, EventOutbox.id == coverage_id,
            EventOutbox.payload["run_id"].astext.in_(run_ids)),
    ).order_by(EventOutbox.id).with_for_update().cte("source_purge_event_locks").prefix_with("MATERIALIZED")
    await session.scalar(select(func.count()).select_from(locked_events))
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type.in_(event_types), *_event_scope(scope),
    ).execution_options(populate_existing=True))
    if event is None or (event.type, event.version, event.producer) != captured_envelope:
        return None
    fresh_subject = _source_purge_event_subject(event.type, event.version, event.producer, event.payload, scope=scope)
    if fresh_subject != subject or await sources.read_source_purge_job_identity(
        session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) != retained:
        return None
    if coverage_exists:
        coverage = await session.scalar(select(EventOutbox).where(
            EventOutbox.id == coverage_id, *_event_scope(retained),
        ).execution_options(populate_existing=True))
        if (coverage is None or coverage.type != "source.purge.coverage"
                or _source_purge_event_subject(coverage.type, coverage.version, coverage.producer,
                                               coverage.payload, scope=retained) != subject):
            raise HTTPException(status_code=409, detail="Source coverage event identity changed")
    return EventDelivery(id=event.id, workspace_id=event.workspace_id, actor_user_id=event.actor_user_id,
                         membership_revision=event.membership_revision, type=event.type,
                         version=event.version, producer=event.producer, dispatched_at=event.dispatched_at,
                         status=event.status, payload=deepcopy(event.payload))


async def set_event_delivery(
    session: AsyncSession,
    event_id: UUID,
    status: Literal["failed", "pending", "delivered"],
    *,
    next_attempt_at: datetime | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Update exact scoped outbox status/retry time without earlier locks or commit.

    Caller holds ordered admission/domain locks; nonlocking revision revalidation
    cannot rebase a retained epoch. Foreign rows return False.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    values: dict[str, object] = {"status": status}
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await session.execute(
        update(EventOutbox)
        .where(EventOutbox.id == event_id, *_event_scope(scope))
        .values(**values)
        .returning(EventOutbox.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_event_delivered(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Acknowledge the scoped outbox row without committing or acquiring earlier locks.

    Callers hold ordered admission/Source/domain locks and own replay/final commit;
    this is the Ingestion owner seam for atomic worker settlement, not Redis proof.
    """
    return await set_event_delivery(session, event_id, "delivered", scope=scope,
                                    multi_workspace_enabled=multi_workspace_enabled)


async def get_source_cursor(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> str | None:
    """Return the persisted collection cursor, or None before first ingestion."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) is None:
        return None
    state = await session.get(SourceIngestionState, source_id)
    return state.cursor if state is not None else None


async def reset_native_collection_cursor(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Clear one native provider cursor only after its exact collection and run leases are inactive.

    The caller holds ordered admission, Source, connector/provider identity locks and
    supplies its captured AccessFence before this state lock. Nonlocking equality
    revalidation rejects stale configuration/epoch without earlier lock acquisition. The
    owner commits the reset with replay publication; a live collector or nonterminal run rejects it.
    """
    current_access = await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if current_access != access_fence:
        raise HTTPException(status_code=409, detail="Collection access fence is stale")
    source = await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        return
    now = datetime.now(UTC)
    if state.collection_lease_token is not None and state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="A collection is still active")
    active_run = await session.scalar(
        select(IngestionRun.id).where(
            *_run_scope(scope), IngestionRun.source_id == source_id,
            IngestionRun.status.not_in(("succeeded", "failed")),
        ).limit(1).with_for_update()
    )
    if active_run is not None:
        raise HTTPException(status_code=409, detail="An ingestion run is still active")
    state.collection_lease_token = None
    state.lease_run_id = None
    state.lease_expires_at = None
    state.cursor = None
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)


async def _validate_source_ingestion_purge_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Compare exact internal Source subject and both actual current fences without locks.

    Caller already holds real account/workspace/membership and Source locks throughout its
    purge transaction. DTO construction is not lock proof. Archived matching Sources are
    permitted; active-only credential/retained-evidence acquiring wrappers are inappropriate.
    Missing/malformed proof or any changed status/local_only/generation/access field fails
    before effects, without upgrading epoch/generation, acquiring early locks or committing.
    """
    if (not isinstance(scope, InternalJobScope) or not isinstance(access_fence, AccessFence)
            or not isinstance(source_fence, SourceFence) or not isinstance(source_id, UUID)
            or scope.source_id != source_id or scope.source_generation is None
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or source_fence.generation != scope.source_generation):
        raise HTTPException(status_code=409, detail="Source ingestion purge proof is invalid")
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence or current_source is None or current_source != source_fence:
        raise HTTPException(status_code=409, detail="Source ingestion purge proof is stale")


async def _source_ingestion_purge_lineage(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
) -> None:
    """Abort contradictory own-table FK lineage before a complete Source purge can widen.

    Read only scalar EXISTS under already-held admission/Source serialization; no foreign
    ORM, payload, locks, mutation or history list. Check both direct Source rows and children
    reached through actual batch/run/stage/observation cascades, including foreign state lease
    references to selected runs. All historical epochs/generations remain eligible. Document/
    version SET NULL may clear journal references after preparation and is not a mismatch.
    """
    batch_ids = select(IngestionBatch.id).where(IngestionBatch.source_id == source_id)
    run_ids = select(IngestionRun.id).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
    )
    observation_ids = select(SourceObservation.id).where(SourceObservation.source_id == source_id)
    stage_ids = select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
    invalid_run = select(IngestionRun.id).where(
        or_(IngestionRun.source_id == source_id, IngestionRun.batch_id.in_(batch_ids)),
        or_(IngestionRun.source_id != source_id, IngestionRun.workspace_id != scope.workspace_id,
            IngestionRun.actor_user_id != scope.actor_user_id, IngestionRun.batch_id.not_in(batch_ids)),
    ).exists()
    invalid_observation = select(SourceObservation.id).where(
        or_(SourceObservation.source_id == source_id, SourceObservation.batch_id.in_(batch_ids)),
        or_(SourceObservation.source_id != source_id, SourceObservation.batch_id.not_in(batch_ids)),
    ).exists()
    matching_journal_parents = select(SourceObservation.id).join(
        IngestionRun, IngestionRun.batch_id == SourceObservation.batch_id,
    ).join(IngestionStage, IngestionStage.run_id == IngestionRun.id).join(
        IngestionBatch, IngestionBatch.id == IngestionRun.batch_id,
    ).where(
        SourceObservation.id == ObservationNormalization.observation_id,
        IngestionRun.id == ObservationNormalization.run_id,
        IngestionStage.id == ObservationNormalization.stage_id,
        SourceObservation.source_id == source_id, IngestionBatch.source_id == source_id,
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
        IngestionRun.actor_user_id == scope.actor_user_id,
        IngestionBatch.source_generation == ObservationNormalization.source_generation,
    ).correlate(ObservationNormalization).exists()
    invalid_journal = select(ObservationNormalization.id).where(
        or_(ObservationNormalization.source_id == source_id,
            ObservationNormalization.observation_id.in_(observation_ids),
            ObservationNormalization.run_id.in_(run_ids), ObservationNormalization.stage_id.in_(stage_ids)),
        or_(ObservationNormalization.source_id != source_id,
            ObservationNormalization.workspace_id != scope.workspace_id, ~matching_journal_parents),
    ).exists()
    invalid_state = select(SourceIngestionState.source_id).where(or_(
        and_(SourceIngestionState.source_id != source_id, SourceIngestionState.lease_run_id.in_(run_ids)),
        and_(SourceIngestionState.source_id == source_id, SourceIngestionState.lease_run_id.is_not(None),
             SourceIngestionState.lease_run_id.not_in(run_ids)),
    )).exists()
    if await session.scalar(select(or_(invalid_run, invalid_observation, invalid_journal, invalid_state))):
        raise HTTPException(status_code=409, detail="Source ingestion purge lineage is inconsistent")


async def _lock_ingestion_purge_identities(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
) -> None:
    """Lock the seven complete own row sets in their fixed local order, returning no data.

    Caller has freshly validated exact Source/access proof and own cascade lineage. Each
    MATERIALIZED identity-only ordered FOR UPDATE CTE is exhausted by a scalar COUNT in
    PostgreSQL, so Python memory stays constant and no payload/config/secret is collected.
    No cap/SKIP LOCKED/partial commit omits history; all locks remain held through one apply.
    Credentials include revoked rows; state absence stays absent; historical run epochs and
    batch generations are not restricted to the purge's newly archived generation/revision.
    """
    batch_ids = select(IngestionBatch.id).where(IngestionBatch.source_id == source_id)
    run_ids = select(IngestionRun.id).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
        IngestionRun.batch_id.in_(batch_ids),
    )
    observation_ids = select(SourceObservation.id).where(
        SourceObservation.source_id == source_id, SourceObservation.batch_id.in_(batch_ids),
    )
    stage_ids = select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
    queries = (
        select(CollectorCredential.token_hash).where(CollectorCredential.source_id == source_id)
        .order_by(CollectorCredential.token_hash),
        select(SourceIngestionState.source_id).where(SourceIngestionState.source_id == source_id)
        .order_by(SourceIngestionState.source_id),
        select(IngestionBatch.id).where(IngestionBatch.source_id == source_id).order_by(IngestionBatch.id),
        select(IngestionRun.id).where(IngestionRun.id.in_(run_ids)).order_by(IngestionRun.id),
        select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
        .order_by(IngestionStage.run_id, IngestionStage.stage_key, IngestionStage.id),
        select(SourceObservation.id).where(SourceObservation.id.in_(observation_ids)).order_by(SourceObservation.id),
        select(ObservationNormalization.id).where(
            ObservationNormalization.workspace_id == scope.workspace_id,
            ObservationNormalization.source_id == source_id,
            ObservationNormalization.observation_id.in_(observation_ids),
            ObservationNormalization.run_id.in_(run_ids), ObservationNormalization.stage_id.in_(stage_ids),
        ).order_by(ObservationNormalization.id),
    )
    for query in queries:
        locked = query.with_for_update().cte("source_ingestion_purge_locks").prefix_with("MATERIALIZED")
        await session.scalar(select(func.count()).select_from(locked))


async def prepare_source_ingestion_purge_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare complete Ingestion roots/children after Source+Documents/URI, before operation/outbox.

    Caller retains real account/workspace/Source locks and the same transaction through D
    cleanup and cancellation. Fresh nonlocking fences and own FK lineage must match; then
    lock credentials -> state -> batches -> runs -> stages -> observations -> journals,
    each in stable identity order. No outbox lock, mutation, token, commit or external I/O.
    Memory use is bounded; complete atomic SQL/transaction work grows with Source history.
    """
    await _validate_source_ingestion_purge_in_uow(session, source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)
    await _lock_ingestion_purge_identities(session, source_id, scope=scope)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)


async def cancel_and_purge_source_ingestion(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Apply complete prepared Source cancellation after claimed D canonical cleanup, without early locks.

    Caller holds real access/Source, prepared Documents/URI and seven Ingestion row sets,
    exact purge operation and complete sorted existing event union; original queued dispatch
    CAS succeeded before D apply. Revalidate fences/own lineage nonlocking, then fail all
    matching historical run events (all statuses/epochs/generations), delete observations and
    batches with existing cascades, clear only existing state leases and revoke all credentials.
    Keep cursor, retained outbox payload/principal/dispatch/schedule and purge receipts intact.
    No missing state insertion, preparation/acquiring call, commit or external effect. D has
    already removed derived structured observations before ingestion-observation FK cascades.
    """
    await _validate_source_ingestion_purge_in_uow(session, source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)
    # A lifecycle purge cancels all historical generations/epochs of this verified
    # Source, rather than only the newly archived generation of its operation.
    run_ids = select(cast(IngestionRun.id, String)).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
    )
    await session.execute(
        update(EventOutbox)
        .where(EventOutbox.workspace_id == scope.workspace_id,
               EventOutbox.actor_user_id == _actor_id(scope),
               EventOutbox.payload["run_id"].astext.in_(run_ids))
        .values(status="failed")
    )
    await session.execute(delete(SourceObservation).where(SourceObservation.source_id == source_id))
    await session.execute(delete(IngestionBatch).where(IngestionBatch.source_id == source_id))
    # Preparation holds the existing state; reread without reacquiring roots after outboxes.
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == source_id,
    ).execution_options(populate_existing=True))
    if state is not None:
        state.lease_run_id = None
        state.collection_lease_token = None
        state.lease_expires_at = None
    await session.execute(update(CollectorCredential).where(CollectorCredential.source_id == source_id)
                          .values(revoked_at=datetime.now(UTC)))


async def collector_can_ingest(
    session: AsyncSession, source_id: UUID, token: str, *, credential_scope: str = "ingestion:write", scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Check literal credential capability and active Source under actual scoped owner admission.

    This nonlocking read is not intake admission. Collection wrappers acquire ordered
    access/Source locks and recheck token revocation before effects.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    credential_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == source_id,
            CollectorCredential.scope == credential_scope,
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    source = await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if credential_valid else None
    return source is not None and source.status == "active"


def _encode_telegram_cursor(cursor: TelegramCursor) -> str:
    """Serialize the exact bounded Telegram cursor object stored in source state."""
    return json.dumps(cursor.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _decode_telegram_cursor(value: str | None) -> TelegramCursor | None:
    """Parse a persisted Telegram cursor and fail closed on corrupt state."""
    if value is None:
        return None
    try:
        return TelegramCursor.model_validate_json(value)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Telegram collection cursor is invalid") from exc


async def _lock_source_projection(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool, expected_access_fence: AccessFence | None = None,
) -> tuple[ConnectorSource | None, SourceFence | None, AccessFence]:
    """Hold the narrow source fence while reading the detached connector projection.

    Entry holds no domain locks. Source set admission captures AccessFence before the
    Source lifecycle lock; its detached configuration is compared under that lock.
    Caller owns release/commit, and no network work occurs under these locks.
    """
    locked = await sources.lock_source_set(
        session, (source_id,), scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected_access_fence,
    )
    fence = locked.fences[0]
    projection = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None or (
        projection.workspace_id != fence.workspace_id
        or projection.status != fence.status or projection.generation != fence.generation
    ):
        raise HTTPException(status_code=409, detail="Source projection changed under lifecycle lock")
    return projection, fence, locked.access_fence



async def validate_connector_collection_in_uow(
    session: AsyncSession, lease: ConnectorCollectionLease, *, collector_token: str,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
    source_fence: SourceFence,
) -> bool:
    """Validate original lease/bearer under prepared parents; lock only Ingestion state.

    Caller holds original account/session admission where applicable, Source/provisioning,
    required slots/native/world and all Source collector credentials; for GitHub those
    credentials precede its already-held grant, which precedes this state lock. Fresh full
    original lease/access/Source and nonlocking active applied Connector proof are mandatory.
    Read the exact prepared ingestion:write hash freshly, never reacquire earlier rows.
    Unavailable Source fails404, stale fences/provisioning409, ineligible bearer401; malformed
    lease/scope/admission and storage errors retain typed failures. False means only changed,
    missing or expired reservation (exact token, no run, cursor, expiry and future UTC time).
    No rotation/renewal/cursor/health/replay mutation, commit, rollback or I/O. Caller releases
    SQL before each physical send. This is not the original provider-credential comparison
    or acceptance's atomic proof, and carries no retained-effect journal authority.
    """
    original_access = _lease_access_fence(lease)
    _lease_scope(lease, scope)
    if access_fence != original_access:
        raise HTTPException(status_code=409, detail="Collection access capture differs from original lease")
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source_fence = await sources.get_source_fence(
        session, lease.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await sources.get_connector_source(
        session, lease.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_source_fence is None or source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if (current_access != original_access or current_source_fence != source_fence
            or source_fence.id != lease.source_id or source_fence.workspace_id != lease.workspace_id
            or source_fence.generation != lease.source_generation
            or source.status != "active" or source.local_only):
        raise HTTPException(status_code=409, detail="Original collection Source/access fence changed")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(source_generation=lease.source_generation, connector_revision=lease.connector_revision),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    eligible = await session.scalar(select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash, CollectorCredential.source_id == lease.source_id,
        CollectorCredential.scope == "ingestion:write", CollectorCredential.revoked_at.is_(None),
    ))
    if eligible is None:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == lease.source_id,
    ).with_for_update().execution_options(populate_existing=True))
    return bool(
        state is not None and state.collection_lease_token == lease.token and state.lease_run_id is None
        and state.cursor == lease.cursor_before and state.lease_expires_at is not None
        and state.lease_expires_at == lease.expires_at and state.lease_expires_at > datetime.now(UTC)
    )


async def acquire_connector_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    collector_token: str, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorCollectionLease:
    """Reserve one native fetch under source, provisioning, credential, then state locks.

    The lease token is distinct from a processing run lease. It is committed with
    the source collection-start event before the provider performs network I/O;
    only an expired owner can be replaced and every later write rechecks its token.
    Detached lease captures workspace/actor/membership/configuration epoch; entry holds
    no domain locks. All Source collector rows are actually prepared after any native
    credential and before state; the exact ingestion:write hash is read under those locks.
    Provider sends occur only after this wrapper commits.
    """
    source, source_fence, access_fence = await _lock_source_projection(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active" or source.generation != source_generation:
        raise HTTPException(status_code=409, detail="Source generation is not active")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(source_generation=source_generation, connector_revision=connector_revision),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    if source.provider == "telegram":
        credential = await connectors.get_native_credential_snapshot(
            session, source_id, source_generation=source_generation,
            connector_revision=connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if (
            credential is None or credential.source_generation != source_generation
            or credential.configuration_revision != connector_revision
            or credential.state != "ready" or not credential.verified_bot_id
            or not credential.encrypted_token or credential.validated_at is None
        ):
            raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
    await lock_source_credentials_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash,
        CollectorCredential.source_id == source_id,
        CollectorCredential.scope == "ingestion:write",
        CollectorCredential.revoked_at.is_(None),
    )))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source collection is already in progress")
    token = uuid4()
    expires_at = now + COLLECTION_LEASE
    state.lease_run_id = None
    state.collection_lease_token = token
    state.lease_expires_at = expires_at
    if not await sources.record_collection_started_in_uow(session, source_id, source_generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise HTTPException(status_code=409, detail="Source is not active")
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    return ConnectorCollectionLease(
        workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision,
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, token=token,
        cursor_before=state.cursor, expires_at=expires_at, configuration_revision=access_fence.configuration_revision,
    )


async def read_telegram_collection_state(
    session: AsyncSession, lease: ConnectorCollectionLease, *, scope: Scope, multi_workspace_enabled: bool,
) -> TelegramCursor | None:
    """Read a reserved Telegram cursor after rechecking source, revision, and bot fences.

    Entry holds no domain locks and rechecks original principal/configuration before
    Source. This short transaction verifies the current native binding and releases every
    row lock before the connector performs network I/O.
    """
    _lease_scope(lease, scope)
    source, source_fence, access_fence = await _lock_source_projection(session, lease.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=AccessFence(lease.workspace_id, lease.actor_user_id, lease.membership_revision, lease.configuration_revision))
    from modules.connectors import public as connectors

    if (
        source is None or source.provider != "telegram" or source.status != "active"
        or source.generation != lease.source_generation
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram collection reservation is stale")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
        ),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram connector revision changed")
    credential = await connectors.get_native_credential_snapshot(
        session, lease.source_id,
        source_generation=lease.source_generation,
        connector_revision=lease.connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if credential is None or credential.state != "ready" or not credential.verified_bot_id:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
    if (
        credential.source_generation != lease.source_generation
        or credential.configuration_revision != lease.connector_revision
        or credential.encrypted_token is None or credential.validated_at is None
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Native Telegram credential fence changed")
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if (
        state is None or state.collection_lease_token != lease.token
        or state.lease_expires_at is None or state.lease_expires_at <= now
        or state.cursor != lease.cursor_before
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram collection reservation is stale")
    cursor = _decode_telegram_cursor(state.cursor)
    if cursor is not None and cursor.bot_id != credential.verified_bot_id:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram cursor bot binding changed")
    await session.commit()
    return cursor


async def release_connector_collection(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    error_code: str | None, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Release the matching reservation; stale revisions never publish old health errors.

    An exact token from the same source generation is cleared even after a desired
    revision changes so it cannot block the replacement. Health updates are only
    written while the original connector revision is still active.
    Entry holds no domain locks; original lease principal/configuration is mandatory.
    Success preserves the wrapper commit using its captured scoped replay fence.
    """
    _lease_scope(lease, scope)
    source, source_fence, access_fence = await _lock_source_projection(session, lease.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=AccessFence(lease.workspace_id, lease.actor_user_id, lease.membership_revision, lease.configuration_revision))
    from modules.connectors import public as connectors

    fence_current = False
    if source is not None and source.status == "active" and source.generation == lease.source_generation:
        fence_current = await connectors.require_collection_fence(
            session, source,
            connectors.CollectionFence(
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
            ),
            lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    if (
        source is None or source.generation != lease.source_generation or state is None
        or state.collection_lease_token != lease.token
    ):
        await session.rollback()
        return False
    if error_code is not None and (not error_code or len(error_code) > 64):
        raise ValueError("Collection error code must be bounded")
    state.collection_lease_token = None
    state.lease_expires_at = None
    now = datetime.now(UTC)
    if error_code is not None and fence_current:
        await sources.record_collection_result_in_uow(session, source.id, source.generation, now, error_code, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    return True


def validate_telegram_record_delivery(
    record: IngestionRecord,
    provider_metadata: object,
    proof: TelegramDeliveryProof,
    raw: TelegramRawDelivery,
    *,
    allowed_chat_ids: tuple[str, ...] | None = None,
) -> None:
    """Bind mapper identity, timestamps, version, and bot epoch to the raw update.

    Called before receipt/cursor commit and again when normalizing persisted owner
    proof. Telegram's Unix clocks and identity are compared to exact Bot API fields;
    optional scope filtering is enforced only at initial acceptance.
    """
    from modules.knowledge.documents.schemas import ProviderRecordMetadata

    metadata = ProviderRecordMetadata.model_validate(provider_metadata)
    detail = metadata.telegram
    update = raw.update
    edited_present = "edited_channel_post" in update
    original_present = "channel_post" in update
    message_key = "edited_channel_post" if edited_present else "channel_post"
    message = update.get(message_key)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(chat, dict) or detail is None:
        raise ValueError("Telegram channel message is missing")
    chat_id_value = chat.get("id")
    message_id_value = message.get("message_id")
    date_value = message.get("date")
    edit_date_value = message.get("edit_date")
    if (
        isinstance(update.get("update_id"), bool)
        or update.get("update_id") != raw.update_id
        or raw.update_id != proof.update_id
        or proof.raw_update_sha256 != raw.raw_update_sha256
        or edited_present == original_present
        or detail.edited_received != edited_present
        or chat.get("type") != "channel"
        or isinstance(chat_id_value, bool) or not isinstance(chat_id_value, int)
        or isinstance(message_id_value, bool) or not isinstance(message_id_value, int) or message_id_value < 0
        or isinstance(date_value, bool) or not isinstance(date_value, int) or date_value < 0
        or (edited_present and (isinstance(edit_date_value, bool) or not isinstance(edit_date_value, int) or edit_date_value < 0))
    ):
        raise ValueError("Telegram raw identity or clock fields are invalid")
    channel_id = str(chat_id_value)
    message_id = str(message_id_value)
    if allowed_chat_ids is not None and channel_id not in allowed_chat_ids:
        raise ValueError("Telegram channel is outside configured source scope")
    try:
        published_at = datetime.fromtimestamp(date_value, UTC)
        edited_at = datetime.fromtimestamp(edit_date_value, UTC) if edited_present and edit_date_value is not None else None
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Telegram raw timestamps are invalid") from exc
    observed_at = edited_at if edited_present else published_at
    assert observed_at is not None
    expected_version = f"telegram:{proof.epoch}:{proof.update_id}:{observed_at.isoformat()}"
    if (
        metadata.provider != "telegram"
        or metadata.identity != record.provider_id
        or record.provider_id != f"telegram:{channel_id}:{message_id}"
        or metadata.provider_version != record.version
        or record.version != expected_version
        or detail.bot_id != proof.bot_id
        or detail.epoch != proof.epoch
        or detail.update_id != proof.update_id
        or detail.channel_id != channel_id
        or detail.message_id != message_id
        or detail.published_at != published_at
        or detail.edited_at != edited_at
        or metadata.provider_modified_at != edited_at
        or record.observed_at != observed_at
        or metadata.timestamp_basis != ("provider_modified" if edited_present else "provider_published")
        or detail.raw_update_sha256 is not None
    ):
        raise ValueError("Telegram mapped record does not match its raw delivery")


async def accept_native_collection(
    session: AsyncSession,
    payload: NativeCollectionBatch,
    *,
    collector_token: str, lease: ConnectorCollectionLease, scope: Scope, multi_workspace_enabled: bool,
    expected_native_operation_id: UUID | None,
    expected_world_credential_operation_id: UUID | None,
) -> NativeCollectionReceipt:
    """Authenticate, reclassify, and atomically persist one reserved native page.

    Source and connector/native credential fences precede batch replay lookup;
    the collection token and cursor are rechecked only for new work. Telegram
    proofs are recomputed from raw updates and the verified bot binding. GitHub
    proof shape, digest, and current grant fence are checked before exact replay
    lookup; for new work, records and cursor are recomputed only after the live
    reservation is confirmed. Targeted hint acknowledgement is flush-only in the
    same batch/source visibility transaction. Ambiguous missing current GitHub
    targets acknowledge uncertainty before pausing the source, fencing current
    evidence while preserving owner history. Replay head publication is last.
    Scope/configured flag precede Source/Connector grant locks. Run/outbox retain actor
    and original membership. Post-state GitHub proof validation consumes the captured
    binding and never reacquires earlier admission/Source/grant locks.
    Possible visibility pause prepares cleanup before grant/state and the complete hint
    set before capacity. The validated empty receipt retains G; late apply returns G+1
    solely for replay, skips inactive health, and commits with the original access fence.
    The actual owner-issued lease is required across provider I/O: exact principal and
    payload Source/generation/token/connector/cursor must match, and its original workspace
    configuration fence is compared during admission before acquiring the Source lock.
    Required internal provider captures precede replay lookup: Telegram's original native
    UUID/access and Alpha's original world UUID must still match under their early locks;
    inapplicable/missing capture shapes fail422, rotated credentials409. Credential operation
    IDs never enter the stable content hash. All collector rows are prepared after provider
    credentials (or inside early visibility preparation before Tools), then exact bearer
    eligibility precedes GitHub grant/state. Replay preserves historical receipt semantics;
    only new work requires no run, exact token/cursor/expiry equal to the actual lease and
    future expiry. No state revision field or journal-only acceptance authority is invented.
    """
    expected_access_fence = _lease_access_fence(lease)
    _lease_scope(lease, scope)
    if (lease.source_id != payload.source_id or lease.source_generation != payload.source_generation
            or lease.token != payload.lease_token or lease.connector_revision != payload.connector_revision
            or lease.cursor_before != payload.cursor_before):
        raise HTTPException(status_code=409, detail="Native collection payload differs from original lease")
    source, source_fence, access_fence = await _lock_source_projection(
        session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active" or source.local_only or source.generation != payload.source_generation:
        raise HTTPException(status_code=409, detail="Source generation changed during collection")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if source.provider == "telegram":
        if not isinstance(expected_native_operation_id, UUID) or expected_world_credential_operation_id is not None:
            raise HTTPException(status_code=422, detail="Original Telegram credential operation is required")
    elif source.provider == "alpha_vantage":
        if not isinstance(expected_world_credential_operation_id, UUID) or expected_native_operation_id is not None:
            raise HTTPException(status_code=422, detail="Original Alpha credential operation is required")
    elif expected_native_operation_id is not None or expected_world_credential_operation_id is not None:
        raise HTTPException(status_code=422, detail="Credential operation capture is not valid for this provider")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(
            source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
        ),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    bot_id: str | None = None
    github_proof = None
    github_fence = None
    needs_visibility_fence = False
    if source.provider == "telegram":
        credential = await connectors.get_native_credential_snapshot(
            session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if (
            credential is None or credential.source_generation != payload.source_generation
            or credential.configuration_revision != payload.connector_revision
            or credential.operation_id != expected_native_operation_id
            or credential.access_fence != expected_access_fence
            or credential.workspace_id != lease.workspace_id or credential.source_id != lease.source_id
            or credential.state != "ready" or not credential.verified_bot_id
            or credential.encrypted_token is None or credential.validated_at is None
        ):
            raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
        bot_id = credential.verified_bot_id
        if any(item.update_id != item.update.get("update_id") for item in payload.telegram_raw_deliveries):
            raise HTTPException(status_code=422, detail="Telegram update identity is invalid")
    elif payload.telegram_raw_deliveries or payload.telegram_deliveries:
        raise HTTPException(status_code=422, detail="Telegram proof is not valid for this provider")
    if source.provider == "alpha_vantage":
        assert isinstance(expected_world_credential_operation_id, UUID)
        if not await connectors.validate_world_credential_operation_in_uow(
            session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
            expected_operation_id=expected_world_credential_operation_id,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_fence,
        ):
            raise HTTPException(status_code=409, detail="Original Alpha credential operation changed")
    if source.provider == "github":
        from modules.connectors.github.schemas import GitHubSegmentProof

        if payload.github_segment is None or payload.telegram_raw_deliveries or payload.telegram_deliveries:
            raise HTTPException(status_code=422, detail="GitHub collection proof is required")
        try:
            github_proof = GitHubSegmentProof.model_validate(payload.github_segment.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="GitHub segment proof is malformed") from exc
        if github_proof.fence.connector_revision != payload.connector_revision:
            raise HTTPException(status_code=409, detail="GitHub collection revision is stale")
        if github_proof.hint_claim is not None:
            # Typed proof chooses lock intent only; live reservation/segment validation
            # must still succeed before any acknowledgement or lifecycle mutation.
            needs_visibility_fence = (
                github_proof.target_outcome == "forbidden"
                or github_proof.hint_claim.intent == "reconcile"
                and github_proof.hint_claim.locator_kind == "repository"
                and github_proof.target_outcome == "not_found"
                or github_proof.hint_claim.intent in {"refresh", "delete_candidate"}
                and github_proof.hint_claim.locator_kind in {"number", "release_id", "sha"}
                and github_proof.target_outcome in {"not_found", "forbidden", "partial"}
                or github_proof.hint_claim.intent == "visibility_check"
                and github_proof.target_outcome == "not_found"
                and github_proof.hint_claim.locator_kind in {"repository", "installation"}
            )
        if needs_visibility_fence:
            await sources.prepare_source_pause_for_connector_in_uow(
                session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence, source_fence=source_fence,
            )
    elif payload.github_segment is not None:
        raise HTTPException(status_code=422, detail="GitHub proof is not valid for this provider")

    # Visibility preparation already holds tokens before Tools: never reenter that set.
    if not needs_visibility_fence:
        await lock_source_credentials_in_uow(
            session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_fence,
        )
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash,
        CollectorCredential.source_id == source.id,
        CollectorCredential.scope == "ingestion:write",
        CollectorCredential.revoked_at.is_(None),
    )))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    if github_proof is not None:
        github_fence = await connectors.lock_github_binding_fence_in_uow(
            session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_fence)
        if github_fence is None or github_proof.fence != github_fence:
            raise HTTPException(status_code=409, detail="GitHub grant is unavailable or requires reconnection")

    stable_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records]
    stable = {
        "source_id": str(payload.source_id), "source_generation": payload.source_generation,
        "connector_revision": payload.connector_revision, "cursor_before": payload.cursor_before,
        "cursor_after": payload.cursor_after,
        "raw_hashes": [item.raw_update_sha256 for item in payload.telegram_raw_deliveries],
        "telegram_deliveries": [item.model_dump(mode="json") for item in payload.telegram_deliveries],
        "github_segment": github_proof.model_dump(mode="json") if github_proof is not None else None,
        "records": stable_records, "coverage": payload.coverage,
    }
    payload_hash = _digest(stable)
    batch_key = f"native:{source.provider}:{payload_hash}"
    if len(batch_key) > 255:
        raise HTTPException(status_code=422, detail="Native batch key exceeds its bound")
    # Provider credential -> collector -> optional GitHub grant -> state -> receipt order.
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == source.id,
    ).with_for_update().execution_options(populate_existing=True))
    existing = await session.scalar(select(IngestionBatch).where(
        IngestionBatch.source_id == source.id,
        IngestionBatch.batch_key == batch_key,
    ))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="Native receipt conflicts with an existing batch")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
        if run is None:
            raise RuntimeError("Native ingestion batch has no run")
        if state is not None and state.collection_lease_token == payload.lease_token:
            # A replay may have acquired a fresh reservation; release only that exact token.
            state.collection_lease_token = None
            state.lease_expires_at = None
            await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        count = len(payload.records)
        return NativeCollectionReceipt(
            workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision,
            batch_id=existing.id, run_id=run.id,
            status="queued" if count else ("succeeded" if payload.telegram_raw_deliveries else "no_changes"),
            received_update_count=len(payload.telegram_raw_deliveries), record_count=count,
            coverage=payload.coverage, cursor_after=payload.cursor_after,
        )

    if state is None or state.collection_lease_token != payload.lease_token or state.lease_run_id is not None:
        raise HTTPException(status_code=409, detail="Native collection reservation is stale")
    now = datetime.now(UTC)
    if (state.lease_expires_at is None or state.lease_expires_at != lease.expires_at
            or state.lease_expires_at <= now):
        raise HTTPException(status_code=409, detail="Native collection reservation expired")
    if state.cursor != payload.cursor_before:
        raise HTTPException(status_code=409, detail="Native collection cursor is stale")

    classification: TelegramProbeClassification | None = None
    accepted_proofs: dict[int, TelegramDeliveryProof] = {}
    cursor_after = payload.cursor_after
    if github_proof is not None:
        github_segment = await connectors.validate_github_collection_segment_in_uow(
            session, source=source, reserved_cursor_before=state.cursor,
            proof=github_proof, binding_fence=github_fence, access_fence=access_fence,
            source_fence=source_fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        supplied_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records]
        validated_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in github_segment.records]
        if (
            supplied_records != validated_records
            or payload.cursor_after != github_segment.cursor_after
            or payload.coverage != github_segment.coverage
        ):
            raise HTTPException(status_code=409, detail="GitHub collection transition is invalid")
        if needs_visibility_fence and (
            github_segment.records or github_segment.cursor_after != state.cursor
        ):
            # A paused G receipt cannot queue records retired by this same transaction.
            raise HTTPException(status_code=409, detail="GitHub visibility pause requires an empty unchanged segment")
        cursor_after = github_segment.cursor_after
    if source.provider == "telegram":
        current_cursor = _decode_telegram_cursor(state.cursor)
        classification = classify_telegram_probe(
            current_cursor, payload.telegram_raw_deliveries,
            received_at=payload.collected_at, verified_bot_id=bot_id or "",
        )
        if classification.conflict_code:
            raise HTTPException(status_code=409, detail=classification.conflict_code)
        if classification.delivery_proofs != tuple(payload.telegram_deliveries):
            raise HTTPException(status_code=409, detail="Telegram delivery proof is stale or invalid")
        computed_cursor = _encode_telegram_cursor(classification.cursor_after) if classification.cursor_after else state.cursor
        if computed_cursor != payload.cursor_after:
            raise HTTPException(status_code=409, detail="Telegram cursor transition is invalid")
        replay_ids = set(classification.replay_update_ids)
        accepted_proofs = {
            proof.update_id: proof for proof in classification.delivery_proofs
            if proof.update_id not in replay_ids
        }
    else:
        replay_ids = set()
    provider_records: list[tuple[IngestionRecord, dict[str, object], dict[str, object] | None]] = []
    seen_update_ids: set[int] = set()
    from modules.knowledge.documents.schemas import ProviderRecordMetadata

    for record in payload.records:
        try:
            metadata = ProviderRecordMetadata.model_validate(record.metadata.get("provider_record"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Native provider metadata is invalid") from exc
        if metadata.provider != source.provider or metadata.identity != record.provider_id or metadata.provider_version != record.version:
            raise HTTPException(status_code=422, detail="Native provider identity is invalid")
        if metadata.coverage != payload.coverage:
            raise HTTPException(status_code=422, detail="Native provider coverage is inconsistent")
        proof_envelope: dict[str, object] | None = None
        if source.provider == "telegram":
            detail = metadata.telegram
            proof = accepted_proofs.get(detail.update_id) if detail is not None else None
            raw = next((item for item in payload.telegram_raw_deliveries if detail is not None and item.update_id == detail.update_id), None)
            if (
                detail is None or proof is None or raw is None or detail.bot_id != bot_id
                or detail.channel_id not in tuple(typing_cast("Iterable[str]", source.configuration.get("telegram_chat_ids", ())))
                or proof.raw_update_sha256 != raw.raw_update_sha256
                or detail.raw_update_sha256 is not None
                or detail.update_id in seen_update_ids
            ):
                # A known exact replay is acknowledged in the cursor but creates no new version.
                if detail is not None and detail.update_id in replay_ids:
                    continue
                raise HTTPException(status_code=422, detail="Telegram document proof is invalid")
            try:
                validate_telegram_record_delivery(
                    record, metadata, proof, raw,
                    allowed_chat_ids=tuple(typing_cast("Iterable[str]", source.configuration.get("telegram_chat_ids", ()))),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Telegram document does not match its raw delivery") from exc
            seen_update_ids.add(detail.update_id)
            proof_envelope = proof.model_dump(mode="json")
        record_data = record.model_dump(mode="json", exclude={"collected_at"})
        telegram_envelope: dict[str, object] | None = None
        if proof_envelope is not None:
            assert detail is not None  # a proof envelope is only built for a validated Telegram detail
            raw = next(item for item in payload.telegram_raw_deliveries if item.update_id == detail.update_id)
            telegram_envelope = {
                "proof": proof_envelope,
                "raw_update": raw.model_dump(mode="json"),
            }
            # A normalized row must fit the worker's bounded single-record budget.
            persisted_size = len(json.dumps(
                {**record_data, "_native_telegram": telegram_envelope},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
            if persisted_size > 4 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Telegram observation exceeds normalization size limit")
        provider_records.append((record, record_data, telegram_envelope))

    batch = IngestionBatch(
        source_id=source.id, batch_key=batch_key, payload_hash=payload_hash,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run_status = "queued" if provider_records else "succeeded"
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=source.id, status=run_status)
    session.add(run)
    await session.flush()
    receive_stage = IngestionStage(
        run_id=run.id, stage_key="receive",
        status="pending" if provider_records else "succeeded",
        result_count=len(provider_records),
    )
    session.add(receive_stage)
    await session.flush()
    received_at = now
    seen: set[tuple[str, str, datetime]] = set()
    for record, record_data, telegram_envelope in provider_records:
        record_hash = _digest({
            "version": record.version, "content": record.content,
            "metadata": record_data["metadata"],
        })
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        observation_payload = dict(record_data)
        if telegram_envelope is not None:
            # Keep owner proof outside the caller record so the accepted hash remains stable.
            observation_payload["_native_telegram"] = telegram_envelope
        session.add(SourceObservation(
            source_id=source.id, batch_id=batch.id,
            provider_id=record.provider_id, record_hash=record_hash,
            payload=observation_payload, observed_at=record.observed_at,
            received_at=received_at, collected_at=payload.collected_at,
        ))
    changes: list[ReplayDraft] = [make_source_change(source.id, source.generation, source.status, scope=scope)]
    if provider_records:
        event = DomainEvent(
            id=uuid4(), type="ingestion.stage.requested", version=1,
            occurred_at=now, producer="modules.ingestion",
            payload={
                "run_id": str(run.id), "stage_id": str(receive_stage.id),
                "source_generation": source.generation,
                "connector_revision": payload.connector_revision,
            },
        )
        await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await session.flush()
        normalize_stage = await schedule_normalization(
            session, run, batch, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if normalize_stage is not None:
            changes.append(make_ingestion_change(
                source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status, scope=scope))
        state.lease_run_id = run.id
        state.collection_lease_token = None
        state.lease_expires_at = now + COLLECTION_LEASE
        changes.append(make_ingestion_change(
            source.id, run.id, run.status, receive_stage.stage_key, receive_stage.status, scope=scope))
    else:
        state.lease_run_id = None
        state.collection_lease_token = None
        state.lease_expires_at = None
        changes.append(make_ingestion_change(
            source.id, run.id, run.status, receive_stage.stage_key, receive_stage.status, scope=scope))
    state.cursor = cursor_after
    source_paused = False
    if github_proof is not None and github_proof.hint_claim is not None:
        from modules.connectors.public import GitHubHintClaim

        try:
            hint_claim = GitHubHintClaim.model_validate(github_proof.hint_claim.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="GitHub hint claim is stale") from exc
        deletion_unverified = (
            github_proof.hint_claim.intent == "delete_candidate"
            and github_proof.target_outcome != "found"
        )
        # A signed delete hint plus a missing/forbidden read lacks the exact current version proof required for a tombstone.
        visibility_unverified = (
            github_proof.target_outcome in {"not_found", "forbidden", "partial"}
        )
        disposition: Literal["accepted_ingestion", "completed", "visibility_unverified"] = (
            "visibility_unverified"
            if deletion_unverified or visibility_unverified
            else "accepted_ingestion" if provider_records else "completed"
        )
        reconcile_next_page = (
            github_proof.next_page
            if hint_claim.intent == "reconcile" and github_proof.has_next else None
        )
        if needs_visibility_fence:
            # Lock the claim plus every pause-eligible hint before capacity; apply/ack
            # may only freshly read these prepared rows and mutate, never reacquire.
            await connectors.lock_github_hints_for_visibility_pause_in_uow(
                session, claim=hint_claim, binding_fence=github_fence,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence, source_fence=source_fence,
            )
            acknowledged = await connectors.acknowledge_github_hint_in_uow(
                session, claim=hint_claim, batch_id=batch.id, disposition=disposition,
                reconcile_next_page=reconcile_next_page,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence, source_fence=source_fence,
            )
        else:
            acknowledged = await connectors.acknowledge_github_hint(
                session, claim=hint_claim, batch_id=batch.id, disposition=disposition,
                reconcile_next_page=reconcile_next_page,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        if not acknowledged:
            raise HTTPException(status_code=409, detail="GitHub hint claim changed during acceptance")
        # A current target 404 may mean deletion or lost private-repository access; pausing fences
        # all current evidence while retained owner history remains available for review.
        if needs_visibility_fence:
            paused = await sources.pause_source_for_connector_in_uow(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
            if paused is None:
                raise HTTPException(status_code=409, detail="GitHub source changed during visibility confirmation")
            source_paused = True
            changes[0] = make_source_change(paused.id, paused.generation, paused.status, scope=scope)
    if not source_paused:
        await sources.record_collection_result_in_uow(
            session, source.id, source.generation, now, None, no_changes=not provider_records, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await commit_with_replay(session, changes, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    return NativeCollectionReceipt(
            workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision,
        batch_id=batch.id, run_id=run.id,
        status="queued" if provider_records else ("succeeded" if payload.telegram_raw_deliveries else "no_changes"),
        received_update_count=len(payload.telegram_raw_deliveries),
        record_count=len(provider_records), coverage=payload.coverage,
        cursor_after=cursor_after,
    )


async def receive_batch(
    session: AsyncSession,
    payload: ReceiveBatch,
    collector_token: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[IngestionBatch, IngestionRun]:
    """Authenticate and idempotently accept a fenced collection batch.

    Enforces active source/generation, collector token, and connector fence
    before duplicate lookup. Exact duplicate keys return the existing batch/run
    before new-work cursor/lease checks and without committing new work; native
    provider sources fail closed to the native reservation path. Differing
    payload hashes raise HTTP 409. A new batch deduplicates identical
    provider/hash/observation-time records, advances the cursor and lease, writes
    stage/outbox state, and commits with realtime changes before returning.
    """
    source, source_fence, access_fence = await _lock_source_projection(session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    if source.generation != payload.source_generation:
        raise HTTPException(status_code=409, detail="Source generation changed during collection")
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == payload.source_id,
            CollectorCredential.scope == "ingestion:write",
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    from modules.connectors import public as connectors

    if connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider collection is required")

    if not await connectors.require_batch_fence(
        session,
        source,
        payload.source_generation,
        payload.connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale or required")

    payload_hash = _digest(payload.model_dump(mode="json"))
    existing = await session.scalar(
        select(IngestionBatch).where(
            IngestionBatch.source_id == payload.source_id,
            IngestionBatch.batch_key == payload.batch_key,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="Batch key was already used with different content")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
        if run is None:
            raise RuntimeError("Ingestion batch has no run")
        return existing, run

    state = await session.get(SourceIngestionState, payload.source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=payload.source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")
    if state.cursor != payload.cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")
    if not await sources.record_collection_started_in_uow(session, payload.source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise HTTPException(status_code=409, detail="Source is not active")

    batch = IngestionBatch(
        source_id=payload.source_id, batch_key=payload.batch_key, payload_hash=payload_hash,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=payload.source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="receive", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid4(),
        type="ingestion.stage.requested",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "source_generation": source.generation,
            **({"connector_revision": payload.connector_revision} if payload.connector_revision is not None else {}),
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    # Keep every distinct provider/content observation in the accepted batch.
    seen: set[tuple[str, str, datetime]] = set()
    for record in payload.records:
        data = record.model_dump(mode="json")
        record_hash = _digest({"version": record.version, "content": record.content, "metadata": record.metadata})
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        session.add(
            SourceObservation(
                source_id=payload.source_id,
                batch_id=batch.id,
                provider_id=record.provider_id,
                record_hash=record_hash,
                payload=data,
                observed_at=record.observed_at,
                received_at=now,
                collected_at=record.collected_at,
            )
        )
    await session.flush()
    normalize_stage = await schedule_normalization(session, run, batch, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    result = await session.execute(
        update(SourceIngestionState)
        .where(
            SourceIngestionState.source_id == payload.source_id,
            SourceIngestionState.cursor.is_not_distinct_from(payload.cursor_before),
        )
        .values(
            cursor=payload.cursor_after, lease_run_id=run.id,
            collection_lease_token=None, lease_expires_at=now + COLLECTION_LEASE,
        )
    )
    if typing_cast("CursorResult[Any]", result).rowcount != 1:
        raise HTTPException(status_code=409, detail="Collection cursor changed")
    changes: list[ReplayDraft] = [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
    ]
    if normalize_stage is not None:
        changes.append(make_ingestion_change(source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status, scope=scope))
    await commit_with_replay(session, changes, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(batch)
    await session.refresh(run)
    return batch, run


async def receive_connector_batch(
    session: AsyncSession, payload: ReceiveBatch, collector_token: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> Receipt:
    """Accept a connector batch and return its public run receipt."""
    batch, run = await receive_batch(session, payload, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return Receipt(workspace_id=scope.workspace_id, batch_id=batch.id, run_id=run.id, status=run.status)


async def queue_connector_crawl(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    cursor_before: str | None,
    configuration: dict[str, object], *, scope: Scope, multi_workspace_enabled: bool,
) -> CrawlReceipt:
    """Idempotently queue a generic crawl keyed by source, cursor, config, and minute.

    A matching batch/run receipt returns without the new-work commit. New work
    persists its stage, request event, cursor lease and realtime updates in this
    function's commit; stale source, connector revision, cursor, or active-lease
    checks raise HTTP 404/409; native sources must use their provider adapter.
    """
    source, source_fence, access_fence = await _lock_source_projection(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    from modules.connectors import public as connectors
    from modules.connectors.public import CollectionFence

    if connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider collection is required")

    if not await connectors.require_collection_fence(
        session,
        source,
        CollectionFence(
            source_generation=source_generation,
            connector_revision=connector_revision,
        ),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    minute = now.replace(second=0, microsecond=0).isoformat()
    key = "crawl:" + _digest({"source_id": str(source_id), "cursor": cursor_before, "config": configuration, "minute": minute})
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == key)
    )
    if existing is not None:
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
        if run is None:
            raise RuntimeError("Crawl batch has no run")
        return CrawlReceipt(workspace_id=scope.workspace_id, run_id=run.id)
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")
    if state.cursor != cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")
    if not await sources.record_collection_started_in_uow(session, source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise HTTPException(status_code=409, detail="Source is not active")

    batch = IngestionBatch(
        source_id=source_id, batch_key=key, payload_hash=_digest(configuration),
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="collect_web", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid4(),
        type="connector.crawl.requested",
        version=1,
        occurred_at=now,
        producer="modules.connectors",
        payload={
            "source_id": str(source_id),
            "source_generation": source_generation,
            "connector_revision": connector_revision,
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "cursor_before": cursor_before,
            "configuration": configuration,
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    state.lease_run_id = run.id
    state.collection_lease_token = None
    state.lease_expires_at = now + COLLECTION_LEASE
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
    ], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return CrawlReceipt(workspace_id=scope.workspace_id, run_id=run.id)


async def receive_file(
    session: AsyncSession,
    source_id: UUID,
    document_id: UUID,
    filename: str,
    mime_type: str,
    raw_uri: str,
    size: int,
    digest: str, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence, expected_source_fence: SourceFence,
) -> tuple[IngestionRun, bool]:
    """Publish an upload only under its original principal/access/Source capture; return run/created.

    Owner-write route captures both required fences before upload I/O, releases SQL locks
    for staging, and owns discarded raw-byte cleanup/exact-session admission. Validate the
    original actor/workspace/epoch and Source identity before the first SourceSet acquisition,
    forward the original AccessFence to its admission CAS, and compare the full locked
    SourceFence before reads/effects. No current-fence fallback upgrades the capture. Commit
    the existing idempotent run (False) or new document/batch/stage/event (True); stale proof,
    inactive Source or reuse after canonical deletion fails409 before publication.
    Only a proven new-batch branch prepares Document/URI/normalized identities under these
    held fences before collection-start and Ingestion DML. Late Document initialization
    consumes the exact original tuple without earlier reentry; retries retain the unchanged
    canonical identity/run False path and acquire no new raw-URI preparation.
    """
    if (not isinstance(scope, (WorkspaceContext, InternalJobScope))
            or not isinstance(expected_access_fence, AccessFence)
            or not isinstance(expected_source_fence, SourceFence)
            or expected_access_fence.workspace_id != scope.workspace_id
            or expected_access_fence.user_id != _actor_id(scope)
            or expected_access_fence.membership_revision != scope.membership_revision
            or expected_source_fence.id != source_id
            or expected_source_fence.workspace_id != scope.workspace_id
            or isinstance(scope, InternalJobScope) and scope.source_id is not None
            and (scope.source_id != source_id or scope.source_generation != expected_source_fence.generation)):
        raise HTTPException(status_code=409, detail="Upload capture proof is invalid")
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected_access_fence)
    source = locked.fences[0]
    source_fence, access_fence = source, locked.access_fence
    if source_fence != expected_source_fence or access_fence != expected_access_fence:
        raise HTTPException(status_code=409, detail="Upload capture proof is stale")
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    batch_key = f"file:{digest}"
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == batch_key)
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise HTTPException(status_code=409, detail="Upload identity conflicts with stored content")
        if not await documents.has_document_identity(session, source_id, f"file:{digest}", scope=scope, multi_workspace_enabled=multi_workspace_enabled):
            raise HTTPException(status_code=409, detail="This file was previously ingested and its document was deleted")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
        if run is None:
            raise RuntimeError("Ingestion batch has no run")
        await session.commit()
        return run, False

    await documents.prepare_uploaded_document_in_uow(
        session, source_id=source_id, document_id=document_id, external_id=f"file:{digest}",
        raw_uri=raw_uri, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    now = datetime.now(UTC)
    if not await sources.record_collection_started_in_uow(session, source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise HTTPException(status_code=409, detail="Source is not active")
    batch = IngestionBatch(
        source_id=source_id, batch_key=batch_key, payload_hash=digest,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="parse_file", status="pending")
    session.add(stage)
    await session.flush()
    metadata = {"filename": filename, "raw_sha256": digest, "raw_size": size, "format": mime_type}
    stored_document_id = await documents.add_uploaded_document(
        session, source_id, filename[:500] or "Uploaded file", mime_type, raw_uri, metadata, f"file:{digest}", document_id,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    event = DomainEvent(
        id=uuid4(),
        type="document.file.uploaded",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={"run_id": str(run.id), "stage_id": str(stage.id), "document_id": str(stored_document_id), "raw_uri": raw_uri, "mime_type": mime_type, "source_generation": source.generation},
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
        make_knowledge_change(source_id, stored_document_id, 1, scope=scope),
    ], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return run, True


async def _read_stages(session: AsyncSession, stages: list[IngestionStage], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[StageRead]:
    """Authorize every persisted stage through its scoped run before owner count projection.

    Foreign stages fail the entire set before aggregation; materialization counts also
    compare their exact run/stage/batch/source-generation lineage. No commit occurs.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not stages:
        return []
    stage_ids = {stage.id for stage in stages}
    authorized_ids = set((await session.scalars(
        select(IngestionStage.id).join(IngestionRun, IngestionRun.id == IngestionStage.run_id)
        .where(IngestionStage.id.in_(stage_ids), *_run_scope(scope))
    )).all())
    if authorized_ids != stage_ids:
        raise HTTPException(status_code=404, detail="Ingestion stage not found")
    counts = await session.execute(
        select(
            ObservationNormalization.stage_id,
            func.sum(case((ObservationNormalization.disposition == "normalized", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "duplicate", 1), else_=0)),
            func.sum(case((ObservationNormalization.selected_current.is_(True), 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "skipped", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "failed", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "pending", 1), else_=0)),
        )
        .where(ObservationNormalization.stage_id.in_([stage.id for stage in stages]), *_materialization_scope(scope))
        .group_by(ObservationNormalization.stage_id)
    )
    by_stage = {
        row[0]: tuple(int(value or 0) for value in row[1:])
        for row in counts
    }
    return [
        StageRead(
            stage_key=stage.stage_key, status=stage.status, attempts=stage.attempts,
            error_code=stage.error_code, result_count=stage.result_count, updated_at=stage.updated_at,
            normalized_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[0],
            duplicate_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[1],
            selected_current_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[2],
            skipped_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[3],
            failed_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[4],
            pending_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[5],
        )
        for stage in stages
    ]


async def get_run(session: AsyncSession, run_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[IngestionRun, list[StageRead]] | None:
    """Return a run and its stage projection, or None when the run is absent."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id, *_run_scope(scope)))
    if run is None:
        return None
    stages = list(
        (
            await session.scalars(
                select(IngestionStage).where(IngestionStage.run_id == run_id).order_by(IngestionStage.stage_key)
            )
        ).all()
    )
    return run, await _read_stages(session, stages, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def _cursor_binding(scope: Scope, access_fence: AccessFence, kind: str, limit: int) -> str:
    """Bind a cursor to principal/configuration epoch, page kind/bounds and source restriction."""
    if (access_fence.workspace_id != scope.workspace_id or access_fence.user_id != _actor_id(scope)
            or access_fence.membership_revision != scope.membership_revision):
        raise ValueError("Cursor fence does not match its principal")
    return _digest({"workspace_id": str(scope.workspace_id), "actor_user_id": _actor_id(scope),
                    "membership_revision": scope.membership_revision,
                    "configuration_revision": access_fence.configuration_revision,
                    "source_id": str(scope.source_id) if isinstance(scope, InternalJobScope) else None,
                    "source_generation": scope.source_generation if isinstance(scope, InternalJobScope) else None,
                    "kind": kind, "limit": limit, "order": "timestamp,id"})


def _encode_history_cursor(position: tuple[datetime, UUID], binding: str) -> str:
    """Encode a scoped v2 cursor around the existing timestamp/UUID pagination primitive."""
    raw = json.dumps({"v": 2, "binding": binding, "position": encode_cursor(*position)},
                     separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str, binding: str) -> tuple[datetime, UUID]:
    """Reject unbound/foreign/stale/noncanonical cursors before applying any query offset."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or "=" in cursor:
            raise ValueError("Invalid cursor encoding")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor:
            raise ValueError("Noncanonical cursor")
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {"v", "binding", "position"}
                or type(envelope["v"]) is not int or envelope["v"] != 2
                or envelope["binding"] != binding or not isinstance(envelope["position"], str)):
            raise ValueError("Cursor binding does not match")
        return decode_cursor(envelope["position"])
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid or stale ingestion cursor") from exc


async def list_source_runs(
    session: AsyncSession,
    source_id: UUID,
    *,
    limit: int = 20,
    cursor: str | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> SourceIngestionRead | None:
    """Return detached current and bounded recent runs after source-owner existence check."""
    access_fence = await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    from modules.ingestion.schemas import SourceIngestionRead

    source = await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        return None
    if not 1 <= limit <= 100:
        raise ValueError("Ingestion history page size must be between 1 and 100")
    binding = _cursor_binding(scope, access_fence, f"history:{source.id}:{source.generation}", limit)
    statement = select(IngestionRun).where(IngestionRun.source_id == source_id, *_run_scope(scope))
    if cursor:
        created_at, identifier = _decode_history_cursor(cursor, binding)
        statement = statement.where(
            tuple_(IngestionRun.created_at, IngestionRun.id) < (created_at, identifier)
        )
    rows = list((await session.scalars(
        statement.order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc()).limit(limit + 1)
    )).all())
    page_rows = rows[:limit]
    next_cursor = (
        _encode_history_cursor((page_rows[-1].created_at, page_rows[-1].id), binding)
        if len(rows) > limit and page_rows
        else None
    )
    current = await session.scalar(
        select(IngestionRun)
        .where(*_run_scope(scope), IngestionRun.source_id == source_id, IngestionRun.status.in_(("queued", "running")))
        .order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())
        .limit(1)
    )
    run_rows = list({run.id: run for run in [*page_rows, *([current] if current else [])]}.values())
    stage_rows = list((await session.scalars(
        select(IngestionStage)
        .where(IngestionStage.run_id.in_([run.id for run in run_rows]))
        .order_by(IngestionStage.stage_key)
    )).all()) if run_rows else []
    stages_by_run: dict[UUID, list[IngestionStage]] = {run.id: [] for run in run_rows}
    for stage in stage_rows:
        stages_by_run[stage.run_id].append(stage)
    stage_reads = {
        run_id: await _read_stages(session, stages, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        for run_id, stages in stages_by_run.items()
    }

    def detach(run: IngestionRun) -> RunRead:
        """Project an ORM run and its stages into a detached response."""
        return RunRead(
            workspace_id=run.workspace_id,
            run_id=run.id,
            source_id=run.source_id,
            status=run.status,
            stages=stage_reads[run.id],
            error_code=run.error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    return SourceIngestionRead(
        workspace_id=scope.workspace_id,
        current_run=detach(current) if current is not None else None,
        items=[detach(run) for run in page_rows],
        next_cursor=next_cursor,
    )


async def retry_run(
    session: AsyncSession, run_id: UUID, requested_stage_key: str | None = None, *, scope: Scope, multi_workspace_enabled: bool,
) -> IngestionRun | None:
    """Requeue an eligible failed stage after validating source generation.

    Locks source, collection state, run, then stage and requires a durable prior event. Returns
    None when the run is absent; no-op paths return the existing run for active,
    successful, or otherwise non-retryable work. Accepted retries reset attempts/errors, copy the prior
    event payload into a fresh outbox event, refresh collection leases when
    needed, and commit. Failed normalization progress requiring correction and
    stale source generations are rejected with HTTP 409.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    run_hint = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id, *_run_scope(scope)))
    if run_hint is None:
        return None
    # Source lock serializes retry against reservation acquisition and source purge.
    locked = await sources.lock_source_set(session, (run_hint.source_id,), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = locked.fences[0]
    source_fence, access_fence = source, locked.access_fence
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    state = await session.get(SourceIngestionState, run_hint.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if state is not None and state.collection_lease_token is not None:
        if state.lease_expires_at is None or state.lease_expires_at > now:
            raise HTTPException(status_code=409, detail="Source collection is already in progress")
        state.collection_lease_token = None
        state.lease_expires_at = None
    run = await session.scalar(
        select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source.id, *_run_scope(scope)).with_for_update()
    )
    if run is None:
        return None
    stages = list((await session.scalars(
        select(IngestionStage).where(IngestionStage.run_id == run_id)
        .order_by(IngestionStage.stage_key).with_for_update()
    )).all())
    if not stages:
        raise RuntimeError("Ingestion run has no stage")
    if any(stage.status in {"pending", "queued", "running", "retrying"} for stage in stages):
        if requested_stage_key is not None:
            raise HTTPException(status_code=409, detail="Ingestion run still has an active stage")
        return run
    if all(stage.status == "succeeded" for stage in stages):
        return run
    failed = sorted(
        (stage for stage in stages if stage.status == "failed"),
        key=lambda stage: (_RUN_RETRY_ORDER.get(stage.stage_key, 100), stage.stage_key),
    )
    if not failed:
        return run
    stage = next((item for item in failed if item.stage_key == requested_stage_key), None) if requested_stage_key else failed[0]
    if stage is None:
        raise HTTPException(status_code=409, detail="Requested stage is not retryable")
    prior_event = await session.scalar(
        select(EventOutbox)
        .where(EventOutbox.payload["stage_id"].astext == str(stage.id), EventOutbox.payload["run_id"].astext == str(run.id), *_event_scope(scope))
        .order_by(EventOutbox.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if prior_event is None:
        raise HTTPException(status_code=409, detail="Failed stage has no durable retry event")
    captured_generation = prior_event.payload.get("source_generation")
    if not isinstance(captured_generation, int) or captured_generation != source.generation:
        raise HTTPException(status_code=409, detail="Failed stage belongs to an obsolete source generation")
    if stage.stage_key == "normalize":
        failed_progress = await session.scalar(
            select(ObservationNormalization.id).where(
                *_materialization_scope(scope), ObservationNormalization.stage_id == stage.id,
                ObservationNormalization.disposition == "failed",
            ).limit(1)
        )
        if failed_progress is not None:
            raise HTTPException(status_code=409, detail="Invalid observations require correction before normalization retry")
    stage.status = "pending"
    stage.attempts = 0
    stage.error_code = None
    stage.next_attempt_at = datetime.now(UTC)
    stage.lease_expires_at = None
    run.status = "queued"
    run.error_code = None
    event = DomainEvent(
        id=uuid4(),
        type=prior_event.type,
        version=1,
        occurred_at=datetime.now(UTC),
        producer="modules.ingestion",
        payload={
            **prior_event.payload,
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if stage.stage_key in {"receive", "collect_web"} and state is not None:
        now = datetime.now(UTC)
        if state.lease_run_id is not None and state.lease_expires_at is None:
            raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
        if state.lease_run_id not in (None, run.id) and state.lease_expires_at and state.lease_expires_at > now:
            raise HTTPException(status_code=409, detail="Source already has an active collection run")
        if state.lease_expires_at is not None and state.lease_expires_at > now and state.lease_run_id != run.id:
            raise HTTPException(status_code=409, detail="Source already has an active collection run")
        state.lease_run_id = run.id
        state.collection_lease_token = None
        state.lease_expires_at = now + COLLECTION_LEASE
    run.status = "queued"
    run.error_code = None
    await commit_with_replay(
        session,
        [make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return run


async def list_ready_events_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, Any] | None]]:
    """Read-only keyset page of ``document.version.ready`` outbox rows for the automations sweep.

    Ordered by ``(created_at, id)`` strictly after the caller's ``(created_at, id)`` tuple
    ``position``. The caller (the sweep) keeps that position in its workspace-keyed cursor row,
    so the position is not an opaque token: the workspace predicate comes from ``_event_scope``
    and precedes the ordering and LIMIT, which means a position taken from another workspace can
    only skip rows that belong to the caller's own scope. Owner admission precedes any query.
    Returns validated private metadata projection including the immutable version identity,
    never content. It never changes delivery status, so the single outbox consumer is unaffected.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Ready-event page size must be between 1 and 100")
    stmt = select(EventOutbox).where(EventOutbox.type == "document.version.ready", *_event_scope(scope))
    if position is not None:
        stmt = stmt.where(tuple_(EventOutbox.created_at, EventOutbox.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(EventOutbox.created_at, EventOutbox.id).limit(limit))).all()
    # A malformed row keeps its slot with a None payload so the sweep cursor still advances past it.
    return [(row.created_at, row.id, str(row.id), _ready_document_payload(row)) for row in rows]


def _ready_document_payload(event: EventOutbox) -> dict[str, Any] | None:
    """Validate finite event-type producers, canonical IDs and original epoch against the root.

    Scoped ready producers add exactly three principal fields to the prior five fields;
    missing/extra/mismatched identity is invalid. The finite 1024-byte UTF-8 bound covers
    the explicit new fields without admitting arbitrary metadata or content.
    Ingestion version-ready journals are proved at publication; News remains Documents-only.
    """
    payload: Any = event.payload
    fields = {"source_id", "document_id", "document_version_id", "source_generation", "version_number",
              "workspace_id", "actor_user_id", "membership_revision"}
    if (event.type not in _READY_EVENT_PRODUCERS or event.version != 1
            or event.producer not in _READY_EVENT_PRODUCERS[event.type]
            or not isinstance(payload, dict) or set(payload) != fields):
        return None
    uuid_fields = ("source_id", "document_id", "document_version_id", "workspace_id")
    if any(not isinstance(payload[name], str) or len(payload[name]) != 36 for name in uuid_fields):
        return None
    if any(type(payload[name]) is not int or not 1 <= payload[name] <= 2_147_483_647
           for name in ("source_generation", "version_number")):
        return None
    if any(type(payload[name]) is not int or payload[name] <= 0
           for name in ("actor_user_id", "membership_revision")):
        return None
    try:
        if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 1024:
            return None
        if any(str(UUID(payload[name])) != payload[name] for name in uuid_fields):
            return None
    except (TypeError, ValueError):
        return None
    if (payload["workspace_id"] != str(event.workspace_id)
            or payload["actor_user_id"] != event.actor_user_id
            or payload["membership_revision"] != event.membership_revision):
        return None
    return deepcopy(payload)



async def resolve_ready_event_provenance(
    session: AsyncSession, event_id: UUID, *, document_id: UUID | None = None,
    source_id: UUID | None = None, accepted_version_ids: tuple[UUID, ...] = (),
    allow_retained_receipt: bool = False, scope: Scope, multi_workspace_enabled: bool,
) -> ReadyDocumentProvenance | None:
    """Resolve one strict historical event against a live Documents locator or detached cleanup receipt.

    Detached version IDs are accepted only when the caller supplies both receipt-owned document and
    source IDs. The bounded receipt path remains usable after canonical rows have cascaded away.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(accepted_version_ids) > 100:
        raise ValueError("Ready-event receipt membership exceeds its bound")
    if (document_id is None) != (source_id is None):
        raise ValueError("Ready-event receipt matching requires both document and source IDs")
    event = await session.scalar(select(EventOutbox).where(EventOutbox.id == event_id, *_event_scope(scope)))
    if event is None:
        return None
    payload = _ready_document_payload(event)
    if payload is None:
        return None
    event_source = UUID(payload["source_id"])
    event_document = UUID(payload["document_id"])
    version_id = UUID(payload["document_version_id"])
    if document_id is not None:
        if (
            event_document != document_id or event_source != source_id
            or version_id not in accepted_version_ids
        ):
            return None
    locator = await documents.review_version_locator(
        session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if locator is None and (allow_retained_receipt or document_id is not None):
        # Retained cleanup lookup filters receipt workspace and exact source/version before LIMIT;
        # supplied version membership alone cannot authorize a foreign receipt.
        retained_document = await documents.cleanup_evidence_version_document(
            session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            source_id=event_source,
        )
        if retained_document != event_document:
            return None
    elif locator is None or locator != (event_document, event_source):
        return None
    return ReadyDocumentProvenance(
        workspace_id=event.workspace_id, actor_user_id=event.actor_user_id, membership_revision=event.membership_revision,
        event_id=event.id, source_id=event_source, document_id=event_document,
        document_version_id=version_id, source_generation=payload["source_generation"],
        version_number=payload["version_number"],
    )


async def list_terminal_runs_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, str | int]]]:
    """Read-only keyset page of ingestion runs in a terminal state for connector sync results.

    Ordered by ``(updated_at, id)`` after the caller's ``(updated_at, id)`` tuple ``position``;
    the key combines run id and status so a later status change is a new event. The workspace
    predicate (``_run_scope``) precedes ordering and LIMIT, owner admission precedes any query, and
    the tuple position is the sweep's own workspace-keyed cursor, never an opaque token.
    Payload carries original principal/source identity, status and the observation count.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Terminal-run page size must be between 1 and 100")
    stmt = select(IngestionRun).join(IngestionBatch, and_(
        IngestionBatch.id == IngestionRun.batch_id, IngestionBatch.source_id == IngestionRun.source_id,
    )).where(IngestionRun.status.in_(("succeeded", "failed", "needs_ocr")),
             IngestionBatch.source_generation >= 1, *_run_scope(scope))
    if position is not None:
        stmt = stmt.where(tuple_(IngestionRun.updated_at, IngestionRun.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(IngestionRun.updated_at, IngestionRun.id).limit(limit))).all()
    # new_items = observations collected in the run's batch (one grouped count for the page).
    counts = dict((await session.execute(
        select(SourceObservation.batch_id, func.count()).join(IngestionRun, and_(
            IngestionRun.batch_id == SourceObservation.batch_id,
            IngestionRun.source_id == SourceObservation.source_id,
        )).where(SourceObservation.batch_id.in_([r.batch_id for r in rows]), *_run_scope(scope))
        .group_by(SourceObservation.batch_id)
    )).all()) if rows else {}
    generations = dict((await session.execute(
        select(IngestionBatch.id, IngestionBatch.source_generation).join(IngestionRun, and_(
            IngestionRun.batch_id == IngestionBatch.id, IngestionRun.source_id == IngestionBatch.source_id,
        )).where(IngestionRun.id.in_([r.id for r in rows]), *_run_scope(scope))
    )).all()) if rows else {}
    return [(r.updated_at, r.id, f"{r.id}:{r.status}",
             {"source_id": str(r.source_id), "source_generation": generations[r.batch_id],
              "workspace_id": str(r.workspace_id), "actor_user_id": r.actor_user_id,
              "membership_revision": r.membership_revision,
              "status": r.status, "new_items": int(counts.get(r.batch_id, 0))})
            for r in rows]


async def list_run_meta(session: AsyncSession, limit: int, *, instance_operator: bool) -> list[_RunMeta]:
    """Return <=100 newest run metadata under real internal instance-operator admission.

    True comes from bootstrap/operator authorization, never client input or workspace
    owner role. This deliberate instance aggregate carries no content.
    """
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    if limit < 1:
        raise ValueError("Run metadata page size must be positive")
    rows = await session.scalars(select(IngestionRun).order_by(IngestionRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="ingestion", id=str(r.id), status=r.status, error_code=r.error_code,
                     created_at=r.created_at, updated_at=r.updated_at,
                     finished_at=r.updated_at if r.status in {"succeeded", "failed", "needs_ocr"} else None)
            for r in rows]


async def observability_quality_summary(session: AsyncSession, *, instance_operator: bool) -> dict[str, int | float]:
    """Return content-free instance aggregates under real operator admission, never workspace-owner authority."""
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    normalized, duplicates = (await session.execute(select(
        func.count().filter(ObservationNormalization.disposition.in_(("normalized", "duplicate"))),
        func.count().filter(ObservationNormalization.disposition == "duplicate"),
    ))).one()
    failed_runs = int(await session.scalar(select(func.count()).select_from(IngestionRun).where(
        IngestionRun.status == "failed"
    )) or 0)
    return {"duplicate_rate": float(duplicates or 0) / int(normalized or 1), "failed_ingestion": failed_runs}


async def observability_queue_summary(session: AsyncSession, *, instance_operator: bool, now: datetime | None = None) -> dict[str, object]:
    """Return instance queue aggregates under real operator admission and current lifecycle metadata.

    Source supplies a narrow operator-only lifecycle projection. Instance True comes
    from operator admission; no credentials/config/content cross this boundary.
    """
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    now = now or datetime.now(UTC)
    source_lifecycle = sources.ingestion_instance_lifecycle_projection(instance_operator=instance_operator).subquery("source_lifecycle")
    stage_counts = dict((await session.execute(
        select(IngestionStage.status, func.count()).group_by(IngestionStage.status)
    )).all())
    active_stage = aliased(IngestionStage)
    active_stage_exists = select(1).select_from(active_stage).where(
        active_stage.run_id == IngestionStage.run_id,
        active_stage.status.in_(("pending", "queued", "running", "retrying")),
    ).exists()
    failed_progress_exists = select(1).where(
        ObservationNormalization.stage_id == IngestionStage.id,
        ObservationNormalization.disposition == "failed",
    ).exists()
    latest_event = aliased(EventOutbox)
    latest_event_id = (select(latest_event.id).where(
        latest_event.payload["stage_id"].astext == cast(IngestionStage.id, String),
    ).order_by(latest_event.created_at.desc()).limit(1).correlate(IngestionStage).scalar_subquery())
    latest_event_matches = select(1).select_from(EventOutbox).where(
        EventOutbox.id == latest_event_id,
        EventOutbox.payload["source_generation"].astext == cast(source_lifecycle.c.generation, String),
    ).exists()
    active_collection_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.collection_lease_token.is_not(None),
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    other_run_lease_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.lease_run_id.is_not(None),
        SourceIngestionState.lease_run_id != IngestionRun.id,
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    failed_candidate = aliased(IngestionStage)
    retry_order = case(*((IngestionStage.stage_key == key, rank)
                         for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    candidate_order = case(*((failed_candidate.stage_key == key, rank)
                             for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    earlier_failed_exists = select(1).select_from(failed_candidate).where(
        failed_candidate.run_id == IngestionStage.run_id,
        failed_candidate.status == "failed",
        or_(candidate_order < retry_order,
            and_(candidate_order == retry_order, failed_candidate.stage_key < IngestionStage.stage_key)),
    ).exists()
    retryable = int(await session.scalar(
        select(func.count()).select_from(IngestionStage)
        .join(IngestionRun, IngestionRun.id == IngestionStage.run_id)
        .join(source_lifecycle, source_lifecycle.c.id == IngestionRun.source_id)
        .where(
            IngestionStage.status == "failed", source_lifecycle.c.status == "active",
            not_(active_stage_exists), not_(earlier_failed_exists), latest_event_matches,
            or_(IngestionStage.stage_key != "normalize", not_(failed_progress_exists)),
            not_(active_collection_exists),
            or_(IngestionStage.stage_key.not_in(("receive", "collect_web")), not_(other_run_lease_exists)),
        )
    ) or 0)
    event_delivery = dict((await session.execute(
        select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
    )).all())
    return {"ingestion_stages": stage_counts, "retryable_ingestion_stages": retryable,
            "event_delivery": event_delivery}


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID, *, instance_operator: bool) -> _RunMeta | None:
    """Return one instance run metadata under explicit real operator admission; no content or scope borrowing."""
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    row = await session.get(IngestionRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="ingestion", id=str(row.id), status=row.status, error_code=row.error_code,
                    created_at=row.created_at, updated_at=row.updated_at,
                    finished_at=row.updated_at if row.status in {"succeeded", "failed", "needs_ocr"} else None)
