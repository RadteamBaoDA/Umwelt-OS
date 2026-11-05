"""Scope/admission helpers shared by every ingestion submodule (internal; import through ``public``)."""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from core.workspaces import public as workspaces
from core.workspaces.admission import actor_id
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.ingestion.models import (
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
)
from modules.ingestion.schemas import (
    ConnectorCollectionLease,
)
from modules.knowledge.documents import public as documents
from modules.sources import public as sources
from modules.sources.public import ConnectorSource

_SOURCE_PURGE_PRODUCERS = {
    "source.purge.requested": "modules.sources",
    "source.purge.coverage": "modules.sources",
    "source.purge.progressed": "modules.knowledge.documents",
}


_actor_id = actor_id  # canonical helper; private name kept for sibling ingestion modules


async def _admit_ingestion_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> AccessFence:
    """Read current owner admission without locks; safe under caller-held earlier locks.

    Members cannot inspect ingestion metadata. Publication/mutation callers must already
    hold ordered admission/Source locks or use a locking entrypoint before domain locks.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise UnauthorizedError(detail="Authentication required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")
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
        raise NotFoundError(detail="Collection reservation not found")


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
        raise ConflictError(detail="Original collection lease required")
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


async def _resolve_document_cleanup_event_scope(
    session: AsyncSession, event_id: UUID, row: Any, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve an operation-only Documents cleanup event through its retained receipt authority.

    The outbox principal columns must equal the receipt's captured tuple and the payload must be
    exactly the operation id with the deterministic event id; anything else (legacy
    identity-in-payload, extra keys, NULL receipt epochs, stale configuration) is None.
    """
    if row.operation_type != "string" or row.version != 1 or row.producer != "modules.knowledge.documents":
        return None
    try:
        operation_id = UUID(row.payload_operation)
    except (TypeError, ValueError):
        return None
    if str(operation_id) != row.payload_operation or event_id != uuid5(operation_id, "document-cleanup-requested"):
        return None
    exact = await session.scalar(select(EventOutbox.id).where(
        EventOutbox.id == event_id, EventOutbox.payload == {"operation_id": str(operation_id)},
    ))
    if exact is None:
        return None
    identity = await documents.resolve_document_cleanup_job_identity(
        session, operation_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if identity is None or (identity.workspace_id, identity.actor_user_id, identity.membership_revision) != (
        row.workspace_id, row.actor_user_id, row.membership_revision,
    ):
        return None
    return InternalJobScope(
        workspace_id=identity.workspace_id, actor_user_id=identity.actor_user_id,
        membership_revision=identity.membership_revision, source_id=identity.source_id,
        source_generation=identity.source_generation,
    )


async def resolve_ingestion_event_scope(
    session: AsyncSession, event_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve a retained outbox principal before domain locks, then compare its exact reread.

    Corrupt/missing scope is unresolved, never an owner/default upgrade. The caller owns
    quarantine and transaction release; Source/claim/lease checks remain owner-specific.
    """
    cleanup_row = (await session.execute(select(
        EventOutbox.workspace_id, EventOutbox.actor_user_id, EventOutbox.membership_revision,
        EventOutbox.version, EventOutbox.producer,
        func.left(EventOutbox.payload["operation_id"].astext, 37).label("payload_operation"),
        func.jsonb_typeof(EventOutbox.payload["operation_id"]).label("operation_type"),
    ).where(EventOutbox.id == event_id, EventOutbox.type == "document.cleanup.requested"))).one_or_none()
    if cleanup_row is not None:
        return await _resolve_document_cleanup_event_scope(
            session, event_id, cleanup_row, multi_workspace_enabled=multi_workspace_enabled,
        )
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


def _digest(value: object) -> str:
    """Hash canonical JSON so equivalent payload mappings share an identity."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cleanup_event_scope(event_id: UUID, operation_id: UUID, scope: Scope) -> tuple[Any, ...]:
    """Principal-column plus exact operation-only payload predicates for one cleanup event.

    The generic event scope adds payload Source predicates for Source-bound scopes, which an
    operation-only cleanup payload can never satisfy; the receipt owner proves Source lineage.
    """
    return (EventOutbox.id == event_id, EventOutbox.type == "document.cleanup.requested",
            EventOutbox.workspace_id == scope.workspace_id,
            EventOutbox.actor_user_id == _actor_id(scope),
            EventOutbox.membership_revision == scope.membership_revision,
            EventOutbox.payload == {"operation_id": str(operation_id)})
