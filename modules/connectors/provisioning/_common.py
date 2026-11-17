"""Shared row readers, operation matchers and admission errors for connector provisioning."""

import json
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.connectors.backends import CURRENT_TEMPLATE_REVISION
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.public import (
    _connector_access,
    _connector_actor,
    _read_scoped_source,
)
from modules.sources import public as sources
from modules.sources.public import SourceFence

_ALL_CREDENTIAL_SLOTS = ("collector", "manual_trigger", "provider")


def _bind_n8n_service(
    row: ConnectorProvisioning | ConnectorManagedCredential,
    service_url: str,
    *,
    has_liability: bool,
) -> None:
    """Bind a first dispatch to its endpoint under existing owner/Source/row locks.

    Never reinterpret a legacy or differently hosted ID using current settings. A row
    with no outstanding remote liability may start a new binding; caller commits this
    field in the same transaction as the dispatch barrier before any provider request.
    """
    from modules.connectors.n8n import n8n_service_identity

    endpoint = n8n_service_identity(service_url)
    previous = row.n8n_service_url
    if has_liability and (previous is None or n8n_service_identity(previous) != endpoint):
        raise RuntimeError("n8n endpoint lineage requires operator reconciliation")
    row.n8n_service_url = endpoint


class RetainedEffectAdmissionDenied(DomainError):
    """Identify definitive original admission denial at the retained Source boundary.

    Trusted drivers release the failed transaction before journal-only bookkeeping;
    database/programming/missing-operation failures never become this exception.
    journal_disposition is internal bookkeeping status, never an HTTP projection or
    evidence that remote cleanup completed. Normal permission-loss detail stays unchanged.
    """

    journal_disposition: Literal["stored", "duplicate", "conflict", "missing"] | None = None


async def _read_connector_rows(
    session: AsyncSession,
    source_id: UUID,
    slots: tuple[str, ...] = (),
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Freshly read owned rows without locks after scoped owner/Source admission."""
    source = await _read_scoped_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None:
        return None, None, {}
    fence = await sources.get_source_fence(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .execution_options(populate_existing=True)
    )
    credentials = (
        list(
            await session.scalars(
                select(ConnectorManagedCredential)
                .where(
                    ConnectorManagedCredential.source_id == source_id,
                    ConnectorManagedCredential.slot.in_(sorted(set(slots))),
                )
                .order_by(ConnectorManagedCredential.slot)
                .execution_options(populate_existing=True)
            )
        )
        if slots
        else []
    )
    return fence, row, {credential.slot: credential for credential in credentials}


async def _lock_connector_rows(
    session: AsyncSession,
    source_id: UUID,
    slots: tuple[str, ...] = (),
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Lock only provisioning then sorted slots under already-held access and Source locks.

    Callers acquired admission/Source before entering and retain them through final commit.
    This is an owner-private continuation, not permission to skip upstream admission.
    """
    source = await _read_scoped_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None:
        return None, None, {}
    fence = await sources.get_source_fence(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    credentials = (
        list(
            await session.scalars(
                select(ConnectorManagedCredential)
                .where(
                    ConnectorManagedCredential.source_id == source_id,
                    ConnectorManagedCredential.slot.in_(sorted(set(slots))),
                )
                .order_by(ConnectorManagedCredential.slot)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if slots
        else []
    )
    return fence, row, {credential.slot: credential for credential in credentials}


def _operation_identity(scope: Scope, access_fence: AccessFence) -> dict[str, object]:
    """Capture the admitted principal/config epoch in durable operation JSON without secrets."""
    return {
        "workspace_id": str(scope.workspace_id),
        "actor_user_id": _connector_actor(scope),
        "membership_revision": scope.membership_revision,
        "workspace_configuration_revision": access_fence.configuration_revision,
    }


async def _operation_matches(
    session: AsyncSession,
    envelope: object,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Reject unbound/stale principals and revoked-effect journals for ordinary execution.

    Journal disposition never regains send/publication authority after regrant/re-enable.
    Original scoped access/config admission is checked without upstream lock acquisition.
    """
    fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return (
        isinstance(envelope, dict)
        and not envelope.get("retained_effect_result")
        and all(
            type(envelope.get(key)) is type(value) and envelope.get(key) == value
            for key, value in _operation_identity(scope, fence).items()
        )
    )


def _retained_operation_matches(
    current: object,
    original_operation: dict[str, object],
    *,
    source_id: UUID,
    scope: Scope,
    access_fence: AccessFence,
) -> bool:
    """Compare original durable principal/config/operation/step/target without renewal.

    Lifecycle may only mark cleanup_required/error while the exact in-flight envelope
    remains retained. All other keys, including dispatch state/time, request, ciphertext,
    remote target, Source generation and revision, must match the captured snapshot.
    This pure CAS proves lineage only; Source owner separately admits original access.
    """
    if not isinstance(current, dict) or not isinstance(original_operation, dict):
        return False
    if (
        access_fence.workspace_id != scope.workspace_id
        or access_fence.user_id != _connector_actor(scope)
        or access_fence.membership_revision != scope.membership_revision
    ):
        return False
    if (
        isinstance(scope, InternalJobScope)
        and scope.source_id is not None
        and (
            scope.source_id != source_id
            or scope.source_generation != original_operation.get("source_generation")
        )
    ):
        return False
    identity = _operation_identity(scope, access_fence)
    if any(
        type(current.get(key)) is not type(value)
        or current.get(key) != value
        or type(original_operation.get(key)) is not type(value)
        or original_operation.get(key) != value
        for key, value in identity.items()
    ):
        return False
    if (
        type(original_operation.get("source_generation")) is not int
        or cast(int, original_operation["source_generation"]) <= 0
        or type(original_operation.get("revision")) is not int
        or cast(int, original_operation["revision"]) <= 0
        or not isinstance(original_operation.get("id"), str)
    ):
        return False
    step = original_operation.get("step")
    if isinstance(step, dict):
        if (
            original_operation.get("kind") not in {"enable", "deactivate"}
            or step.get("kind") not in {"lookup", "create", "update", "activate", "deactivate"}
            or original_operation.get("kind") == "deactivate"
            and step.get("kind") != "deactivate"
        ):
            return False
    elif original_operation.get("kind") not in {"create", "update", "delete"}:
        return False
    # JSON preserves nested value types and absent-vs-null fields in the exact request.
    excluded = {"cleanup_required", "error", "retained_effect_result"}
    try:
        return json.dumps(
            {key: value for key, value in current.items() if key not in excluded}, sort_keys=True
        ) == json.dumps(
            {key: value for key, value in original_operation.items() if key not in excluded},
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


async def _read_retained_connector_rows(
    session: AsyncSession,
    source_id: UUID,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Reread exact effect rows without locks under held original admission/Source/slots.

    Source returns lifecycle metadata only, allowing this same anchor's later generation.
    Caller separately CASes immutable operation/step/target before mutation. No Source
    current configuration or renewed epoch is exposed; no commit or external I/O.
    """
    if not _retained_operation_matches(
        original_operation,
        original_operation,
        source_id=source_id,
        scope=scope,
        access_fence=access_fence,
    ) or original_operation.get("retained_effect_result"):
        return None, None, {}
    source = await sources.get_connector_retained_effect_anchor_in_uow(
        session,
        source_id,
        source_generation=cast(int, original_operation["source_generation"]),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    if source is None:
        return None, None, {}
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .execution_options(populate_existing=True)
    )
    slots = list(
        await session.scalars(
            select(ConnectorManagedCredential)
            .where(
                ConnectorManagedCredential.source_id == source_id,
                ConnectorManagedCredential.slot.in_(_ALL_CREDENTIAL_SLOTS),
            )
            .order_by(ConnectorManagedCredential.slot)
            .execution_options(populate_existing=True)
        )
    )
    return source, row, {credential.slot: credential for credential in slots}


def finalize_backend(row: ConnectorProvisioning) -> None:
    """Mark the current backend revision (and for n8n the installed template) applied and go idle."""
    row.applied_backend_revision = row.backend_revision
    if row.execution_backend == "n8n":
        row.template_revision = row.applied_template_revision = CURRENT_TEMPLATE_REVISION
    row.transition_phase = "idle"
    row.target_backend = row.transition_operation_id = None
    row.old_workflow_id = None
