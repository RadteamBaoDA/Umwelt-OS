"""Credential operation claim, drive, completion, failure and delete-intent lifecycle."""

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.provisioning._common import (
    RetainedEffectAdmissionDenied,
    _bind_n8n_service,
    _operation_identity,
    _operation_matches,
    _read_connector_rows,
    _read_retained_connector_rows,
    _retained_operation_matches,
)
from modules.connectors.provisioning._observation import (
    capture_connector_observation,
    commit_connector_observation,
)
from modules.connectors.provisioning._retained import (
    commit_retained_connector_effect,
    lock_retained_connector_effect,
    record_retained_credential_result_in_uow,
)
from modules.connectors.provisioning._status import get_managed_credential, lock_connector
from modules.connectors.public import (
    _connector_access,
)


async def _apply_retained_outcome(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    outcome: str,
    remote_id: str | None,
    binding: dict[str, object] | None,
    error_code: str | None,
) -> bool:
    """Apply a retained credential outcome through the matching state transition.

    ``known_success`` acknowledges a delete or completes a create/replace; any other outcome fails
    the operation (``unknown`` keeps it ambiguous). Runs inside the caller's already locked
    transaction and does not commit; returns whether state changed.
    """
    operation_id = UUID(str(original_operation["id"]))
    if outcome == "known_success":
        if original_operation.get("kind") == "delete":
            changed = await acknowledge_credential_delete(
                session,
                source_id,
                slot,
                operation_id,
                str(original_operation["target_id"]),
                original_operation=original_operation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence,
            )
        else:
            changed = await complete_credential_operation(
                session,
                source_id,
                slot,
                operation_id,
                credential_id=remote_id,
                binding=binding or {},
                original_operation=original_operation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence,
            )
    else:
        changed = await fail_credential_operation(
            session,
            source_id,
            slot,
            operation_id,
            error_code or "credential_operation_outcome_unknown",
            unknown=outcome == "unknown",
            original_operation=original_operation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    return changed


async def _settle_retained_credential_after_io(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    outcome: str,
    remote_id: str | None = None,
    binding: dict[str, object] | None = None,
    error_code: str | None = None,
) -> bool:
    """Settle one trusted credential transport or durably journal definitive access loss.

    Ordinary admitted path acquires original parents then exact callback and replay/commit.
    Only RetainedEffectAdmissionDenied releases all locks before fresh journal-only write.
    Journal stores no binding or secret; after commit rethrow original permission loss with
    internal journal_disposition. Missing/conflict rolls back and is never progress success.
    """
    try:
        before = await lock_retained_connector_effect(
            session,
            source_id,
            original_operation=original_operation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_credential_result_in_uow(
            session,
            source_id,
            slot,
            original_operation=original_operation,
            scope=scope,
            access_fence=access_fence,
            outcome=outcome,
            remote_id=remote_id,
            error_code=error_code,
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    changed = await _apply_retained_outcome(
        session,
        source_id,
        slot,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        outcome=outcome,
        remote_id=remote_id,
        binding=binding,
        error_code=error_code,
    )
    if changed:
        await commit_retained_connector_effect(
            session,
            before,
            source_id=source_id,
            original_operation=original_operation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    else:
        await session.rollback()
    return changed


async def _credential_send_allowed(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    envelope: dict[str, object],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Freshly admit exact dispatched credential/Source/config before send and release SQL.

    Retained envelope principal is compared to original caller scope; no actor/revision is
    rebuilt from current credentials. Denial preserves the durable dispatch barrier for
    reconciliation. Cleanup sends may dispose known remote effects on paused/local Source.
    """
    from modules.settings.public import module_is_enabled

    try:
        await lock_retained_connector_effect(
            session,
            source_id,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        source, row, slots = await _read_retained_connector_rows(
            session,
            source_id,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        credential = slots.get(slot)
        current = credential.operation_envelope if credential is not None else None
        cleanup = envelope.get("kind") == "delete"
        return bool(
            source is not None
            and row is not None
            and credential is not None
            and current is not None
            and _retained_operation_matches(
                current, envelope, source_id=source_id, scope=scope, access_fence=access_fence
            )
            and not current.get("retained_effect_result")
            and current == envelope
            and envelope.get("state") == "dispatched"
            and credential.operation_id is not None
            and str(credential.operation_id) == envelope.get("id")
            and credential.operation_revision == envelope.get("revision")
            and credential.source_generation == envelope.get("source_generation")
            and credential.credential_type == envelope.get("credential_type")
            and (
                cleanup
                and current.get("target_id") == credential.credential_id
                or not cleanup
                and source.generation == row.source_generation == envelope.get("source_generation")
                and row.desired_revision == envelope.get("revision")
                and (
                    envelope.get("kind") == "create"
                    and envelope.get("target_id") is None
                    or envelope.get("kind") == "update"
                    and envelope.get("target_id") == credential.credential_id
                )
                and source.status == "active"
                and not source.local_only
                and await module_is_enabled(
                    session,
                    "connectors",
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            )
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_credential_result_in_uow(
            session,
            source_id,
            slot,
            original_operation=envelope,
            scope=scope,
            access_fence=access_fence,
            outcome="not_sent",
            error_code="original_access_revoked",
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    finally:
        await session.rollback()


def _prepared_intent_validity(
    envelope: dict[str, object],
    source: Any,
    desired: Any,
    slot: str,
    operation_id: UUID,
) -> tuple[bool, bool]:
    """Return ``(valid_enable, valid_delete)`` for a prepared envelope against current source intent.

    An enable (create/update) must match the active source, enabled provisioning row, activation
    intent, generation and revision; a delete must match a paused source with disabled intent.
    Pure; no I/O.
    """
    valid_enable = bool(
        envelope.get("kind") in {"create", "update"}
        and source is not None
        and source.status == "active"
        and desired is not None
        and desired.desired_enabled
        and isinstance(desired.activation_intent, dict)
        and envelope.get("activation_id") == desired.activation_intent.get("id")
        and isinstance(desired.activation_intent.get("required_credentials"), dict)
        and isinstance(desired.activation_intent["required_credentials"].get(slot), dict)
        and desired.activation_intent["required_credentials"][slot].get("operation_id")
        == str(operation_id)
        and source.generation == envelope.get("source_generation")
        and desired.source_generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    valid_delete = bool(
        envelope.get("kind") == "delete"
        and source is not None
        and source.status == "paused"
        and desired is not None
        and not desired.desired_enabled
        and source.generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    return valid_enable, valid_delete


def _discard_stale_prepared_intent(
    row: Any, slots: dict[str, Any], desired: Any, source: Any, envelope: dict[str, object]
) -> None:
    """Clear a stale prepared credential operation and, when it owns the activation intent, the intent.

    Resets ``row`` and any sibling slot prepared for the same activation, then disables the
    desired state. Mutates locked ORM rows only; the caller commits the observation.
    """
    activation_id = envelope.get("activation_id")
    row.operation_envelope = None
    row.operation_id = None
    row.state = "ready" if row.credential_id else "queued"
    row.error_code = "prepared_credential_intent_stale"
    if (
        isinstance(activation_id, str)
        and desired is not None
        and isinstance(desired.activation_intent, dict)
    ):
        if desired.activation_intent.get("id") == activation_id:
            for sibling in slots.values():
                sibling_envelope = sibling.operation_envelope
                if (
                    sibling is not row
                    and isinstance(sibling_envelope, dict)
                    and sibling_envelope.get("activation_id") == activation_id
                    and sibling_envelope.get("state") == "prepared"
                ):
                    sibling.operation_id = None
                    sibling.operation_envelope = None
                    sibling.state = "ready" if sibling.credential_id else "queued"
            desired.activation_intent = None
            desired.desired_enabled = False
            desired.state = (
                "disabled" if source is None or source.status != "active" else "saved_not_active"
            )
            desired.error_code = "activation_intent_stale"


async def claim_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    service_url: str,
) -> dict[str, object] | None:
    """Durably claim a prepared credential operation before external dispatch.

    Rechecks source, activation/delete intent, generation, and revision; returns
    the dispatched envelope or None when absent/stale. Claiming commits the
    dispatch barrier and actual endpoint (and stale-intent cleanup when applicable), preventing blind
    re-dispatch after an uncertain provider response.
    """
    before = await capture_connector_observation(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    source, desired, slots = await _read_connector_rows(
        session,
        source_id,
        ("collector", "manual_trigger", "provider"),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    row = slots.get(slot)
    if row is None or row.operation_id != operation_id:
        return None
    envelope = copy.deepcopy(row.operation_envelope)
    if not isinstance(envelope, dict):
        return None
    if not await _operation_matches(
        session, envelope, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    ):
        return None
    if envelope.get("state") != "prepared":
        return None
    valid_enable, valid_delete = _prepared_intent_validity(
        envelope, source, desired, slot, operation_id
    )
    if not valid_enable and not valid_delete:
        _discard_stale_prepared_intent(row, slots, desired, source, envelope)
        await commit_connector_observation(
            session, before, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        return None
    _bind_n8n_service(
        row, service_url, has_liability=bool(row.credential_id or envelope.get("target_id"))
    )
    envelope["state"] = "dispatched"
    envelope["dispatch_started_at"] = datetime.now(UTC).isoformat()
    row.operation_envelope = envelope
    row.state = "dispatching"
    # Commit the dispatch barrier before the driver makes the external n8n call.
    await commit_connector_observation(
        session, before, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return envelope


async def drive_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    client: Any,
    encryption_key: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Dispatch current credentials; settle exact effects with original access/envelope CAS.

    ``access_fence`` is the caller's original captured fence; current access must still equal it.

    Provider I/O holds no SQL locks. Later Source/config retains known remote IDs or unknown
    recovery material for reconciliation and cannot publish stale activation. Original
    access revocation requires explicit journal-only reconciliation authority.
    """
    from modules.connectors.credentials import (
        CredentialEncryptionUnavailable,
        CredentialOutcomeUnknown,
        CredentialRequestRejected,
        CredentialUpdateOutcomeUnknown,
        decrypt_credential_input,
    )

    await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    row = await get_managed_credential(
        session, source_id, slot, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
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
    envelope = await claim_credential_operation(
        session,
        source_id,
        slot,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        service_url=client.service_url,
    )
    if envelope is None:
        await session.rollback()
        return False
    if not await _credential_send_allowed(
        session,
        source_id,
        slot,
        envelope,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    ):
        await _settle_retained_credential_after_io(
            session,
            source_id,
            slot,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            outcome="not_sent",
            error_code="original_effect_send_fenced",
        )
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
            raise CredentialEncryptionUnavailable(
                "Stored connector credential operation is invalid"
            )
    except asyncio.CancelledError:
        await _settle_retained_credential_after_io(
            session,
            source_id,
            slot,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            outcome="unknown",
            error_code="credential_operation_outcome_unknown",
        )
        raise
    except CredentialRequestRejected:
        await _settle_retained_credential_after_io(
            session,
            source_id,
            slot,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            outcome="known_rejection",
            error_code="n8n_credential_rejected",
        )
        return False
    except (
        CredentialOutcomeUnknown,
        CredentialUpdateOutcomeUnknown,
        CredentialEncryptionUnavailable,
    ):
        await _settle_retained_credential_after_io(
            session,
            source_id,
            slot,
            original_operation=envelope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            outcome="unknown",
            error_code="credential_operation_outcome_unknown",
        )
        return False
    return await _settle_retained_credential_after_io(
        session,
        source_id,
        slot,
        original_operation=envelope,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        outcome="known_success",
        remote_id=credential_id,
        binding=binding,
    )


async def complete_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *,
    credential_id: str | None,
    binding: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Acknowledge a matching dispatched credential operation in the caller transaction.

    Clears encrypted request material and fences stale activation intent; returns
    False for an operation that no longer owns the dispatched slot. Flushes only,
    leaving commit to the driver that also records the safe observation/event.

    Caller holds lock_retained_connector_effect's original admission/Source/provisioning/
    slots. Compare captured envelope/target under original access; later Source/config
    only selects retained reconciliation, never ready activation. Flush-only; commit through
    commit_retained_connector_effect. Revoked access needs explicit reconciliation authority.
    """
    from modules.settings.public import module_is_enabled

    source, desired, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None
        or row.operation_id != operation_id
        or not isinstance(envelope, dict)
        or envelope.get("state") != "dispatched"
        or envelope.get("id") != str(operation_id)
        or row.operation_revision != envelope.get("revision")
        or row.source_generation != envelope.get("source_generation")
        or envelope.get("retained_effect_result")
        or not _retained_operation_matches(
            envelope,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or desired is None
        or envelope.get("kind") not in {"create", "update"}
        or not isinstance(credential_id, str)
        or not credential_id
        or envelope.get("kind") == "update"
        and envelope.get("target_id") != credential_id
    ):
        return False
    if credential_id is not None:
        row.credential_id = credential_id
    row.resolved_binding = copy.deepcopy(binding)
    current = bool(
        await module_is_enabled(
            session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        and source.status == "active"
        and not source.local_only
        and source.generation == desired.source_generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    row.state = "ready" if current else "reconciliation_required"
    row.error_code = None if current else "retained_credential_effect_requires_reconciliation"
    desired.credential_revision += 1
    envelope["state"] = "succeeded"
    if not current:
        envelope["cleanup_required"] = True
        envelope["error"] = "retained_credential_effect_requires_reconciliation"
    envelope.pop("input_ciphertext", None)
    row.operation_envelope = envelope
    if (
        desired is not None
        and isinstance(desired.activation_intent, dict)
        and desired.activation_intent.get("id") == envelope.get("activation_id")
        and not current
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


def _release_prepared_siblings(row: Any, slots: dict[str, Any], activation_id: str) -> None:
    """Clear every other slot's prepared operation that belongs to the same activation.

    Each released slot returns to ``ready`` when it holds a credential, otherwise ``queued``.
    Mutates locked ORM rows only; the caller flushes.
    """
    for sibling in slots.values():
        sibling_envelope = sibling.operation_envelope
        if (
            sibling is not row
            and isinstance(sibling_envelope, dict)
            and sibling_envelope.get("activation_id") == activation_id
            and sibling_envelope.get("state") == "prepared"
        ):
            sibling.operation_id = None
            sibling.operation_envelope = None
            sibling.state = "ready" if sibling.credential_id else "queued"


def _dispatched_envelope_matches(
    row: Any,
    envelope: dict[str, Any],
    operation_id: UUID,
    original_operation: dict[str, object],
    *,
    source_id: UUID,
    scope: Scope,
    access_fence: AccessFence,
) -> bool:
    """Return whether ``envelope`` is the still-dispatched, un-settled operation captured in the original.

    Requires the slot's operation id/revision/generation to equal the envelope's, no retained
    effect result already recorded, and the retained-operation identity to match. Pure; no I/O.
    """
    return (
        row.operation_id == operation_id
        and envelope.get("state") == "dispatched"
        and envelope.get("id") == str(operation_id)
        and row.operation_revision == envelope.get("revision")
        and row.source_generation == envelope.get("source_generation")
        and not envelope.get("retained_effect_result")
        and _retained_operation_matches(
            envelope,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
    )


def _desired_matches_envelope(desired: Any, envelope: dict[str, Any]) -> bool:
    """Return whether the provisioning row still sits at the envelope's revision and generation.

    False when ``desired`` is missing. Pure; used to decide if a failed credential operation may
    also disable or flag the owner's desired configuration.
    """
    return (
        desired is not None
        and desired.desired_revision == int(envelope["revision"])
        and desired.source_generation == envelope.get("source_generation")
    )


async def fail_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    error_code: str,
    *,
    unknown: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Record a known rejection or ambiguous outcome in the caller transaction.

    Unknown outcomes retain recovery information; returns False for a stale
    operation identity. Flushes only, so the operation driver owns commit/rollback.

    Caller holds original admission/retained Source/provisioning/all slots. Immutable
    captured operation/target CAS is independent of current generation/revision; unknown
    outcomes retain exact encrypted recovery material and never become redispatchable.
    Flush-only; commit through commit_retained_connector_effect under original access.
    """
    source, desired, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None
        or not isinstance(envelope, dict)
        or not _dispatched_envelope_matches(
            row,
            envelope,
            operation_id,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or desired is None
    ):
        return False
    envelope["state"] = "unknown" if unknown else "rejected"
    if unknown and (
        source.generation != envelope.get("source_generation")
        or desired.desired_revision != envelope.get("revision")
        or source.status != "active"
        or source.local_only
    ):
        envelope["cleanup_required"] = True
    if not unknown:
        envelope.pop("input_ciphertext", None)
        row.state = "ready" if row.credential_id else "queued"
        row.operation_id = None
        row.operation_envelope = None
        row.error_code = error_code
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            _release_prepared_siblings(row, slots, activation_id)
            if (
                desired is not None
                and isinstance(desired.activation_intent, dict)
                and desired.activation_intent.get("id") == activation_id
            ):
                desired.activation_intent = None
        if _desired_matches_envelope(desired, envelope):
            desired.desired_enabled = False
            desired.state = "saved_not_active"
            desired.error_code = error_code
        await session.flush()
        return True
    else:
        row.state = "reconciliation_required"
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            _release_prepared_siblings(row, slots, activation_id)
            if (
                desired is not None
                and isinstance(desired.activation_intent, dict)
                and desired.activation_intent.get("id") == activation_id
            ):
                desired.activation_intent["state"] = "outcome_unknown"
    row.error_code = error_code
    row.operation_envelope = envelope
    if unknown and _desired_matches_envelope(desired, envelope):
        desired.state = "reconciliation_required"
        desired.error_code = error_code
    await session.flush()
    return True


async def create_delete_intent(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[ConnectorProvisioning, ConnectorManagedCredential, UUID] | None:
    """Acquire admission/Source/provisioning/slots before the held create_delete_intent mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    access_fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    _source, _desired, _slots = await lock_connector(
        session, source_id, (slot,), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return await create_delete_intent_in_uow(
        session,
        source_id,
        slot,
        expected_revision,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


async def create_delete_intent_in_uow(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> tuple[ConnectorProvisioning, ConnectorManagedCredential, UUID] | None:
    """Prepare deletion under held admission/Source/provisioning/exact credential slot locks.

    Compare original AccessFence without locks; Source must be paused/config disabled.
    Stamp the original principal/config in the durable intent; flush only, caller commits.
    """
    await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source, desired, slots = await _read_connector_rows(
        session, source_id, (slot,), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    row = slots.get(slot)
    if (
        source is None
        or source.status != "paused"
        or desired is None
        or desired.desired_revision != expected_revision
        or desired.desired_enabled
        or desired.workflow_operation is not None
        or desired.state != "disabled"
        or not row
        or not row.credential_id
        or row.state in {"dispatching", "reconciliation_required", "delete_pending"}
    ):
        return None
    operation_id = uuid7()
    row.operation_id = operation_id
    row.operation_revision = expected_revision
    row.source_generation = source.generation
    row.state = "delete_pending"
    row.error_code = None
    desired.credential_revision += 1
    row.operation_envelope = {
        **_operation_identity(scope, access_fence),
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
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    original_operation: dict[str, object],
    access_fence: AccessFence,
) -> bool:
    """Acknowledge exact dispatched deletion under held observation admission/Source/slots.

    Caller acquired lock_retained_connector_effect after I/O with original scope/access.
    Compare immutable original operation/remote target, allowing later Source/config only
    for this exact delete result. Flush-only; caller commits retained effect or rolls back.
    """
    source, desired, slots = await _read_retained_connector_rows(
        session,
        source_id,
        original_operation=original_operation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None
        or row.operation_id != operation_id
        or row.credential_id != target_id
        or row.state != "dispatching"
        or not isinstance(envelope, dict)
        or envelope.get("id") != str(operation_id)
        or envelope.get("target_id") != target_id
        or envelope.get("state") != "dispatched"
        or envelope.get("kind") != "delete"
        or row.operation_revision != envelope.get("revision")
        or row.source_generation != envelope.get("source_generation")
        or envelope.get("retained_effect_result")
        or not _retained_operation_matches(
            envelope,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or source is None
        or desired is None
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
