import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import ReplayDraft, commit_with_replay, make_source_change
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    ConnectorWorldCredential,
)
from modules.connectors.public import NativeCredentialSnapshot
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

_ALL_CREDENTIAL_SLOTS = ("collector", "manual_trigger", "provider")


async def get_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
) -> NativeCredentialSnapshot | None:
    """Return only a native credential whose persisted generation and revision match current collection authority."""
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS)
    if (
        source is None or row is None or source.generation != source_generation
        or row.desired_revision != connector_revision or row.source_generation != source_generation
    ):
        return None
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    if (
        native.source_generation != source_generation
        or native.configuration_revision != connector_revision
    ):
        return None
    return NativeCredentialSnapshot(
        source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def get_retained_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
) -> NativeCredentialSnapshot | None:
    """Return a retained binding only under the active owner's requested source/revision fence.

    Source, provisioning, managed slots, and native row are locked in the normal
    connector order before an owner may decrypt or remotely revalidate a token.
    A missing native row returns None; a stale source or revision raises ValueError.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS)
    if source is None or source.status != "active" or source.generation != source_generation:
        raise ValueError("Connector credential fence is stale")
    if row is None:
        if connector_revision == 0:
            return None
        raise ValueError("Connector credential fence is stale")
    if row.source_generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    if row.state == "disabled" and row.error_code == "deactivation_pending":
        raise ValueError("Connector deactivation is pending")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    return NativeCredentialSnapshot(
        source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def save_native_credential(
    session: AsyncSession,
    *,
    source_id: UUID,
    operation_id: UUID,
    source_generation: int,
    connector_revision: int,
    encrypted_token: str,
    token_fingerprint: str,
    verified_bot_id: str,
    validated_at: datetime,
) -> None:
    """Persist a fully verified token only under current source and connector fences."""
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS)
    if (
        source is None or row is None or source.generation != source_generation
        or row.source_generation != source_generation or row.desired_revision != connector_revision
    ):
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is not None and (
        native.bound_bot_id not in (None, verified_bot_id)
        or native.verified_bot_id not in (None, verified_bot_id)
    ):
        raise ValueError("Telegram bot identity cannot change for an existing source")
    if native is None:
        native = ConnectorNativeCredential(source_id=source_id, operation_id=operation_id)
        session.add(native)
    native.provider = "telegram"
    native.operation_id = operation_id
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.encrypted_token = encrypted_token
    native.token_fingerprint = token_fingerprint
    native.verified_bot_id = verified_bot_id
    # Keep historical identity separate from the unique live reservation.
    native.bound_bot_id = verified_bot_id
    native.state = "ready"
    native.validated_at = validated_at
    native.error_code = None
    await session.flush()


async def revoke_native_credential(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    release_bot_reservation: bool = False,
) -> None:
    """Fence native token use and clear ciphertext, optionally releasing its unique bot identity."""
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS)
    if source is None or row is None or source.generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.operation_id = uuid4()
    native.encrypted_token = None
    native.token_fingerprint = None
    native.validated_at = None
    native.state = "revoked"
    native.error_code = None
    if release_bot_reservation:
        # The unique active reservation is reusable; the source's bot binding is immutable.
        native.verified_bot_id = None
    await session.flush()


@dataclass(frozen=True)
class ConnectorObservation:
    """Capture owner-visible connector state while omitting credential material."""
    fence: SourceFence
    desired_revision: int
    applied_revision: int
    state: str
    error_code: str | None
    credential_recovery: str
    desired_enabled: bool
    credential_presence: tuple[tuple[str, bool], ...]
    header_auth_configured: bool


def _connector_observation(
    source: SourceFence | None,
    row: ConnectorProvisioning | None,
    slots: dict[str, ConnectorManagedCredential],
) -> ConnectorObservation | None:
    """Project locked connector rows into a redacted observable state snapshot."""
    if source is None:
        return None
    if row is None:
        return ConnectorObservation(
            fence=source, desired_revision=0, applied_revision=0,
            state="saved_not_active", error_code=None, credential_recovery="supported",
            desired_enabled=False, credential_presence=(), header_auth_configured=False,
        )
    unresolved = any(
        credential.state in {"dispatching", "reconciliation_required", "delete_pending"}
        for credential in slots.values()
    )
    state = row.state
    error_code = row.error_code
    if unresolved:
        error_code = "credential_operation_pending"
        if state != "disabled":
            state = "reconciliation_required"
    desired_configuration = row.desired_configuration
    return ConnectorObservation(
        fence=source,
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        state=state,
        error_code=error_code,
        credential_recovery="unsupported_operation" if unresolved else "supported",
        desired_enabled=row.desired_enabled,
        credential_presence=tuple(
            (slot, bool(slots.get(slot) and slots[slot].credential_id))
            for slot in _ALL_CREDENTIAL_SLOTS
        ),
        header_auth_configured=bool(
            isinstance(desired_configuration, dict)
            and desired_configuration.get("auth_method") == "http_header"
        ),
    )


async def capture_connector_observation(
    session: AsyncSession, source_id: UUID
) -> ConnectorObservation | None:
    """Read a coherent source/provisioning/credential snapshot under lock order."""
    source, row, slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS)
    return _connector_observation(source, row, slots)


async def commit_connector_observation(
    session: AsyncSession,
    before: ConnectorObservation | None,
    *,
    operation_id: UUID | None = None,
) -> None:
    """Commit connector state and publish a source event when its safe view changed.

    Always flushes and commits the caller's session, even when the redacted
    snapshot is unchanged and no realtime event is emitted. ``operation_id``
    correlates an emitted source change with its durable provider operation.
    With ``before=None`` there is no comparable snapshot and no event draft.
    """
    await session.flush()
    drafts: list[ReplayDraft] = []
    if before is not None:
        result = await session.execute(
            select(
                ConnectorProvisioning.desired_revision,
                ConnectorProvisioning.applied_revision,
                ConnectorProvisioning.state,
                ConnectorProvisioning.error_code,
                ConnectorProvisioning.desired_enabled,
                ConnectorProvisioning.desired_configuration,
            ).where(ConnectorProvisioning.source_id == before.fence.id)
        )
        row = result.one_or_none()
        credential_rows = list((await session.execute(
            select(
                ConnectorManagedCredential.slot,
                ConnectorManagedCredential.credential_id.is_not(None),
                ConnectorManagedCredential.state,
            ).where(ConnectorManagedCredential.source_id == before.fence.id)
        )).all())
        if row is None:
            after = ConnectorObservation(
                fence=before.fence, desired_revision=0, applied_revision=0,
                state="saved_not_active", error_code=None, credential_recovery="supported",
                desired_enabled=False, credential_presence=(), header_auth_configured=False,
            )
        else:
            values = {slot: (present, state) for slot, present, state in credential_rows}
            unresolved = any(state in {"dispatching", "reconciliation_required", "delete_pending"}
                             for _present, state in values.values())
            state = row.state
            error_code = row.error_code
            if unresolved:
                error_code = "credential_operation_pending"
                if state != "disabled":
                    state = "reconciliation_required"
            desired_configuration = row.desired_configuration
            after = ConnectorObservation(
                fence=before.fence,
                desired_revision=row.desired_revision,
                applied_revision=row.applied_revision,
                state=state,
                error_code=error_code,
                credential_recovery="unsupported_operation" if unresolved else "supported",
                desired_enabled=row.desired_enabled,
                credential_presence=tuple(
                    (slot, bool(values.get(slot, (False, "queued"))[0]))
                    for slot in _ALL_CREDENTIAL_SLOTS
                ),
                header_auth_configured=bool(
                    isinstance(desired_configuration, dict)
                    and desired_configuration.get("auth_method") == "http_header"
                ),
            )
        if after != before:
            drafts.append(make_source_change(
                before.fence.id,
                before.fence.generation,
                before.fence.status,
                connector_state=after.state,
                operation_id=operation_id,
            ))
    await commit_with_replay(session, drafts)


async def lock_connector(
    session: AsyncSession,
    source_id: UUID,
    slots: tuple[str, ...] = (),
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Lock source, provisioning, then credential slots in the one supported order."""
    source = await sources.lock_source(session, source_id)
    if source is None:
        return None, None, {}
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_slots: dict[str, ConnectorManagedCredential] = {}
    for slot in sorted(set(slots)):
        credential = await session.scalar(
            select(ConnectorManagedCredential)
            .where(
                ConnectorManagedCredential.source_id == source_id,
                ConnectorManagedCredential.slot == slot,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if credential is not None:
            locked_slots[slot] = credential
    return source, row, locked_slots


async def get_managed_credential(
    session: AsyncSession, source_id: UUID, slot: str
) -> ConnectorManagedCredential | None:
    """Read one credential slot without acquiring the provisioning lock chain."""
    return await session.scalar(
        select(ConnectorManagedCredential)
        .where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.slot == slot,
        )
        .execution_options(populate_existing=True)
    )


async def activation_status(
    session: AsyncSession, source_id: UUID
) -> ConnectorProvisioning | None:
    """Read a session-bound provisioning ORM row as a current-state hint.

    Returns None when no row exists. The row remains managed by the supplied
    session; callers must not treat it as a detached DTO or use it after that
    session closes. This lookup refreshes the identity-map row but does not
    acquire the connector provisioning lock.
    """
    return await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source_id)
        .execution_options(populate_existing=True)
    )


def _step(
    kind: str,
    target: str | None,
    request: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a durable prepared step with a unique ID and empty history."""
    return {
        "id": str(uuid4()),
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
) -> dict[str, object]:
    """Create a workflow operation envelope and its first prepared step."""
    step_kind = (
        "update" if kind == "enable" and workflow_id
        else "lookup" if kind == "enable"
        else "deactivate"
    )
    return {
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


def _new_deactivation(row: ConnectorProvisioning, generation: int) -> dict[str, Any] | None:
    """Build cleanup work for the known workflow, or None when no workflow exists."""
    if not row.workflow_id:
        return None
    operation_id = uuid4()
    return new_workflow_operation(
        operation_id=operation_id,
        kind="deactivate",
        source_generation=generation,
        revision=row.desired_revision,
        configuration={},
        workflow_id=row.workflow_id,
        workflow_name=row.workflow_name or f"BBD-OS connector {row.source_id}",
        body=None,
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
            credential is None or credential.state != "ready" or not credential.credential_id
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
) -> ConnectorProvisioning | None:
    """Revision-fence desired configuration and reconcile interrupted activation/workflow state."""
    _, row, slots = await lock_connector(
        session, source_id, ("collector", "manual_trigger", "provider")
    )
    if row is None:
        if expected_revision != 0:
            return None
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=source_generation,
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
    activation = copy.deepcopy(row.activation_intent)
    if isinstance(activation, dict):
        required = activation.get("required_credentials")
        unresolved = False
        if isinstance(required, dict):
            for slot_name in required:
                credential = slots.get(str(slot_name))
                envelope = credential.operation_envelope if credential is not None else None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("activation_id") == activation.get("id")
                ):
                    if envelope.get("state") == "prepared":
                        assert credential is not None
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
    if row.workflow_operation is not None:
        operation = copy.deepcopy(row.workflow_operation)
        step = operation.get("step")
        if operation.get("kind") == "deactivate" and isinstance(step, dict) and step.get("state") == "prepared":
            row.error_code = "deactivation_pending"
        elif isinstance(step, dict) and step.get("state") in {"prepared", "blocked"}:
            row.workflow_operation = None
            new_operation = _new_deactivation(row, source_generation) if prior_enabled else None
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
        new_operation = _new_deactivation(row, source_generation)
        if new_operation is not None:
            row.workflow_operation = new_operation
            row.error_code = "deactivation_pending"
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
) -> UUID | None:
    """Persist a workflow enable operation only while source and activation fences match."""
    source, row, slots = await lock_connector(
        session, source_id, tuple((required_credentials or {}).keys())
    )
    if (
        source is None or source.status != "active" or source.generation != source_generation
        or row is None or row.source_generation != source_generation
        or row.desired_revision != revision or row.workflow_operation is not None
        or row.activation_intent is None
        or row.activation_intent.get("id") != str(activation_id)
        or not _required_credentials_match(required_credentials or {}, slots)
    ):
        return None
    operation_id = operation_id or uuid4()
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
    )
    row.workflow_operation["required_credentials"] = copy.deepcopy(required_credentials or {})
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
) -> bool:
    """Stage activation intent and credential operations under source/slot locks.

    Returns True only when source generation, desired revision, and all credential
    bindings still match. It flushes but does not commit. A False result may follow
    partial credential-row and ``required_credentials`` dictionary mutations, so
    the caller must roll back; on True the caller must commit the prepared bundle.
    """
    slots_to_lock = tuple(sorted(set(required_credentials) | set(credential_intents)))
    source, row, slots = await lock_connector(session, source_id, slots_to_lock)
    if (
        source is None or source.status != "active" or source.generation != source_generation
        or row is None or row.source_generation != source_generation
        or row.desired_revision != revision or row.desired_enabled
        or row.state == "provisioning" or row.workflow_operation is not None
        or row.activation_intent is not None
    ):
        return False

    for slot, intent in credential_intents.items():
        existing = slots.get(slot)
        if existing is not None and existing.state in {
            "dispatching", "reconciliation_required", "delete_pending"
        }:
            return False
        if (existing.credential_id if existing is not None else None) != intent.get("target_id"):
            return False
        operation_id = UUID(str(intent["operation_id"]))
        envelope = {
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

    for slot, required in required_credentials.items():
        if slot in credential_intents:
            required["operation_id"] = str(credential_intents[slot]["operation_id"])
            required["credential_id"] = credential_intents[slot].get("target_id")
        else:
            existing = slots.get(slot)
            if (
                existing is None or existing.state != "ready" or not existing.credential_id
                or existing.resolved_binding != required.get("binding")
            ):
                return False
            required["credential_id"] = existing.credential_id
            required["operation_id"] = None

    row.desired_enabled = True
    row.state = "provisioning"
    row.error_code = None
    row.activation_intent = {
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
    session: AsyncSession, source_id: UUID, revision: int, error_code: str
) -> bool:
    """Reject a matching activation revision when no workflow operation is in flight."""
    _, row, _ = await lock_connector(session, source_id)
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
) -> bool:
    """Replace a dispatched step with its next prepared action if IDs still match."""
    _, row, _ = await lock_connector(session, source_id)
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched"
    ):
        return False
    operation["phase"] = kind
    operation["step"] = _step(kind, target, request)
    row.workflow_operation = operation
    await session.flush()
    return True


async def clear_retired_source_credentials(session: AsyncSession, source_id: UUID) -> None:
    """Remove native credential and GitHub grant ciphertext of an archived or purging source.

    Runs inside the caller's source/provisioning lock transaction, so it needs no generation
    check (the source is already fenced). Telegram: ciphertext cleared and the unique active
    bot reservation released so the same bot can be added again. GitHub: tokens cleared and the
    grant marked revoked with an opaque code. No network revoke is attempted here, because
    GitHub's revoke is app/user-wide and would break the owner's other sources of the same
    account; the remaining peers can still revoke explicitly (cleared grants drop out of the
    peer inventory). No token value is read, logged or returned.
    """
    from modules.connectors.models import GithubOAuthGrant

    native = await session.scalar(
        select(ConnectorNativeCredential).where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if native is not None and (native.state != "revoked" or native.verified_bot_id is not None):
        native.operation_id = uuid4()
        native.encrypted_token = None
        native.token_fingerprint = None
        native.validated_at = None
        native.state = "revoked"
        native.error_code = None
        native.verified_bot_id = None
    grant = await session.scalar(
        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if grant is not None and grant.encrypted_tokens is not None:
        grant.state = "revoked"
        grant.refresh_operation_id = None
        if await github_grant_has_active_peer(session, grant):
            # GitHub's revoke is app/user-wide; another live source still needs the account grant.
            grant.encrypted_tokens = None
            grant.error_code = "provider_revoke_skipped_source_deleted"
        else:
            # Last source of this GitHub account: keep the ciphertext only until the worker's
            # best-effort remote revoke (outside this transaction) clears it, whatever the outcome.
            grant.error_code = "provider_revoke_pending_source_deleted"
    await session.flush()


async def github_grant_has_active_peer(session: AsyncSession, grant: Any) -> bool:
    """Return whether another non-archived source still holds tokens for the same GitHub user.

    The peer scan is bounded (101 rows); an oversized inventory is treated as having a peer so
    the shared account grant is never revoked from under live sources.
    """
    from modules.connectors.models import GithubOAuthGrant

    peer_ids = list((await session.scalars(
        select(GithubOAuthGrant.source_id).where(
            GithubOAuthGrant.github_user_id == grant.github_user_id,
            GithubOAuthGrant.source_id != grant.source_id,
            GithubOAuthGrant.encrypted_tokens.is_not(None),
        ).limit(101)
    )).all())
    if len(peer_ids) > 100:
        return True
    for peer_id in peer_ids:
        peer = await sources.get_connector_source(session, peer_id)
        if peer is not None and peer.status != "archived":
            return True
    return False


async def finish_deleted_source_grant_revoke(
    session: AsyncSession, source_id: UUID, outcome_code: str
) -> None:
    """Clear the retained ciphertext of a deleted source's grant and record the revoke outcome.

    Always clears: a failed or impossible remote revoke never leaves token material at rest.
    """
    from modules.connectors.models import GithubOAuthGrant

    grant = await session.scalar(
        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if grant is not None:
        grant.encrypted_tokens = None
        grant.error_code = outcome_code
        await session.flush()


async def fence_source_collection(
    session: AsyncSession, source: SourceFence
) -> bool:
    """Disable connector collection at the source generation and schedule cleanup."""
    _, row, slots = await lock_connector(
        session, source.id, ("collector", "manual_trigger", "provider")
    )
    if row is None:
        return False
    row.source_generation = source.generation
    row.desired_enabled = False
    row.state = "disabled"
    if source.status == "archived":
        await clear_retired_source_credentials(session, source.id)
    activation = row.activation_intent
    if isinstance(activation, dict):
        unresolved = False
        required = activation.get("required_credentials")
        if isinstance(required, dict):
            for slot_name in required:
                credential = slots.get(str(slot_name))
                envelope = credential.operation_envelope if credential is not None else None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("activation_id") == activation.get("id")
                ):
                    if envelope.get("state") == "prepared":
                        assert credential is not None
                        credential.operation_id = None
                        credential.operation_envelope = None
                        credential.state = "ready" if credential.credential_id else "queued"
                    elif envelope.get("state") in {"dispatched", "unknown"}:
                        unresolved = True
        if unresolved:
            activation["state"] = "stale_unresolved"
            row.activation_intent = activation
            row.error_code = "activation_outcome_pending"
        else:
            row.activation_intent = None
    if row.workflow_operation is not None:
        operation = copy.deepcopy(row.workflow_operation)
        step = operation.get("step")
        if isinstance(step, dict) and step.get("state") in {"prepared", "blocked"}:
            row.workflow_operation = None
        else:
            operation["cleanup_required"] = True
            operation["error"] = "source_fenced_during_dispatch"
            row.workflow_operation = operation
            row.error_code = "workflow_operation_pending"
            await session.flush()
            return True
    new_operation = _new_deactivation(row, source.generation)
    if new_operation is not None:
        row.workflow_operation = new_operation
        row.error_code = "deactivation_pending"
    else:
        row.error_code = None
    await session.flush()
    return True


async def require_collection_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    revision: int,
    *,
    lock: bool = False,
) -> bool:
    """Require active source and fully applied desired revision, optionally locking state."""
    current_source = await sources.lock_source(session, source.id)
    if (
        current_source is None
        or current_source.status != source.status
        or current_source.generation != source.generation
    ):
        return False
    statement = select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source.id
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    else:
        statement = statement.execution_options(populate_existing=True)
    row = await session.scalar(statement)
    return bool(
        source.status == "active"
        and source.generation == source_generation
        and row is not None
        and row.source_generation == source_generation
        and row.desired_revision == revision
        and row.applied_revision == revision
        and row.desired_enabled
        and row.state == "active"
    )


async def require_validation_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    revision: int,
) -> bool:
    """Lock active source/revision state, including the initial revision-zero state.

    This allows an unsaved draft at revision zero while ensuring callers can
    recheck the same authority after a network validation without holding locks
    across that request.
    """
    current_source = await sources.lock_source(session, source.id)
    if (
        current_source is None or current_source.status != "active"
        or current_source.status != source.status
        or current_source.generation != source_generation
        or source.generation != source_generation
    ):
        return False
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return revision == 0
    return row.source_generation == source_generation and row.desired_revision == revision


async def claim_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
) -> dict[str, object] | None:
    """Durably claim a prepared credential operation before external dispatch.

    Rechecks source, activation/delete intent, generation, and revision; returns
    the dispatched envelope or None when absent/stale. Claiming commits the
    dispatch barrier (and stale-intent cleanup when applicable), preventing blind
    re-dispatch after an uncertain provider response.
    """
    before = await capture_connector_observation(session, source_id)
    source, desired, slots = await lock_connector(
        session, source_id, ("collector", "manual_trigger", "provider")
    )
    row = slots.get(slot)
    if row is None or row.operation_id != operation_id:
        return None
    envelope = copy.deepcopy(row.operation_envelope)
    if not isinstance(envelope, dict):
        return None
    if envelope.get("state") != "prepared":
        return None
    valid_enable = bool(
        envelope.get("kind") in {"create", "update"}
        and source is not None and source.status == "active"
        and desired is not None and desired.desired_enabled
        and isinstance(desired.activation_intent, dict)
        and envelope.get("activation_id") == desired.activation_intent.get("id")
        and isinstance(desired.activation_intent.get("required_credentials"), dict)
        and isinstance(desired.activation_intent["required_credentials"].get(slot), dict)
        and desired.activation_intent["required_credentials"][slot].get("operation_id") == str(operation_id)
        and source.generation == envelope.get("source_generation")
        and desired.source_generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    valid_delete = bool(
        envelope.get("kind") == "delete"
        and source is not None and source.status == "paused"
        and desired is not None and not desired.desired_enabled
        and source.generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    if not valid_enable and not valid_delete:
        activation_id = envelope.get("activation_id")
        row.operation_envelope = None
        row.operation_id = None
        row.state = "ready" if row.credential_id else "queued"
        row.error_code = "prepared_credential_intent_stale"
        if isinstance(activation_id, str) and desired is not None and isinstance(desired.activation_intent, dict):  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
            if desired.activation_intent.get("id") == activation_id:
                for sibling in slots.values():
                    sibling_envelope = sibling.operation_envelope
                    if (
                        sibling is not row and isinstance(sibling_envelope, dict)
                        and sibling_envelope.get("activation_id") == activation_id
                        and sibling_envelope.get("state") == "prepared"
                    ):
                        sibling.operation_id = None
                        sibling.operation_envelope = None
                        sibling.state = "ready" if sibling.credential_id else "queued"
                desired.activation_intent = None
                desired.desired_enabled = False
                desired.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
                desired.error_code = "activation_intent_stale"
        await commit_connector_observation(session, before)
        return None
    envelope["state"] = "dispatched"
    envelope["dispatch_started_at"] = datetime.now(UTC).isoformat()
    row.operation_envelope = envelope
    row.state = "dispatching"
    # Commit the dispatch barrier before the driver makes the external n8n call.
    await commit_connector_observation(session, before)
    return envelope


async def drive_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    client: Any,
    encryption_key: str,
) -> bool:
    """Decrypt and execute one credential operation, retaining ambiguous outcomes for recovery."""
    from modules.connectors.credentials import (
        CredentialEncryptionUnavailable,
        CredentialOutcomeUnknown,
        CredentialRequestRejected,
        CredentialUpdateOutcomeUnknown,
        decrypt_credential_input,
    )

    row = await get_managed_credential(session, source_id, slot)
    if row is None or row.operation_id is None:
        return False
    operation_id = row.operation_id
    prepared = row.operation_envelope
    if not isinstance(prepared, dict) or prepared.get("state") != "prepared":
        return False
    try:
        request, binding = decrypt_credential_input(
            encryption_key,
            str(prepared["input_ciphertext"]),
            source_id=source_id,
            slot=slot,
            operation_id=operation_id,
        )
    except CredentialEncryptionUnavailable:
        await session.rollback()
        return False
    envelope = await claim_credential_operation(session, source_id, slot, operation_id)
    if envelope is None:
        return False
    try:
        data = request["data"]
        if not isinstance(data, dict):
            raise CredentialEncryptionUnavailable("Stored connector credential request is invalid")
        name = str(request["name"])
        header = str(data["name"])
        secret = str(data["value"])
        target = envelope.get("target_id")
        if envelope.get("kind") == "create" and target is None:
            credential_id = await client.create_http_header(name, header, secret)
        elif envelope.get("kind") == "update" and isinstance(target, str):
            await client.rotate_http_header(target, name, header, secret)
            credential_id = target
        else:
            raise CredentialEncryptionUnavailable("Stored connector credential operation is invalid")
    except CredentialRequestRejected:
        before = await capture_connector_observation(session, source_id)
        changed = await fail_credential_operation(
            session, source_id, slot, operation_id, "n8n_credential_rejected", unknown=False
        )
        if changed:
            await commit_connector_observation(session, before, operation_id=operation_id)
        else:
            await session.rollback()
        return False
    except (CredentialOutcomeUnknown, CredentialUpdateOutcomeUnknown, CredentialEncryptionUnavailable):
        before = await capture_connector_observation(session, source_id)
        changed = await fail_credential_operation(
            session, source_id, slot, operation_id, "credential_operation_outcome_unknown", unknown=True
        )
        if changed:
            await commit_connector_observation(session, before, operation_id=operation_id)
        else:
            await session.rollback()
        return False
    before = await capture_connector_observation(session, source_id)
    if not await complete_credential_operation(
        session, source_id, slot, operation_id, credential_id=credential_id, binding=binding
    ):
        await session.rollback()
        return False
    await commit_connector_observation(session, before, operation_id=operation_id)
    return True


async def complete_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *,
    credential_id: str | None,
    binding: dict[str, object],
) -> bool:
    """Acknowledge a matching dispatched credential operation in the caller transaction.

    Clears encrypted request material and fences stale activation intent; returns
    False for an operation that no longer owns the dispatched slot. Flushes only,
    leaving commit to the driver that also records the safe observation/event.
    """
    source, desired, slots = await lock_connector(
        session, source_id, ("collector", "manual_trigger", "provider")
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or not isinstance(envelope, dict)
        or envelope.get("state") != "dispatched"
    ):
        return False
    if credential_id is not None:
        row.credential_id = credential_id
    row.resolved_binding = copy.deepcopy(binding)
    row.state = "ready"
    row.error_code = None
    envelope["state"] = "succeeded"
    envelope.pop("input_ciphertext", None)
    row.operation_envelope = envelope
    if (
        desired is not None and isinstance(desired.activation_intent, dict)
        and desired.activation_intent.get("id") == envelope.get("activation_id")
        and (
            source is None or source.status != "active"
            or source.generation != envelope.get("source_generation")
            or desired.source_generation != envelope.get("source_generation")
            or desired.desired_revision != envelope.get("revision")
        )
    ):
        desired.activation_intent = None
        desired.desired_enabled = False
        if source is None or source.status != "active":
            desired.state = "disabled"
        else:
            desired.state = "saved_not_active"
        desired.error_code = "activation_intent_stale"
    await session.flush()
    return True


async def fail_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    error_code: str,
    *,
    unknown: bool,
) -> bool:
    """Record a known rejection or ambiguous outcome in the caller transaction.

    Unknown outcomes retain recovery information; returns False for a stale
    operation identity. Flushes only, so the operation driver owns commit/rollback.
    """
    _source, desired, slots = await lock_connector(
        session, source_id, ("collector", "manual_trigger", "provider")
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or not isinstance(envelope, dict)
        or envelope.get("state") != "dispatched"
    ):
        return False
    envelope["state"] = "unknown" if unknown else "rejected"
    if not unknown:
        envelope.pop("input_ciphertext", None)
        row.state = "ready" if row.credential_id else "queued"
        row.operation_id = None
        row.operation_envelope = None
        row.error_code = error_code
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            for sibling in slots.values():
                sibling_envelope = sibling.operation_envelope
                if (
                    sibling is not row and isinstance(sibling_envelope, dict)
                    and sibling_envelope.get("activation_id") == activation_id
                    and sibling_envelope.get("state") == "prepared"
                ):
                    sibling.operation_id = None
                    sibling.operation_envelope = None
                    sibling.state = "ready" if sibling.credential_id else "queued"
            if desired is not None and isinstance(desired.activation_intent, dict) and desired.activation_intent.get("id") == activation_id:
                desired.activation_intent = None
        if (
            desired is not None
            and desired.desired_revision == int(envelope["revision"])
            and desired.source_generation == envelope.get("source_generation")
        ):
            desired.desired_enabled = False
            desired.state = "saved_not_active"
            desired.error_code = error_code
        await session.flush()
        return True
    else:
        row.state = "reconciliation_required"
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            for sibling in slots.values():
                sibling_envelope = sibling.operation_envelope
                if (
                    sibling is not row and isinstance(sibling_envelope, dict)
                    and sibling_envelope.get("activation_id") == activation_id
                    and sibling_envelope.get("state") == "prepared"
                ):
                    sibling.operation_id = None
                    sibling.operation_envelope = None
                    sibling.state = "ready" if sibling.credential_id else "queued"
            if desired is not None and isinstance(desired.activation_intent, dict) and desired.activation_intent.get("id") == activation_id:
                desired.activation_intent["state"] = "outcome_unknown"
    row.error_code = error_code
    row.operation_envelope = envelope
    if (
        desired is not None and unknown and desired.desired_revision == int(envelope["revision"])
        and desired.source_generation == envelope.get("source_generation")
    ):
        desired.state = "reconciliation_required"
        desired.error_code = error_code
    await session.flush()
    return True


async def create_delete_intent(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    expected_revision: int,
) -> tuple[ConnectorProvisioning, ConnectorManagedCredential, UUID] | None:
    """Prepare deletion only for a ready credential on a paused, disabled source."""
    source, desired, slots = await lock_connector(session, source_id, (slot,))
    row = slots.get(slot)
    if (
        source is None or source.status != "paused" or desired is None
        or desired.desired_revision != expected_revision or desired.desired_enabled
        or desired.workflow_operation is not None
        or desired.state != "disabled" or not row or not row.credential_id
        or row.state in {"dispatching", "reconciliation_required", "delete_pending"}
    ):
        return None
    operation_id = uuid4()
    row.operation_id = operation_id
    row.operation_revision = expected_revision
    row.source_generation = source.generation
    row.state = "delete_pending"
    row.error_code = None
    row.operation_envelope = {
        "id": str(operation_id),
        "kind": "delete",
        "state": "prepared",
        "source_generation": source.generation,
        "revision": expected_revision,
        "target_id": row.credential_id,
        "credential_type": row.credential_type,
        "dispatch_started_at": None,
    }
    await session.flush()
    return desired, row, operation_id


async def acknowledge_credential_delete(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    target_id: str,
) -> bool:
    """Acknowledge a provider deletion only for the matching dispatched identity.

    Clears the local credential slot and flushes; returns False for a stale or
    mismatched claim. The deletion driver commits the resulting safe state.
    """
    _, _, slots = await lock_connector(session, source_id, (slot,))
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or row.credential_id != target_id
        or row.state != "dispatching" or not isinstance(envelope, dict)
        or envelope.get("id") != str(operation_id) or envelope.get("target_id") != target_id
        or envelope.get("state") != "dispatched"
    ):
        return False
    row.credential_id = None
    row.operation_id = None
    row.state = "queued"
    row.error_code = None
    row.operation_envelope = None
    row.resolved_binding = None
    await session.flush()
    return True


async def claim_workflow_step(
    session: AsyncSession, source_id: UUID
) -> dict[str, object] | None:
    """Durably claim a prepared workflow step after rechecking current fences.

    Returns the dispatched operation or None when there is no eligible step;
    None can still commit blocked-credential or stale-intent cleanup. A claim
    commits its dispatch barrier before external n8n work, preventing automatic
    blind replay when the provider outcome is uncertain.
    """
    before = await capture_connector_observation(session, source_id)
    existing = await activation_status(session, source_id)
    required = (
        existing.workflow_operation.get("required_credentials", {})
        if existing is not None and isinstance(existing.workflow_operation, dict)
        else {}
    )
    required_slots = tuple(required.keys()) if isinstance(required, dict) else ()
    source, row, slots = await lock_connector(session, source_id, required_slots)
    if source is None or row is None or not isinstance(row.workflow_operation, dict):
        return None
    operation = copy.deepcopy(row.workflow_operation)
    step = operation.get("step")
    if not isinstance(step, dict) or step.get("state") != "prepared":
        return None
    if operation.get("kind") == "enable" and not _required_credentials_match(
        operation.get("required_credentials", {}), slots
    ):
        operation["error"] = "required_credential_binding_unresolved"
        step["state"] = "blocked"
        operation["step"] = step
        row.workflow_operation = operation
        row.state = "reconciliation_required"
        row.error_code = "required_credential_binding_unresolved"
        await commit_connector_observation(session, before)
        return None
    if operation.get("kind") == "enable" and (
        not row.desired_enabled or source.status != "active"
        or row.source_generation != operation.get("source_generation")
        or row.desired_revision != operation.get("revision")
        or source.generation != operation.get("source_generation")
    ):
        if row.workflow_id:
            row.workflow_operation = _new_deactivation(row, source.generation)
            row.error_code = "deactivation_pending"
            row.state = "saved_not_active"
        else:
            row.workflow_operation = None
            row.state = "disabled" if source.status != "active" else "saved_not_active"
        row.desired_enabled = False
        if isinstance(row.activation_intent, dict) and row.activation_intent.get("id") == operation.get("activation_id"):
            row.activation_intent = None
        await commit_connector_observation(session, before)
        return None
    step["state"] = "dispatched"
    step["dispatch_started_at"] = datetime.now(UTC).isoformat()
    operation["step"] = step
    row.workflow_operation = operation
    # Commit the step's dispatch barrier before the workflow driver calls n8n.
    await commit_connector_observation(session, before)
    return operation


async def acknowledge_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    *,
    workflow_id: str | None = None,
) -> bool:
    """Acknowledge a matching dispatched step and flush its workflow transition.

    Returns False for a stale operation/step identity. The driver performs the
    final commit together with the safe source observation/event.
    """
    existing = await activation_status(session, source_id)
    current_operation = existing.workflow_operation if existing is not None else None
    required = (
        current_operation.get("required_credentials", {})
        if isinstance(current_operation, dict) else {}
    )
    required_slots = tuple(required.keys()) if isinstance(required, dict) else ()
    source, row, slots = await lock_connector(session, source_id, required_slots)
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched"
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
        source is not None and source.status == "active" and row.desired_enabled
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
        and _required_credentials_match(operation.get("required_credentials", {}), slots)
    )
    target = str(operation.get("workflow_id") or row.workflow_id or "") or None
    if kind in {"create", "update"}:
        operation["workflow_id"] = target
        if current:
            operation["phase"] = "activate"
            operation["step"] = _step("activate", target)
        else:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", target)
            row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
            row.error_code = "deactivation_pending"
        row.workflow_operation = operation
    elif kind == "activate" and current:
        row.state = "active"
        row.applied_revision = int(operation["revision"])
        row.error_code = None
        row.workflow_operation = None
        if (
            isinstance(row.activation_intent, dict)
            and row.activation_intent.get("id") == operation.get("activation_id")
        ):
            row.activation_intent = None
    elif kind in {"activate", "create", "update"}:
        operation["cleanup_required"] = True
        operation["phase"] = "deactivate"
        operation["step"] = _step("deactivate", target)
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        row.error_code = "deactivation_pending"
        row.workflow_operation = operation
        row.desired_enabled = False
    elif kind == "deactivate":
        row.workflow_operation = None
        row.error_code = None
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
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
    await session.flush()
    return True


async def resolve_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    workflow_id: str,
) -> bool:
    """Attach a recovered workflow ID when its dispatched create still matches.

    Reconciles against current source fences, then flushes the transition for the
    caller to commit; returns False when the operation/step identity is stale.
    """
    source, row, _ = await lock_connector(session, source_id)
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("kind") != "create" or step.get("state") != "unknown"
    ):
        return False
    row.workflow_id = workflow_id
    row.workflow_name = str(operation.get("workflow_name") or row.workflow_name or "")
    operation["workflow_id"] = workflow_id
    current = bool(
        source is not None and source.status == "active" and row.desired_enabled
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
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
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        row.error_code = "deactivation_pending"
    row.workflow_operation = operation
    await session.flush()
    return True


async def defer_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID | str,
    step_id: str,
) -> bool:
    """Move one unchanged unknown-create barrier behind other recovery work."""
    _, row, _ = await lock_connector(session, source_id)
    operation = row.workflow_operation if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("kind") != "create" or step.get("state") != "unknown"
    ):
        return False
    latest = await session.scalar(
        select(func.max(ConnectorProvisioning.updated_at)).where(
            ConnectorProvisioning.workflow_operation["step"]["state"].astext == "unknown",
            ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
        )
    )
    now = datetime.now(UTC)
    if latest is not None and now <= latest:
        now = latest + timedelta(microseconds=1)
    assert row is not None
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
) -> bool:
    """Record a rejected or ambiguous workflow result and flush recovery state.

    Schedules deactivation when desired state has changed; returns False for a
    stale step identity. The workflow driver owns the final transaction commit.
    """
    source, row, _ = await lock_connector(session, source_id)
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched"
    ):
        return False
    current = bool(
        source is not None and source.status == "active" and row.desired_enabled
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
            row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        else:
            row.workflow_operation = None
            if current:
                row.desired_enabled = False
                row.state = "saved_not_active"
                row.error_code = error_code
                if isinstance(row.activation_intent, dict) and row.activation_intent.get("id") == operation.get("activation_id"):
                    row.activation_intent = None
            elif not row.desired_enabled or source is None or source.status != "active":
                row.state = "disabled"
                row.error_code = "workflow_operation_rejected" if source is not None and source.generation == operation.get("source_generation") else row.error_code
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
        "reconciliation_required" if current
        else "disabled" if not row.desired_enabled or source is None or source.status != "active"
        else "saved_not_active"
    )
    await session.flush()
    return True


async def drive_workflow_operation(
    session: AsyncSession, source_id: UUID, api: Any
) -> bool:
    """Run prepared public n8n steps; every mutation is claimed and acknowledged by identity."""
    for _ in range(4):
        from modules.connectors.n8n import workflow_matches

        operation = await claim_workflow_step(session, source_id)
        if operation is None:
            return False
        step = operation.get("step")
        if not isinstance(step, dict):
            return False
        operation_id = UUID(str(operation["id"]))
        step_id = str(step["id"])
        kind = str(step["kind"])
        target = step.get("target")
        body = step.get("request")
        try:
            if kind == "lookup":
                matches = await api.find_workflows(str(operation["workflow_name"]))
                if len(matches) > 1:
                    raise ValueError("n8n has multiple workflows for this connector operation")
                if matches:
                    workflow_id = matches[0].get("id")
                    if not isinstance(workflow_id, str) or not workflow_id:
                        raise ValueError("n8n workflow lookup response omitted its ID")
                    if not isinstance(body, dict):
                        raise ValueError("Prepared workflow request body is invalid")
                    candidate = await api.get_workflow(workflow_id)
                    if not workflow_matches(body, candidate):
                        raise ValueError("n8n workflow lookup returned a mismatched identity")
                    next_kind = "update"
                    next_target = workflow_id
                else:
                    next_kind = "create"
                    next_target = None
                if not await prepare_workflow_step(
                    session, source_id, operation_id, step_id, next_kind, next_target,
                    body if isinstance(body, dict) else {},
                ):
                    await session.rollback()
                    return False
                await session.commit()
                continue
            if not isinstance(body, dict) and kind in {"create", "update"}:
                raise ValueError("Prepared workflow request body is invalid")
            if kind == "create":
                workflow_id = await api.create_workflow(body)
            elif kind == "update" and isinstance(target, str):
                await api.update_workflow(target, body)
                workflow_id = target
            elif kind == "activate" and isinstance(target, str):
                await api.set_active(target, True)
                workflow_id = None
            elif kind == "deactivate" and isinstance(target, str):
                await api.set_active(target, False)
                workflow_id = None
            else:
                raise ValueError("Unsupported prepared workflow step")
        except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            from httpx import HTTPStatusError

            response = exc.response if isinstance(exc, HTTPStatusError) else None
            known_rejection = (
                response is not None
                and 400 <= response.status_code < 500
                and response.status_code != 408
            )
            before = await capture_connector_observation(session, source_id)
            changed = await fail_workflow_step(
                session, source_id, operation_id, step_id,
                "n8n_request_rejected" if known_rejection else "n8n_outcome_unknown",
                unknown=not known_rejection and kind != "lookup",
            )
            if changed:
                await commit_connector_observation(session, before, operation_id=operation_id)
            else:
                await session.rollback()
            return False
        before = await capture_connector_observation(session, source_id)
        if not await acknowledge_workflow_step(
            session, source_id, operation_id, step_id, workflow_id=workflow_id
        ):
            await session.rollback()
            return False
        await commit_connector_observation(session, before, operation_id=operation_id)
    return False


async def mark_reconciliation(
    session: AsyncSession,
    source_id: UUID,
    desired_revision: int,
    state: str,
    *,
    error_code: str | None = None,
    workflow_id: str | None = None,
    applied_revision: int | None = None,
) -> bool:
    """Update provisioning status only while the expected desired revision is current."""
    _, row, _ = await lock_connector(session, source_id)
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


async def unresolved_credential_error(session: AsyncSession, source_id: UUID) -> str | None:
    """Return a stable error code when any source credential operation needs recovery."""
    rows = await session.scalars(
        select(ConnectorManagedCredential).where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.state.in_(
                ("dispatching", "reconciliation_required", "delete_pending")
            ),
        )
    )
    return "credential_operation_pending" if rows.first() is not None else None
