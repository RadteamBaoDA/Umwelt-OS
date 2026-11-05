"""Reconciliation marking and backend (n8n/native) transition lifecycle."""

import copy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.backends import CURRENT_TEMPLATE_REVISION
from modules.connectors.models import (
    ConnectorCollectionRequest,
    ConnectorManagedCredential,
    ConnectorProvisioning,
    ConnectorSchedule,
)
from modules.connectors.provisioning._activation import _new_deactivation
from modules.connectors.provisioning._common import _ALL_CREDENTIAL_SLOTS
from modules.connectors.provisioning._observation import (
    _connector_observation,
    commit_connector_observation,
)
from modules.connectors.provisioning._status import lock_connector
from modules.connectors.provisioning._workflow_drive import drive_workflow_operation
from modules.connectors.public import (
    _read_scoped_source,
)


async def mark_reconciliation(
    session: AsyncSession,
    source_id: UUID,
    desired_revision: int,
    state: str,
    *,
    error_code: str | None = None,
    workflow_id: str | None = None,
    applied_revision: int | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Update provisioning status only while the expected desired revision is current."""
    _, row, _ = await lock_connector(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if row is None or row.desired_revision != desired_revision:
        return False
    row.state = state
    row.error_code = error_code
    if workflow_id is not None:
        row.workflow_id = workflow_id
    if applied_revision is not None:
        row.applied_revision = applied_revision
    await session.flush()
    return True


async def unresolved_credential_error(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> str | None:
    """Return a stable error code when any source credential operation needs recovery."""
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError("Source not found")
    rows = await session.scalars(
        select(ConnectorManagedCredential).where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.state.in_(
                ("dispatching", "reconciliation_required", "delete_pending")
            ),
        )
    )
    return "credential_operation_pending" if rows.first() is not None else None


async def fence_active_requests(session: AsyncSession, source_id: UUID) -> None:
    """Cancel queued requests, fence running ones (token invalidated, slot freed) and pause the schedule.

    Caller holds Source/provisioning locks. A remote call already sent cannot be recalled, but its
    result can no longer be accepted because the request is no longer running.
    """
    from modules.connectors import scheduler

    active = (await session.scalars(
        select(ConnectorCollectionRequest)
        .where(ConnectorCollectionRequest.source_id == source_id,
               ConnectorCollectionRequest.status.in_(("queued", "running")))
        .with_for_update().execution_options(populate_existing=True))).all()
    for request in active:
        if request.status == "running" and request.active_admission_token is not None:
            await scheduler.settle_admission_in_uow(
                session, request.id, request.active_admission_token, outcome="cancelled", error_code="revision_changed")
        else:
            request.status, request.error_code = "cancelled", "revision_changed"
    schedule = await session.get(ConnectorSchedule, source_id, with_for_update=True)
    if schedule is not None:
        schedule.enabled = False
    await session.flush()


async def begin_backend_transition_in_uow(
    session: AsyncSession, source_id: UUID, expected_revision: int, target_backend: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ConnectorProvisioning:
    """Invalidate the backend revision, stop admission and fence current work; caller commits.

    Covers backend switches and n8n template upgrades (target == current backend with a stale
    template). Raises 404/409; nothing is sent. Source/provisioning/all slots are locked in order.
    """
    source_fence, row, slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if source_fence is None:
        raise NotFoundError("Source not found")
    if source_fence.status != "active" or row is None or row.source_generation != source_fence.generation:
        raise ConflictError("Enable this source from connector settings first")
    if row.desired_revision != expected_revision:
        raise ConflictError("Connector configuration revision is stale")
    if row.transition_phase != "idle":
        raise ConflictError("A backend transition is already in progress")
    if (
        row.state == "provisioning" or row.workflow_operation is not None or row.activation_intent is not None
        or any(c.state in {"dispatching", "reconciliation_required", "delete_pending"} for c in slots.values())
    ):
        raise ConflictError("Resolve the pending connector operation first")
    stale_template = row.execution_backend == "n8n" and row.applied_template_revision < CURRENT_TEMPLATE_REVISION
    if target_backend == row.execution_backend and not stale_template:
        raise ConflictError("Source already uses this backend")
    row.backend_revision += 1
    row.target_backend = target_backend
    row.transition_phase = "draining"
    row.transition_operation_id = uuid7()
    row.old_workflow_id = row.workflow_id if row.execution_backend == "n8n" else None
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = "backend_transition_pending"
    await fence_active_requests(session, source_id)
    await session.flush()
    return row


async def resolve_backend_transition_in_uow(
    session: AsyncSession, source_id: UUID, expected_revision: int, action: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ConnectorProvisioning:
    """Leave reconciliation_required by retrying the old-workflow stop or on explicit owner attestation.

    ``retry`` prepares a fresh idempotent deactivation; ``confirm_inactive`` records the owner's
    out-of-band confirmation that the old workflow is inactive. Neither admits any backend.
    """
    source_fence, row, _slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if source_fence is None:
        raise NotFoundError("Source not found")
    if row is None or row.transition_phase != "reconciliation_required" or row.desired_revision != expected_revision:
        raise ConflictError("No reconciliation is pending for this revision")
    if action == "retry":
        operation = _new_deactivation(row, row.source_generation, scope=scope, access_fence=access_fence)
        if operation is None:
            raise ConflictError("Old workflow is unknown; confirm it inactive instead")
        row.workflow_operation = operation
        row.transition_phase = "deactivating_old"
    else:
        row.workflow_operation = None
        row.transition_phase = "activating_new"
    row.error_code = "backend_transition_pending"
    await session.flush()
    return row


async def advance_backend_transition(
    session: AsyncSession, source_id: UUID, api: Any | None,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> str:
    """Drive a persisted transition as far as safe and return its resulting phase.

    draining -> deactivating_old (n8n stop through the reviewed workflow saga, confirmed only by its
    acknowledgement) -> activating_new. An unconfirmed stop becomes reconciliation_required and no
    backend admits. Native activation runs here; n8n activation waits for the owner's /activate.
    """
    from modules.connectors.activation import activate_native_in_uow

    for _ in range(8):
        source_fence, row, slots = await lock_connector(
            session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
        if source_fence is None or row is None:
            await session.rollback()
            return "idle"
        phase = row.transition_phase
        if phase in ("idle", "reconciliation_required"):
            await session.rollback()
            return phase
        before = _connector_observation(source_fence, row, slots, access_fence)
        if phase == "draining":
            await fence_active_requests(session, source_id)
            operation = _new_deactivation(row, row.source_generation, scope=scope, access_fence=access_fence) if row.old_workflow_id else None
            if row.old_workflow_id and operation is None:
                row.transition_phase, row.error_code = "reconciliation_required", "deactivation_unconfirmed"
            else:
                row.workflow_operation = operation
                row.transition_phase = "deactivating_old" if operation is not None else "activating_new"
        elif phase == "deactivating_old":
            operation = row.workflow_operation
            step = operation.get("step") if isinstance(operation, dict) else None
            if operation is None:
                row.transition_phase = "activating_new"  # only a succeeded stop acknowledgement clears the operation
                row.error_code = "backend_transition_pending"
            elif (
                operation.get("kind") != "deactivate" or not isinstance(step, dict)
                or step.get("state") in {"dispatched", "unknown", "rejected", "blocked"}
            ):
                row.transition_phase, row.error_code = "reconciliation_required", "deactivation_unconfirmed"
            elif api is None:  # n8n is not configured: the stop stays pending, never assumed
                await session.rollback()
                return phase
            else:
                original = copy.deepcopy(operation)
                await session.rollback()
                await drive_workflow_operation(
                    session, source_id, api, original_operation=original, access_fence=access_fence,
                    multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                continue
        else:  # activating_new: the old backend is confirmed inactive
            row.execution_backend = row.target_backend or row.execution_backend
            if row.execution_backend == "native":
                error = await activate_native_in_uow(
                    session, source_id, source_fence, row, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled)
                row.error_code = error or None
            else:
                row.error_code = "activation_required"
            resulting = row.transition_phase
            await commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
            return resulting
        await commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return "deactivating_old"
