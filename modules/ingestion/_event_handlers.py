"""Outbox event handlers and scope runtime supplied to ``core.events`` through module descriptors (internal).

Core owns the outbox mechanics; the type-specific rules live here because they depend on Ingestion lineage
(runs/stages/journals) and on the Documents/Sources retained receipts Ingestion already reads. Descriptors
reference these classes lazily through ``modules.ingestion.public`` (``"...public:IngestionStageEventHandler"``).
Behaviour is identical to the former ``publish_event`` branch chain and dispatcher envelope/claim checks.
"""

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.events.model import EventOutbox
from core.events.registry import EventHandler, PublishContext
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.ingestion._event_publisher import (
    _scope_run_event,
    _verify_cleanup_event,
    _verify_purge_event,
    _verify_ready_event,
)
from modules.ingestion._scope import (
    _actor_id,
    _admit_ingestion_scope,
    resolve_ingestion_event_scope,
)
from modules.ingestion.models import IngestionRun, IngestionStage
from modules.ingestion.schemas import EventDelivery
from modules.knowledge.documents import public as documents
from modules.sources import public as sources


class IngestionOutboxRuntime:
    """Scope/admission services Ingestion provides to the core outbox (declared by its descriptor)."""

    async def admit(self, session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> AccessFence:
        """Read current owner admission without locks (owner-role WorkspaceContext or InternalJobScope)."""
        return await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)

    def actor_id(self, scope: Scope) -> int:
        """Return the validated principal id without granting resource authority."""
        return _actor_id(scope)

    async def resolve_scope(
        self, session: AsyncSession, event_id: UUID, *, multi_workspace_enabled: bool,
    ) -> InternalJobScope | None:
        """Resolve a retained outbox principal before domain locks; ``None`` when corrupt or missing."""
        return await resolve_ingestion_event_scope(
            session, event_id, multi_workspace_enabled=multi_workspace_enabled)


OUTBOX_RUNTIME = IngestionOutboxRuntime()


def _principal(scope: InternalJobScope) -> dict[str, Any]:
    """Return the exact principal/Source fields every non-cleanup payload must echo."""
    return {"workspace_id": str(scope.workspace_id), "actor_user_id": scope.actor_user_id,
            "membership_revision": scope.membership_revision,
            "source_id": str(scope.source_id), "source_generation": scope.source_generation}


class _PrincipalEnvelopeHandler(EventHandler):
    """Shared envelope proof: retained principal columns and payload echo, then per-type canonical UUIDs."""

    def _uuid_fields(self, event: EventOutbox | EventDelivery, payload: dict[str, Any],
                     principal: dict[str, Any]) -> list[str] | None:
        """Return the payload fields that must be canonical UUID strings, or ``None`` for an invalid shape."""
        raise NotImplementedError

    def valid_envelope(self, event: EventOutbox | EventDelivery, scope: InternalJobScope) -> bool:
        """Validate exact retained principal and per-type shape before worker content use.

        No coercion, metadata promotion, default actor or current membership upgrade occurs.
        """
        payload = event.payload
        principal = _principal(scope)
        if (event.workspace_id != scope.workspace_id or event.actor_user_id != scope.actor_user_id
                or event.membership_revision != scope.membership_revision or scope.source_id is None):
            return False
        if any(type(payload.get(key)) is not type(value) or payload.get(key) != value
               for key, value in principal.items()):
            return False
        uuid_fields = self._uuid_fields(event, payload, principal)
        if uuid_fields is None:
            return False
        try:
            return all(isinstance(payload.get(key), str) and str(UUID(payload[key])) == payload[key]
                       for key in uuid_fields)
        except (ValueError, TypeError, AttributeError):
            return False


class IngestionStageEventHandler(_PrincipalEnvelopeHandler):
    """Run/stage-bound events: ``document.file.uploaded``, ``ingestion.stage.requested``,
    ``connector.crawl.requested`` and ``ingestion.normalize.requested``."""

    async def verify_publish(self, session: AsyncSession, ctx: PublishContext) -> None:
        """Bind the payload to its scoped run/batch/stage lineage and stamp the principal identity."""
        await _scope_run_event(session, ctx.payload, ctx.identity, scope=ctx.scope)

    def _uuid_fields(self, event: EventOutbox | EventDelivery, payload: dict[str, Any],
                     principal: dict[str, Any]) -> list[str] | None:
        """Name run/stage (plus document for uploads); normalize and crawl add bounded integer checks."""
        uuid_fields = ["run_id", "stage_id"]
        if event.type == "document.file.uploaded":
            uuid_fields.append("document_id")
        if event.type == "ingestion.normalize.requested" and (
            type(payload.get("normalization_version")) is not int or payload["normalization_version"] != 1
        ):
            return None
        if event.type == "connector.crawl.requested" and (
            type(payload.get("connector_revision")) is not int or payload["connector_revision"] < 1
        ):
            return None
        return uuid_fields

    async def validate_dispatch(
        self, session: AsyncSession, event: EventOutbox, scope: InternalJobScope, *, now: datetime, enabled: bool,
    ) -> bool:
        """Fail an event whose stage or generation-bound Source is gone; skip one whose running stage still holds an unexpired lease.

        A missing stage fails the event (committed here); an in-flight running stage with an unexpired lease
        is skipped to preserve the worker's original timestamp.
        """
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
                return False
            # A Source whose generation advanced (pause/archive/purge/edit/config bump) can never satisfy the
            # generation-bound scope again; fail the event here or the worker 404s and redispatch loops forever.
            if await sources.get_source_fence(
                session, cast(UUID, scope.source_id), scope=scope, multi_workspace_enabled=enabled,
            ) is None:
                event.status = "failed"
                await session.commit()
                return False
            # Preserve an in-flight worker's original timestamp until its stage lease expires.
            if stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now:
                return False
        return True


class DocumentReadyEventHandler(_PrincipalEnvelopeHandler):
    """``document.version.ready`` / ``news.document.ready``: finite 1024-byte producer contracts."""

    async def verify_publish(self, session: AsyncSession, ctx: PublishContext) -> None:
        """Stamp the principal identity (ready payloads carry no run/stage)."""
        await _scope_run_event(session, ctx.payload, ctx.identity, scope=ctx.scope)

    async def verify_outbox(self, session: AsyncSession, ctx: PublishContext, outbox: EventOutbox) -> None:
        """Prove the payload shape and the Documents/Ingestion-journal lineage of the ready version."""
        await _verify_ready_event(
            session, ctx.event, outbox, ctx.payload, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled)

    def _uuid_fields(self, event: EventOutbox | EventDelivery, payload: dict[str, Any],
                     principal: dict[str, Any]) -> list[str] | None:
        """Require exactly the principal plus three document fields, bounded integers and a 1024-byte body."""
        if set(payload) != set(principal) | {"document_id", "document_version_id", "version_number"}:
            return None
        if (type(payload.get("version_number")) is not int or not 1 <= payload["version_number"] <= 2_147_483_647
                or not 1 <= payload["source_generation"] <= 2_147_483_647):
            return None
        if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 1024:
            return None
        return ["document_id", "document_version_id"]


class SourcePurgeEventHandler(_PrincipalEnvelopeHandler):
    """``source.purge.requested`` / ``.progressed`` / ``.coverage`` against the retained Sources receipt."""

    async def verify_publish(self, session: AsyncSession, ctx: PublishContext) -> None:
        """Prove the exact producer contract and retained Source purge receipt scope."""
        await _verify_purge_event(
            session, ctx.event, ctx.payload, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled)

    def _uuid_fields(self, event: EventOutbox | EventDelivery, payload: dict[str, Any],
                     principal: dict[str, Any]) -> list[str] | None:
        """Purge events carry exactly the principal plus ``operation_id``."""
        return ["operation_id"] if set(payload) == set(principal) | {"operation_id"} else None

    async def validate_dispatch(
        self, session: AsyncSession, event: EventOutbox, scope: InternalJobScope, *, now: datetime, enabled: bool,
    ) -> bool:
        """Fail the event unless the retained Sources receipt scope equals the stored scope exactly."""
        retained = await sources.read_source_purge_job_identity(
            session, UUID(event.payload["operation_id"]), scope=scope, multi_workspace_enabled=enabled,
        )
        if retained != scope:
            event.status = "failed"
            await session.commit()
            return False
        return True


def _valid_cleanup_envelope(
    event: EventOutbox | EventDelivery, scope: InternalJobScope, payload: dict[str, Any],
) -> bool:
    """Validate the operation-only ``document.cleanup.requested`` envelope.

    Principal is proven on the outbox columns (and by the receipt owner), never by payload keys; legacy
    identity-in-payload shapes are rejected, and the event id must be the deterministic id of its operation.
    """
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


class DocumentCleanupEventHandler(EventHandler):
    """``document.cleanup.requested``: operation-only payload proven by the retained Documents receipt."""

    async def verify_publish(self, session: AsyncSession, ctx: PublishContext) -> None:
        """Prove the event against its retained Documents cleanup receipt (404 otherwise)."""
        await _verify_cleanup_event(
            session, ctx.event, ctx.payload, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled)

    def valid_envelope(self, event: EventOutbox | EventDelivery, scope: InternalJobScope) -> bool:
        """Validate the operation-only envelope with the deterministic event id."""
        return _valid_cleanup_envelope(event, scope, event.payload)

    async def validate_dispatch(
        self, session: AsyncSession, event: EventOutbox, scope: InternalJobScope, *, now: datetime, enabled: bool,
    ) -> bool:
        """Fail the event unless the Documents receipt matches the stored Source and generation."""
        receipt = await documents.read_document_cleanup_job_identity(
            session, UUID(event.payload["operation_id"]), scope=scope, multi_workspace_enabled=enabled,
        )
        if receipt is None or (receipt.source_id, receipt.source_generation) != (
            scope.source_id, scope.source_generation,
        ):
            event.status = "failed"
            await session.commit()
            return False
        return True


__all__ = [
    "OUTBOX_RUNTIME", "DocumentCleanupEventHandler", "DocumentReadyEventHandler", "IngestionOutboxRuntime",
    "IngestionStageEventHandler", "SourcePurgeEventHandler", "_valid_cleanup_envelope",
]
