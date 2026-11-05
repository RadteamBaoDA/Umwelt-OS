"""Desired-configuration saves, enable/activation bundles and workflow step preparation."""

import copy
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
    ConnectorWorldCredential,
)
from modules.connectors.provisioning._common import (
    _lock_connector_rows,
    _operation_identity,
    _operation_matches,
    _read_connector_rows,
    _read_retained_connector_rows,
    _retained_operation_matches,
)
from modules.connectors.provisioning._status import lock_connector
from modules.connectors.public import (
    _connector_access,
)
from modules.sources import public as sources


def _step(
    kind: str,
    target: str | None,
    request: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a durable prepared step with a unique ID and empty history."""
    return {
        "id": str(uuid7()),
        "kind": kind,
        "target": target,
        "request": copy.deepcopy(request or {}),
        "state": "prepared",
        "dispatch_started_at": None,
        "history": [],
    }


def new_workflow_operation(
    *,
    operation_id: UUID,
    kind: str,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_id: str | None,
    workflow_name: str,
    body: dict[str, object] | None,
    activation_id: UUID | None = None,
    scope: Scope,
    access_fence: AccessFence,
) -> dict[str, object]:
    """Create a workflow operation envelope and its first prepared step."""
    step_kind = (
        "update"
        if kind == "enable" and workflow_id
        else "lookup"
        if kind == "enable"
        else "deactivate"
    )
    return {
        **_operation_identity(scope, access_fence),
        "id": str(operation_id),
        "kind": kind,
        "source_generation": source_generation,
        "revision": revision,
        "configuration": copy.deepcopy(configuration),
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "activation_id": str(activation_id) if activation_id else None,
        "phase": step_kind,
        "step": _step(step_kind, workflow_id, body),
        "cleanup_required": False,
        "error": None,
    }


def _new_deactivation(
    row: ConnectorProvisioning, generation: int, *, scope: Scope, access_fence: AccessFence
) -> dict[str, Any] | None:
    """Build cleanup work for the known workflow, or None when no workflow exists."""
    from modules.connectors.n8n import connector_workflow_names

    if not row.workflow_id:
        return None
    operation_id = uuid7()
    return new_workflow_operation(
        operation_id=operation_id,
        kind="deactivate",
        source_generation=generation,
        revision=row.desired_revision,
        configuration={},
        workflow_id=row.workflow_id,
        workflow_name=row.workflow_name or connector_workflow_names(row.source_id)[0],
        body=None,
        scope=scope,
        access_fence=access_fence,
    )


def _required_credentials_match(
    required: dict[str, dict[str, object]],
    slots: dict[str, ConnectorManagedCredential],
) -> bool:
    """Require ready credential IDs, bindings, and any succeeded operation identities."""
    for slot, value in required.items():
        if not isinstance(value, dict):
            return False
        credential = slots.get(slot)
        if (
            credential is None
            or credential.state != "ready"
            or not credential.credential_id
            or credential.resolved_binding != value.get("binding")
        ):
            return False
        expected_id = value.get("credential_id")
        if expected_id is not None and credential.credential_id != expected_id:
            return False
        expected_operation = value.get("operation_id")
        if expected_operation is not None:
            envelope = credential.operation_envelope
            if (
                not isinstance(envelope, dict)
                or envelope.get("id") != expected_operation
                or envelope.get("state") != "succeeded"
            ):
                return False
    return True


async def save_desired(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    expected_revision: int,
    configuration: dict[str, object],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Acquire admission/Source then desired-state rows; caller enters before domain locks."""
    source = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if source is None:
        return None
    return await _save_desired_in_uow(
        session,
        source_id,
        source_generation,
        expected_revision,
        configuration,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


def _retire_activation_for_saved_configuration(
    row: ConnectorProvisioning,
    slots: dict[str, ConnectorManagedCredential],
) -> None:
    """Retire a pending activation intent after the desired configuration changed.

    Mutates ``row``/``slots`` in the caller's transaction without flushing. Prepared credential
    operations of that activation are released; if any dispatched/unknown operation remains the
    intent is kept as ``stale_unresolved`` and the row enters ``reconciliation_required`` with
    ``activation_outcome_pending``, otherwise the intent is dropped.
    """
    activation = copy.deepcopy(row.activation_intent)
    if isinstance(activation, dict):
        required = activation.get("required_credentials")
        unresolved = False
        if isinstance(required, dict):
            for slot_name in required:
                credential = slots.get(str(slot_name))
                envelope = credential.operation_envelope if credential is not None else None
                if isinstance(envelope, dict) and envelope.get("activation_id") == activation.get(
                    "id"
                ):
                    if envelope.get("state") == "prepared":
                        if credential is None:
                            raise RuntimeError("invariant violated: credential must not be None")
                        credential.operation_id = None
                        credential.operation_envelope = None
                        credential.state = "ready" if credential.credential_id else "queued"
                    elif envelope.get("state") in {"dispatched", "unknown"}:
                        unresolved = True
        if unresolved:
            activation["state"] = "stale_unresolved"
            row.activation_intent = activation
            row.state = "reconciliation_required"
            row.error_code = "activation_outcome_pending"
        else:
            row.activation_intent = None


def _retire_workflow_operation_for_saved_configuration(
    row: ConnectorProvisioning,
    *,
    prior_enabled: bool,
    source_generation: int,
    scope: Scope,
    access_fence: AccessFence,
) -> None:
    """Reconcile the pending workflow operation with a changed desired configuration.

    Mutates ``row`` without flushing. A prepared/blocked step is discarded (replaced by a fresh
    deactivation when the source was enabled), a prepared deactivation is kept pending, and an
    already-dispatched operation is flagged for cleanup. With no operation, a previously enabled
    source gets a new deactivation.
    """
    if row.workflow_operation is not None:
        operation = copy.deepcopy(row.workflow_operation)
        step = operation.get("step")
        if (
            operation.get("kind") == "deactivate"
            and isinstance(step, dict)
            and step.get("state") == "prepared"
        ):
            row.error_code = "deactivation_pending"
        elif isinstance(step, dict) and step.get("state") in {"prepared", "blocked"}:
            row.workflow_operation = None
            new_operation = (
                _new_deactivation(row, source_generation, scope=scope, access_fence=access_fence)
                if prior_enabled
                else None
            )
            if new_operation is not None:
                row.workflow_operation = new_operation
                row.error_code = "deactivation_pending"
            else:
                row.error_code = None
        else:
            operation["cleanup_required"] = True
            operation["error"] = "desired_state_changed_during_dispatch"
            row.workflow_operation = operation
            row.error_code = "workflow_operation_pending"
    elif prior_enabled:
        new_operation = _new_deactivation(
            row, source_generation, scope=scope, access_fence=access_fence
        )
        if new_operation is not None:
            row.workflow_operation = new_operation
            row.error_code = "deactivation_pending"


async def _save_desired_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    expected_revision: int,
    configuration: dict[str, object],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Continue held admission/Source into provisioning/sorted slots then revision-fenced mutation.

    Source setter holds only earlier parents. No earlier acquisition or commit; share the
    ordinary save_desired wrapper's complete interrupted activation/workflow cleanup body.
    """
    access_fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    source, row, slots = await _lock_connector_rows(
        session,
        source_id,
        ("collector", "manual_trigger", "provider"),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.generation != source_generation:
        return None
    if row is None:
        if expected_revision != 0:
            return None
        from modules.connectors import collection  # lazy: collection imports this module

        packaged = await sources.get_connector_source(
            session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
        )
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=source_generation,
            execution_backend="native"
            if packaged is not None and collection.supports_native(packaged)
            else "n8n",  # new rows only
            desired_revision=1,
            desired_configuration=copy.deepcopy(configuration),
            state="saved_not_active",
            desired_enabled=False,
        )
        session.add(row)
        await session.flush()
        return row
    if row.desired_revision != expected_revision:
        return None

    prior_enabled = row.desired_enabled or row.state == "active"
    row.source_generation = source_generation
    row.desired_revision += 1
    world_credential = await session.get(ConnectorWorldCredential, source_id, with_for_update=True)
    if world_credential is not None:
        await session.delete(world_credential)
    row.desired_configuration = copy.deepcopy(configuration)
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = None
    _retire_activation_for_saved_configuration(row, slots)
    _retire_workflow_operation_for_saved_configuration(
        row,
        prior_enabled=prior_enabled,
        source_generation=source_generation,
        scope=scope,
        access_fence=access_fence,
    )
    await session.flush()
    return row


async def begin_enable(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_name: str,
    body: dict[str, object],
    operation_id: UUID | None = None,
    required_credentials: dict[str, dict[str, object]] | None = None,
    activation_id: UUID | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Acquire admission/Source/provisioning/slots before the held begin_enable mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    access_fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    _source, _row, _slots = await lock_connector(
        session,
        source_id,
        tuple((required_credentials or {}).keys()),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return await begin_enable_in_uow(
        session,
        source_id,
        source_generation,
        revision,
        configuration,
        workflow_name,
        body,
        operation_id,
        required_credentials,
        activation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


async def begin_enable_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_name: str,
    body: dict[str, object],
    operation_id: UUID | None = None,
    required_credentials: dict[str, dict[str, object]] | None = None,
    activation_id: UUID | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> UUID | None:
    """Create enable work under held admission/Source/provisioning/required credential locks.

    Fresh nonlocking proof checks the original AccessFence and durable activation principal;
    no upstream locks, provider I/O or commit. Caller captured parents before entering.
    """
    await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source, row, slots = await _read_connector_rows(
        session,
        source_id,
        tuple((required_credentials or {}).keys()),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None
        or source.status != "active"
        or source.generation != source_generation
        or row is None
        or row.source_generation != source_generation
        or row.desired_revision != revision
        or row.workflow_operation is not None
        or row.activation_intent is None
        or row.activation_intent.get("id") != str(activation_id)
        or not await _operation_matches(
            session,
            row.activation_intent,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        or not _required_credentials_match(required_credentials or {}, slots)
    ):
        return None
    operation_id = operation_id or uuid7()
    row.desired_enabled = True
    row.state = "provisioning"
    row.error_code = None
    row.workflow_operation = new_workflow_operation(
        operation_id=operation_id,
        kind="enable",
        source_generation=source_generation,
        revision=revision,
        configuration=configuration,
        workflow_id=row.workflow_id,
        workflow_name=workflow_name,
        body=body,
        activation_id=activation_id,
        scope=scope,
        access_fence=access_fence,
    )
    row.workflow_operation["required_credentials"] = copy.deepcopy(required_credentials or {})
    row.workflow_operation["backend_revision"] = row.backend_revision
    await session.flush()
    return operation_id


async def begin_activation_bundle(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    activation_id: UUID,
    required_credentials: dict[str, dict[str, object]],
    credential_intents: dict[str, dict[str, object]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Acquire admission/Source/provisioning/slots before the held begin_activation_bundle mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    slots_to_lock = tuple(sorted(set(required_credentials) | set(credential_intents)))
    access_fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    _source, _row, _slots = await lock_connector(
        session,
        source_id,
        slots_to_lock,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return await begin_activation_bundle_in_uow(
        session,
        source_id,
        source_generation,
        revision,
        configuration,
        activation_id,
        required_credentials,
        credential_intents,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


def _stage_credential_intent(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    intent: dict[str, object],
    existing: ConnectorManagedCredential | None,
    *,
    activation_id: UUID,
    source_generation: int,
    revision: int,
    scope: Scope,
    access_fence: AccessFence,
) -> None:
    """Stage one prepared credential operation envelope on a (new or existing) managed slot.

    Adds a new ``ConnectorManagedCredential`` to ``session`` or updates ``existing`` in place and
    marks it ``queued``; performs no flush, I/O or commit. The caller has already verified the slot
    is not mid-operation and that its target credential id matches the intent.
    """
    operation_id = UUID(str(intent["operation_id"]))
    envelope = {
        **_operation_identity(scope, access_fence),
        "id": str(operation_id),
        "activation_id": str(activation_id),
        "kind": str(intent["kind"]),
        "state": "prepared",
        "source_generation": source_generation,
        "revision": revision,
        "credential_type": str(intent["credential_type"]),
        "target_id": intent.get("target_id"),
        "binding": copy.deepcopy(intent["binding"]),
        "input_ciphertext": str(intent["input_ciphertext"]),
        "dispatch_started_at": None,
    }
    if existing is None:
        existing = ConnectorManagedCredential(
            source_id=source_id,
            slot=slot,
            credential_id=intent.get("target_id"),
            operation_id=operation_id,
            operation_revision=revision,
            source_generation=source_generation,
            credential_type=str(intent["credential_type"]),
            state="queued",
            operation_envelope=envelope,
        )
        session.add(existing)
    else:
        existing.operation_id = operation_id
        existing.operation_revision = revision
        existing.source_generation = source_generation
        existing.credential_type = str(intent["credential_type"])
        existing.state = "queued"
        existing.error_code = None
        existing.operation_envelope = envelope


def _bind_required_credentials(
    required_credentials: dict[str, dict[str, object]],
    credential_intents: dict[str, dict[str, object]],
    slots: dict[str, ConnectorManagedCredential],
) -> bool:
    """Attach operation/credential ids to every required slot of an activation bundle.

    A slot with a staged intent takes the intent's operation and target ids; any other slot must
    already hold a ready credential whose resolved binding equals the required one. Mutates
    ``required_credentials``; returns False (caller rolls back) when a slot is not ready.
    """
    for slot, required in required_credentials.items():
        if slot in credential_intents:
            required["operation_id"] = str(credential_intents[slot]["operation_id"])
            required["credential_id"] = credential_intents[slot].get("target_id")
        else:
            existing = slots.get(slot)
            if (
                existing is None
                or existing.state != "ready"
                or not existing.credential_id
                or existing.resolved_binding != required.get("binding")
            ):
                return False
            required["credential_id"] = existing.credential_id
            required["operation_id"] = None
    return True


async def begin_activation_bundle_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    activation_id: UUID,
    required_credentials: dict[str, dict[str, object]],
    credential_intents: dict[str, dict[str, object]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Stage activation under held admission/Source/provisioning/all required credential locks.

    Compare captured AccessFence with fresh nonlocking proof; flush without I/O or commit.
    False may follow partial slot/dictionary mutation and requires rollback; True requires
    caller commit. Enter only after acquiring every required/intended slot in sorted order.
    """
    slots_to_lock = tuple(sorted(set(required_credentials) | set(credential_intents)))
    await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source, row, slots = await _read_connector_rows(
        session,
        source_id,
        slots_to_lock,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None
        or source.status != "active"
        or source.generation != source_generation
        or row is None
        or row.source_generation != source_generation
        or row.desired_revision != revision
        or row.desired_enabled
        or row.state == "provisioning"
        or row.workflow_operation is not None
        or row.activation_intent is not None
    ):
        return False

    for slot, intent in credential_intents.items():
        existing = slots.get(slot)
        if existing is not None and existing.state in {
            "dispatching",
            "reconciliation_required",
            "delete_pending",
        }:
            return False
        if (existing.credential_id if existing is not None else None) != intent.get("target_id"):
            return False
        _stage_credential_intent(
            session,
            source_id,
            slot,
            intent,
            existing,
            activation_id=activation_id,
            source_generation=source_generation,
            revision=revision,
            scope=scope,
            access_fence=access_fence,
        )

    if not _bind_required_credentials(required_credentials, credential_intents, slots):
        return False

    row.desired_enabled = True
    row.state = "provisioning"
    row.error_code = None
    if credential_intents:
        row.credential_revision += 1
    row.activation_intent = {
        **_operation_identity(scope, access_fence),
        "id": str(activation_id),
        "source_generation": source_generation,
        "revision": revision,
        "configuration": copy.deepcopy(configuration),
        "required_credentials": copy.deepcopy(required_credentials),
        "state": "prepared",
    }
    await session.flush()
    return True


async def reject_activation(
    session: AsyncSession,
    source_id: UUID,
    revision: int,
    error_code: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Reject a matching activation revision when no workflow operation is in flight."""
    _, row, _ = await lock_connector(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if row is None or row.desired_revision != revision or row.workflow_operation is not None:
        return False
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = error_code
    await session.flush()
    return True


async def prepare_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    kind: str,
    target: str | None,
    request: dict[str, object] | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Settle dispatched lookup under held original retained-effect parent locks.

    Current Source/config permits next create/update; stale lifecycle schedules exact
    known-target cleanup or removes a no-effect lookup. Original envelope/step/request
    CAS remains mandatory. Flush-only; retained effect commit owns transaction release.
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
        or source is None
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
        or operation.get("id") != str(operation_id)
        or step.get("id") != step_id
        or step.get("state") != "dispatched"
        or step.get("kind") != "lookup"
        or kind not in {"create", "update"}
        or kind == "create"
        and target is not None
        or kind == "update"
        and (not isinstance(target, str) or not target)
    ):
        return False
    current = bool(
        await module_is_enabled(
            session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        and source.status == "active"
        and not source.local_only
        and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
    )
    if not current and target is None:
        # A lookup that found no workflow has no remote effect to repeat or dispose.
        row.workflow_operation = None
        row.state = "disabled" if source.status != "active" else "saved_not_active"
        row.error_code = None
        await session.flush()
        return True
    if target is not None:
        operation["workflow_id"] = target
        row.workflow_id = target
    operation["phase"] = kind if current else "deactivate"
    operation["step"] = _step(kind, target, request) if current else _step("deactivate", target)
    if not current:
        operation["cleanup_required"] = True
        row.error_code = "deactivation_pending"
        row.state = "disabled" if source.status != "active" else "saved_not_active"
    row.workflow_operation = operation
    await session.flush()
    return True
