"""Workflow step claim, acknowledgement, unknown-create resolution and failure transitions."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorProvisioning,
)
from modules.connectors.provisioning._activation import _required_credentials_match, _step
from modules.connectors.provisioning._common import (
    _bind_n8n_service,
    _read_retained_connector_rows,
    _retained_operation_matches,
    finalize_backend,
)
from modules.connectors.provisioning._retained import (
    commit_retained_connector_effect,
    lock_retained_connector_effect,
)
from modules.sources.public import SourceFence


async def claim_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
    service_url: str,
) -> dict[str, object] | None:
    """Claim current activation or exact retained prepared deactivation under original access.

    Returns the dispatched operation or None when there is no eligible step;
    None can still commit blocked-credential or stale-intent cleanup. A claim
    commits its actual endpoint and dispatch barrier before external n8n work, preventing automatic
    blind replay when the provider outcome is uncertain.
    Supplied envelope is captured before entry; CAS includes original step/request/target.
    Deactivation remains usable after Source/config advancement without changing its lineage.
    """
    before = await lock_retained_connector_effect(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source, row, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    if source is None or row is None or not isinstance(row.workflow_operation, dict):
        return None
    operation = copy.deepcopy(row.workflow_operation)
    step = operation.get("step")
    if operation.get("retained_effect_result") or not _retained_operation_matches(
        operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence
    ):
        return None
    if not isinstance(step, dict) or step.get("state") != "prepared":
        return None
    cleanup = step.get("kind") == "deactivate"
    if cleanup and (
        not isinstance(step.get("target"), str)
        or step.get("target") != operation.get("workflow_id")
        or step.get("target") != row.workflow_id
    ):
        return None
    if (
        not cleanup
        and operation.get("kind") == "enable"
        and not _required_credentials_match(operation.get("required_credentials", {}), slots)
    ):
        operation["error"] = "required_credential_binding_unresolved"
        step["state"] = "blocked"
        operation["step"] = step
        row.workflow_operation = operation
        row.state = "reconciliation_required"
        row.error_code = "required_credential_binding_unresolved"
        await commit_retained_connector_effect(
            session,
            before,
            source_id=source_id,
            original_operation=original_operation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return None
    if (
        not cleanup
        and operation.get("kind") == "enable"
        and (
            operation.get("cleanup_required")
            or not row.desired_enabled
            or source.status != "active"
            or source.local_only
            or row.source_generation != operation.get("source_generation")
            or row.desired_revision != operation.get("revision")
            or source.generation != operation.get("source_generation")
        )
    ):
        if row.workflow_id:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", row.workflow_id)
            operation["workflow_id"] = row.workflow_id
            row.workflow_operation = operation
            row.error_code = "deactivation_pending"
            row.state = "saved_not_active"
        else:
            row.workflow_operation = None
            row.state = "disabled" if source.status != "active" else "saved_not_active"
        row.desired_enabled = False
        if isinstance(row.activation_intent, dict) and row.activation_intent.get(
            "id"
        ) == operation.get("activation_id"):
            row.activation_intent = None
        await commit_retained_connector_effect(
            session,
            before,
            source_id=source_id,
            original_operation=original_operation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return None
    _bind_n8n_service(
        row,
        service_url,
        has_liability=bool(row.workflow_id or row.old_workflow_id or step.get("target")),
    )
    step["state"] = "dispatched"
    step["dispatch_started_at"] = datetime.now(UTC).isoformat()
    operation["step"] = step
    row.workflow_operation = operation
    # Commit the step's dispatch barrier before the workflow driver calls n8n.
    await commit_retained_connector_effect(
        session,
        before,
        source_id=source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return operation


def _acknowledgement_matches(
    row: ConnectorProvisioning,
    operation: dict[str, object],
    step: dict[str, object],
    original_operation: dict[str, object],
    *,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    workflow_id: str | None,
    scope: Scope,
    access_fence: AccessFence,
) -> bool:
    """Return whether a workflow acknowledgement names the retained dispatched step exactly.

    Pure check: the stored operation must not already carry a retained result, must match the
    immutable original envelope, and the step must be the dispatched one with the expected
    target/workflow identity for its kind (create has no target and needs a workflow id;
    update must repeat its target).
    """
    return not (
        operation.get("retained_effect_result")
        or not _retained_operation_matches(
            operation,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or operation.get("id") != str(operation_id)
        or step.get("id") != step_id
        or step.get("state") != "dispatched"
        or step.get("kind") in {"update", "activate", "deactivate"}
        and (
            step.get("target") != operation.get("workflow_id")
            or step.get("target") != row.workflow_id
        )
        or step.get("kind") == "update"
        and workflow_id != step.get("target")
        or step.get("kind") == "create"
        and (step.get("target") is not None or not workflow_id)
    )


def _apply_acknowledged_step(
    row: ConnectorProvisioning,
    source: SourceFence | None,
    operation: dict[str, Any],
    *,
    kind: object,
    current: bool,
    target: str | None,
) -> None:
    """Advance the provisioning row after an acknowledged step according to the step ``kind``.

    ``current`` says the activation is still wanted (Source active, revisions and credentials
    unchanged). A current create/update chains to activate; a stale one chains to deactivate
    cleanup; a current activate finalizes the backend; deactivate clears the operation; an
    unknown kind is recorded as unsupported. Mutates ``row``/``operation`` only (no flush).
    """
    if kind in {"create", "update"}:
        operation["workflow_id"] = target
        if current:
            operation["phase"] = "activate"
            operation["step"] = _step("activate", target)
        else:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", target)
            row.state = (
                "disabled" if source is None or source.status != "active" else "saved_not_active"
            )
            row.error_code = "deactivation_pending"
        row.workflow_operation = operation
    elif kind == "activate" and current:
        row.state = "active"
        row.applied_revision = int(operation["revision"])
        row.error_code = None
        row.workflow_operation = None
        finalize_backend(row)
        if isinstance(row.activation_intent, dict) and row.activation_intent.get(
            "id"
        ) == operation.get("activation_id"):
            row.activation_intent = None
    elif kind in {"activate", "create", "update"}:
        operation["cleanup_required"] = True
        operation["phase"] = "deactivate"
        operation["step"] = _step("deactivate", target)
        row.state = (
            "disabled" if source is None or source.status != "active" else "saved_not_active"
        )
        row.error_code = "deactivation_pending"
        row.workflow_operation = operation
        row.desired_enabled = False
    elif kind == "deactivate":
        row.workflow_operation = None
        row.error_code = None
        row.state = (
            "disabled" if source is None or source.status != "active" else "saved_not_active"
        )
        if (
            operation.get("cleanup_required")
            and isinstance(row.activation_intent, dict)
            and row.activation_intent.get("id") == operation.get("activation_id")
        ):
            row.activation_intent = None
    else:
        operation["error"] = "unsupported_workflow_step"
        row.workflow_operation = operation
        row.error_code = "workflow_operation_unsupported"
        row.state = "disabled" if not row.desired_enabled else "saved_not_active"


async def acknowledge_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    *,
    workflow_id: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Acknowledge a matching dispatched step and flush its workflow transition.

    Returns False for a stale operation/step identity. The driver performs the
    final commit together with the safe source observation/event.

    Caller holds original admission/retained Source/provisioning/all slots. Immutable
    original envelope/step/request/target CAS precedes current activation-versus-cleanup
    selection. Later generation/config never discards a known effect or publishes stale
    activation. Flush-only; caller uses commit_retained_connector_effect after settlement.
    """
    from modules.settings.public import module_is_enabled

    source, row, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None
        or not isinstance(operation, dict)
        or not isinstance(step, dict)
        or source is None
        or not _acknowledgement_matches(
            row,
            operation,
            step,
            original_operation,
            source_id=source_id,
            operation_id=operation_id,
            step_id=step_id,
            workflow_id=workflow_id,
            scope=scope,
            access_fence=access_fence,
        )
    ):
        return False
    step["state"] = "succeeded"
    history = step.get("history")
    if isinstance(history, list):
        history.append({"id": step_id, "kind": step.get("kind"), "state": "succeeded"})
    operation["step"] = step
    if workflow_id is not None:
        operation["workflow_id"] = workflow_id
        row.workflow_id = workflow_id
        row.workflow_name = str(operation.get("workflow_name") or row.workflow_name or "")
    kind = step.get("kind")
    current = bool(
        await module_is_enabled(
            session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        and source is not None
        and source.status == "active"
        and not source.local_only
        and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
        and row.execution_backend == "n8n"
        and row.transition_phase in ("idle", "activating_new")
        and operation.get("backend_revision", row.backend_revision) == row.backend_revision
        and _required_credentials_match(operation.get("required_credentials", {}), slots)
    )
    target = str(operation.get("workflow_id") or row.workflow_id or "") or None
    _apply_acknowledged_step(row, source, operation, kind=kind, current=current, target=target)
    await session.flush()
    return True


async def resolve_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    workflow_id: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Attach recovered workflow under held observation admission/Source/provisioning locks.

    Caller holds lock_retained_connector_effect's original admission/Source/Connector
    rows and supplies the exact unknown-create envelope retained through identity lookup.
    Retain discovered remote ID and choose activation or exact cleanup from current fences;
    never repeat the uncertain create. Flush-only; use commit_retained_connector_effect.
    """
    from modules.settings.public import module_is_enabled

    source, row, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None
        or not isinstance(operation, dict)
        or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(
            operation,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or not isinstance(workflow_id, str)
        or not workflow_id
        or operation.get("id") != str(operation_id)
        or step.get("id") != step_id
        or step.get("kind") != "create"
        or step.get("state") != "unknown"
        or step.get("target") is not None
    ):
        return False
    row.workflow_id = workflow_id
    row.workflow_name = str(operation.get("workflow_name") or row.workflow_name or "")
    operation["workflow_id"] = workflow_id
    current = bool(
        await module_is_enabled(
            session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        and source is not None
        and source.status == "active"
        and not source.local_only
        and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
        and _required_credentials_match(operation.get("required_credentials", {}), slots)
    )
    if current:
        operation["phase"] = "activate"
        operation["step"] = _step("activate", workflow_id)
        row.state = "provisioning"
        row.error_code = None
    else:
        operation["cleanup_required"] = True
        operation["phase"] = "deactivate"
        operation["step"] = _step("deactivate", workflow_id)
        row.state = (
            "disabled" if source is None or source.status != "active" else "saved_not_active"
        )
        row.error_code = "deactivation_pending"
    row.workflow_operation = operation
    await session.flush()
    return True


async def defer_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID | str,
    step_id: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Acquire original retained parents and move one exact unknown-create barrier later.

    Later Source/config does not erase the uncertain effect. Exact immutable original
    envelope/step CAS remains mandatory; no blind redispatch or commit is performed.
    """
    await lock_retained_connector_effect(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source, row, _ = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    operation = row.workflow_operation if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        not isinstance(operation, dict)
        or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(
            operation,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or row is None
        or operation.get("id") != str(operation_id)
        or step.get("id") != step_id
        or step.get("kind") != "create"
        or step.get("state") != "unknown"
    ):
        return False
    latest = await session.scalar(
        select(func.max(ConnectorProvisioning.updated_at)).where(
            ConnectorProvisioning.source_id == source_id,
            ConnectorProvisioning.workflow_operation["step"]["state"].astext == "unknown",
            ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
        )
    )
    now = datetime.now(UTC)
    if latest is not None and now <= latest:
        now = latest + timedelta(microseconds=1)
    if row is None:
        raise RuntimeError("invariant violated: row must not be None")
    row.updated_at = now
    await session.flush()
    return True


async def fail_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    error_code: str,
    *,
    unknown: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Record a rejected or ambiguous workflow result and flush recovery state.

    Schedules deactivation when desired state has changed; returns False for a
    stale step identity. The workflow driver owns the final transaction commit.

    Caller holds original admission/retained Source/provisioning/all slots and supplies
    the immutable dispatched envelope. Known rejection and unknown outcome settle that
    exact step even after generation/config changes; unknown work never becomes prepared.
    Flush-only; use commit_retained_connector_effect without renewed epoch or scope.
    """
    from modules.settings.public import module_is_enabled

    source, row, _ = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None
        or not isinstance(operation, dict)
        or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(
            operation,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or operation.get("id") != str(operation_id)
        or step.get("id") != step_id
        or step.get("state") != "dispatched"
    ):
        return False
    current = bool(
        await module_is_enabled(
            session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        and source is not None
        and source.status == "active"
        and not source.local_only
        and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
    )
    if not unknown and step.get("kind") in {"lookup", "create", "update", "activate"}:
        target = str(operation.get("workflow_id") or row.workflow_id or "") or None
        if not current and target is not None:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", target)
            row.workflow_operation = operation
            row.error_code = "deactivation_pending"
            row.state = (
                "disabled" if source is None or source.status != "active" else "saved_not_active"
            )
        else:
            row.workflow_operation = None
            if current:
                row.desired_enabled = False
                row.state = "saved_not_active"
                row.error_code = error_code
                if isinstance(row.activation_intent, dict) and row.activation_intent.get(
                    "id"
                ) == operation.get("activation_id"):
                    row.activation_intent = None
            elif not row.desired_enabled or source is None or source.status != "active":
                row.state = "disabled"
                row.error_code = (
                    "workflow_operation_rejected"
                    if source is not None
                    and source.generation == operation.get("source_generation")
                    else row.error_code
                )
            else:
                row.state = "saved_not_active"
        await session.flush()
        return True
    step["state"] = "unknown" if unknown else "rejected"
    operation["step"] = step
    operation["error"] = error_code
    if unknown and not current:
        operation["cleanup_required"] = True
    row.workflow_operation = operation
    row.error_code = error_code if current else "workflow_operation_pending"
    row.state = (
        "reconciliation_required"
        if current
        else "disabled"
        if not row.desired_enabled or source is None or source.status != "active"
        else "saved_not_active"
    )
    await session.flush()
    return True
