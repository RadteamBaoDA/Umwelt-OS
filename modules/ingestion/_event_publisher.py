"""Domain-event outbox publication seam (internal; import through ``public``).

``publish_event`` is the single stable entry point for writing ``EventOutbox`` rows and is
deliberately isolated here so a later wave can relocate it to ``core`` without touching callers."""

import json
from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cron import notify_on_commit
from core.errors import NotFoundError
from core.events import DomainEvent
from core.workspaces.schemas import InternalJobScope, Scope
from modules.ingestion._scope import (
    _SOURCE_PURGE_PRODUCERS,
    _actor_id,
    _admit_ingestion_scope,
    _document_cleanup_operation_id,
    _materialization_scope,
    _run_scope,
    _source_purge_event_subject,
)
from modules.ingestion.models import (
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceObservation,
)
from modules.knowledge.documents import public as documents
from modules.sources import public as sources

_READY_EVENT_PRODUCERS = {
    "document.version.ready": {"modules.ingestion", "modules.knowledge.documents"},
    "news.document.ready": {"modules.ingestion", "modules.knowledge.documents"},
}


async def _verify_cleanup_event(
    session: AsyncSession, event: DomainEvent, payload: dict[str, Any], *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Prove a ``document.cleanup.requested`` event against its retained Documents receipt (404 otherwise).

    The operation-only payload is preserved: principal columns come from ``scope``. The receipt must match the
    workspace, actor, membership epoch and, for a Source-bound job scope, the exact Source and generation.
    """
    operation_id = _document_cleanup_operation_id(event.id, event.version, event.producer, payload)
    receipt = await documents.read_document_cleanup_job_identity(
        session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        receipt is None or receipt.workspace_id != scope.workspace_id
        or receipt.actor_user_id != _actor_id(scope)
        or receipt.membership_revision != scope.membership_revision
        or isinstance(scope, InternalJobScope) and scope.source_id is not None
        and (receipt.source_id != scope.source_id or receipt.source_generation != scope.source_generation)
    ):
        raise NotFoundError(detail="Document cleanup receipt not found")


async def _verify_purge_event(
    session: AsyncSession, event: DomainEvent, payload: dict[str, Any], *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Prove a Source purge event's exact producer contract and retained Sources receipt scope.

    ``ValueError`` for a missing producer identity, 404 when the retained receipt scope differs.
    """
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
        raise NotFoundError(detail="Source purge receipt not found")


async def _scope_run_event(
    session: AsyncSession, payload: dict[str, Any], identity: dict[str, Any], *, scope: Scope,
) -> None:
    """Bind a run/stage event to its scoped lineage and stamp the principal identity into ``payload`` (in place).

    A payload naming a run must resolve it (scoped), its batch generation, optional Source and stage, else 404;
    malformed ids are ``ValueError``. A Source-bound job scope must see exactly its own Source and generation.
    """
    if "run_id" in payload:
        try:
            run_id = UUID(payload["run_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Event run identity is invalid") from exc
        run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id,
                                                              *_run_scope(scope)))
        if run is None:
            raise NotFoundError(detail="Event run not found")
        batch = await session.scalar(select(IngestionBatch).where(
            IngestionBatch.id == run.batch_id, IngestionBatch.source_id == run.source_id,
        ))
        if batch is None or batch.source_generation != payload.get("source_generation"):
            raise NotFoundError(detail="Event batch lineage not found")
        if "source_id" in payload and payload["source_id"] != str(run.source_id):
            raise NotFoundError(detail="Event source not found")
        payload["source_id"] = str(run.source_id)
        if "stage_id" in payload:
            try:
                stage_id = UUID(payload["stage_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Event stage identity is invalid") from exc
            if await session.scalar(select(IngestionStage.id).where(
                IngestionStage.id == stage_id, IngestionStage.run_id == run.id,
            )) is None:
                raise NotFoundError(detail="Event stage not found")
    if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
        payload.get("source_id") != str(scope.source_id)
        or payload.get("source_generation") != scope.source_generation
    ):
        raise NotFoundError(detail="Event Source identity not found")
    payload.update(identity)


async def _verify_ready_event(
    session: AsyncSession, event: DomainEvent, outbox: EventOutbox, payload: dict[str, Any], *, scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Prove a ``document.version.ready``/``news.document.ready`` payload and its document lineage.

    The finite producer payload must validate, Documents must locate the version under the exact document and
    Source, and an Ingestion-produced event must additionally link to an already-written ``normalized`` journal
    row with chunks through the admitted run/stage/batch/observation (404 otherwise).
    """
    if _ready_document_payload(outbox) is None:
        raise ValueError("Ready event requires exact scoped producer payload")
    locator = await documents.review_version_locator(
        session, UUID(payload["document_version_id"]), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if locator != (UUID(payload["document_id"]), UUID(payload["source_id"])):
        raise NotFoundError(detail="Ready document lineage not found")
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
            raise NotFoundError(detail="Ready normalization lineage not found")


async def publish_event(session: AsyncSession, event: DomainEvent, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Add scoped durable identity to the caller's already-admitted transaction outbox.

    Caller holds admission and all required domain locks; only nonlocking owner reads
    occur here. Exact retained purge identity survives canonical deletion. Existing
    claimed scope fields must match, never be silently overwritten. No commit/I/O.
    Purge event types have exact Source/Documents producer contracts; normalized Ingestion
    version-ready publication additionally proves its scoped journal/run/batch lineage.

    This is the single stable outbox-publication seam of the module: its signature and behaviour are the
    contract a later wave can relocate to ``core`` without touching callers (66 transitive dependents).
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    payload = deepcopy(event.payload)
    identity = {"workspace_id": str(scope.workspace_id), "actor_user_id": _actor_id(scope),
                "membership_revision": scope.membership_revision}
    for key, value in identity.items():
        if key in payload and (type(payload[key]) is not type(value) or payload[key] != value):
            raise NotFoundError(detail="Event identity not found")
    if event.type == "document.cleanup.requested":
        await _verify_cleanup_event(
            session, event, payload, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    elif event.type in _SOURCE_PURGE_PRODUCERS:
        await _verify_purge_event(
            session, event, payload, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    else:
        await _scope_run_event(session, payload, identity, scope=scope)
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
        await _verify_ready_event(
            session, event, outbox, payload, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    session.add(outbox)
    # Event-driven dispatch: once this transaction commits, wake the dispatcher poller instead of
    # waiting for its slow safety-net tick. No-op when no process hook is installed.
    notify_on_commit(session, "dispatch_pending_work")
    if event.type == "document.version.ready":
        notify_on_commit(session, "index_pending_chunks")  # chunks exist now: index without waiting for the minute tick


NORMALIZATION_VERSION = 1


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
