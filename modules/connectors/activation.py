import copy
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.connectors import provider_terms, provisioning, scheduler
from modules.connectors.models import ConnectorProvisioning, ConnectorRestCredential
from modules.sources.schemas import SourceFence
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    N8nCredentials,
    encrypt_credential_input,
    secret_fingerprint,
)
from modules.connectors.n8n import N8nApi, build_workflow, workflow_name
from modules.sources import public as sources


def prepare_credential_assignment(
    *,
    source_id: UUID,
    activation_id: UUID,
    slot: str,
    credential_name: str,
    header_name: str,
    secret: str | None,
    binding: dict[str, object],
    existing: Any,
    encryption_key: str,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Prepare a credential binding and optional encrypted n8n operation intent.

    Unresolved existing operations are rejected. ``secret=None`` keeps only a
    ready credential with an ID and matching header binding; a supplied secret
    reuses a ready exact binding/fingerprint match. Otherwise the secret is
    encrypted into a create/update intent; unavailable encryption raises
    ``CredentialEncryptionUnavailable``. Returns ``(assignment, None)`` for
    keep/reuse, or ``(assignment, operation_intent)`` for a required mutation.
    This helper does not persist either value.
    """
    if existing is not None and existing.state in {
        "dispatching", "reconciliation_required", "delete_pending"
    }:
        raise ValueError("Credential operation is unresolved")
    existing_binding = existing.resolved_binding if existing is not None else None
    if secret is None:
        if (
            existing is None or existing.state != "ready" or not existing.credential_id
            or not isinstance(existing_binding, dict)
            or existing_binding.get("header_name") != header_name
        ):
            raise ValueError("A replacement credential is required for the current binding")
        return (
            {"binding": dict(existing_binding), "credential_id": existing.credential_id},
            None,
        )

    expected = {
        **binding,
        "header_name": header_name,
        "secret_fingerprint": secret_fingerprint(encryption_key, secret),
    }
    if (
        existing is not None and existing.state == "ready" and existing.credential_id
        and isinstance(existing_binding, dict) and existing_binding == expected
    ):
        # Exact fingerprint and binding equality avoids a needless provider mutation.
        return ({"binding": expected, "credential_id": existing.credential_id}, None)

    operation_id = uuid4()
    target_id = existing.credential_id if existing is not None else None
    request: dict[str, object] = {
        "name": credential_name,
        "type": "httpHeaderAuth",
        "data": {"name": header_name, "value": secret},
    }
    ciphertext = encrypt_credential_input(
        encryption_key,
        source_id=source_id,
        slot=slot,
        operation_id=operation_id,
        request=request,
        binding=expected,
    )
    return (
        {
            "binding": expected,
            "credential_id": target_id,
            "operation_id": str(operation_id),
        },
        {
            "operation_id": str(operation_id),
            "kind": "update" if target_id else "create",
            "target_id": target_id,
            "credential_type": "httpHeaderAuth",
            "binding": expected,
            "input_ciphertext": ciphertext,
            "activation_id": str(activation_id),
        },
    )


async def drive_activation(
    session: AsyncSession,
    source_id: UUID,
    api: N8nApi,
    credentials: N8nCredentials,
    encryption_key: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> bool:
    """Progress a persisted activation under its captured principal/configuration fence.

    HTTP/worker callers supply the original Scope and AccessFence, never a current
    fallback. Up to eight local transitions precede the bounded owner driver. Earlier
    Source/provisioning/slot locks are reused through held APIs; immutable workflow
    envelopes are copied before release and kept through every claim/send/settlement.
    Revoked dispatched results follow the owner's journal-only exception and raise;
    journaling grants no provider send, activation or publication authority.
    """
    if not encryption_key:
        await session.rollback()
        return False
    try:
        secret_fingerprint(encryption_key, "connector-key-validation")
    except CredentialEncryptionUnavailable:
        await session.rollback()
        return False

    for _ in range(8):
        observed = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        intent = observed.activation_intent if observed is not None else None
        required = intent.get("required_credentials", {}) if isinstance(intent, dict) else {}
        source_fence, row, slots = await provisioning.lock_connector(
            session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, expected_access_fence=access_fence,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        if source_fence is None or row is None or not isinstance(row.activation_intent, dict):
            await session.rollback()
            return False
        intent = row.activation_intent
        required = intent.get("required_credentials")
        current = bool(
            isinstance(required, dict)
            and await provisioning._operation_matches(session, intent, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            and intent.get("workspace_id") == str(access_fence.workspace_id)
            and intent.get("actor_user_id") == access_fence.user_id
            and intent.get("membership_revision") == access_fence.membership_revision
            and intent.get("workspace_configuration_revision") == access_fence.configuration_revision
            and row.desired_enabled
            and row.state == "provisioning"
            and source_fence.status == "active"
            and source_fence.generation == intent.get("source_generation")
            and row.source_generation == intent.get("source_generation")
            and row.desired_revision == intent.get("revision")
            and intent.get("state") == "prepared"
        )
        if not current:
            await session.rollback()
            return False
        before = provisioning._connector_observation(source_fence, row, slots, access_fence)

        pending: tuple[str, UUID] | None = None
        ready_ids: dict[str, str] = {}
        for slot, value in (required or {}).items():
            if not isinstance(value, dict):
                row.state = "reconciliation_required"
                row.error_code = "activation_credential_intent_invalid"
                await provisioning.commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                return False
            credential = slots.get(slot)
            operation_id = value.get("operation_id")
            if operation_id is not None:
                try:
                    expected_operation_id = UUID(str(operation_id))
                except ValueError:
                    row.state = "reconciliation_required"
                    row.error_code = "activation_credential_intent_invalid"
                    await provisioning.commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                    return False
                envelope = credential.operation_envelope if credential is not None else None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("id") == str(expected_operation_id)
                    and envelope.get("activation_id") == intent.get("id")
                    and envelope.get("state") == "prepared"
                ):
                    pending = (slot, expected_operation_id)
                    break
                if (
                    credential is not None
                    and credential.state == "dispatching"
                    and credential.operation_id == expected_operation_id
                    and isinstance(envelope, dict)
                    and envelope.get("id") == str(expected_operation_id)
                    and envelope.get("activation_id") == intent.get("id")
                    and envelope.get("revision") == intent.get("revision")
                    and envelope.get("source_generation") == intent.get("source_generation")
                    and envelope.get("state") == "dispatched"
                ):
                    await session.rollback()
                    return False
            if (
                credential is None or credential.state != "ready"
                or not credential.credential_id
                or credential.resolved_binding != value.get("binding")
                or (value.get("credential_id") is not None and credential.credential_id != value.get("credential_id"))
            ):
                row.state = "reconciliation_required"
                row.error_code = "activation_credential_binding_unresolved"
                await provisioning.commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                return False
            if operation_id is not None:
                envelope = credential.operation_envelope
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("id") != str(operation_id)
                    or envelope.get("state") != "succeeded"
                    or envelope.get("activation_id") != intent.get("id")
                ):
                    row.state = "reconciliation_required"
                    row.error_code = "activation_credential_binding_unresolved"
                    await provisioning.commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                    return False
            ready_ids[slot] = credential.credential_id

        if pending is not None:
            await session.rollback()
            progressed = await provisioning.drive_credential_operation(
                session, source_id, pending[0], credentials, encryption_key,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope, access_fence=access_fence,
            )
            if progressed:
                continue
            return False

        if row.workflow_operation is not None:
            original_operation = copy.deepcopy(row.workflow_operation)
            await session.rollback()
            return await provisioning.drive_workflow_operation(
                session, source_id, api, original_operation=original_operation, access_fence=access_fence,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )

        source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        if source is None:
            await session.rollback()
            return False
        operation_id = uuid4()
        name = (
            row.workflow_name if row.workflow_id and row.workflow_name
            else workflow_name(source_id, operation_id)
        )
        body = build_workflow(
            source,
            desired_revision=int(intent["revision"]),
            workflow_operation_id=operation_id,
            workflow_name_value=name,
            collector_credential_id=ready_ids["collector"],
            manual_credential_id=ready_ids["manual_trigger"],
            provider_credential_id=ready_ids.get("provider"),
            backend_revision=row.backend_revision,
        )
        prepared = await provisioning.begin_enable_in_uow(
            session,
            source_id,
            int(intent["source_generation"]),
            int(intent["revision"]),
            dict(intent.get("configuration", {})),
            name,
            body,
            operation_id,
            required_credentials=required,
            activation_id=UUID(str(intent["id"])),
            access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        if prepared is None:
            await session.rollback()
            return False
        await provisioning.commit_connector_observation(session, before, operation_id=operation_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    # No mutable envelope is reconstructed after transport: this is still pre-dispatch.
    source_fence, row, slots = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, expected_access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    original_operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    await session.rollback()
    if not isinstance(original_operation, dict):
        return False
    return await provisioning.drive_workflow_operation(
        session, source_id, api, original_operation=original_operation, access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )


async def activate_native_in_uow(
    session: AsyncSession, source_id: UUID, source_fence: SourceFence, row: ConnectorProvisioning,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Activate the native backend on already-locked Source/provisioning rows; the caller commits.

    Validates active/private-allowed source, native support, terms eligibility and (for header
    authentication) a ready owner-entered credential for this exact generation/revision. On success
    persists desired+applied revision, applied backend revision and the schedule in the caller's
    transaction and returns ""; otherwise returns a bounded owner-action code and changes nothing.
    No n8n credential, workflow or key is involved.
    """
    from modules.connectors import collection, public
    from modules.settings.public import module_is_enabled

    source = await sources.get_connector_source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if (
        source is None or source.status != "active" or source_fence.local_only
        or row.source_generation != source.generation or row.workflow_operation is not None
        or row.activation_intent is not None
        or not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ):
        return "source_inactive"
    if not collection.supports_native(source):
        return "native_unsupported"
    try:
        await provider_terms.require_terms_eligible(session, source)
    except HTTPException:
        return "terms_not_accepted"
    if (row.desired_configuration or {}).get("auth_method") == "http_header":
        credential = await session.get(ConnectorRestCredential, source_id, with_for_update=True)
        if (
            credential is None or credential.state != "ready" or credential.encrypted_secret is None
            or credential.source_generation != source.generation
            or credential.configuration_revision != row.desired_revision
            or credential.header_name != (row.desired_configuration or {}).get("auth_header_name")
        ):
            return "invalid_credential"
    elif (row.desired_configuration or {}).get("auth_method") not in (None, "none"):
        return "native_unsupported"
    row.execution_backend = "native"
    row.desired_enabled = True
    row.state = "active"
    row.applied_revision = row.desired_revision
    row.error_code = None
    provisioning.finalize_backend(row)
    interval = source.configuration.get("schedule_interval_minutes")
    if interval not in {15, 30, 60, 360, 1440}:
        interval = public.default_schedule_interval_minutes(source.type)
    await scheduler.upsert_schedule(
        session, workspace_id=source.workspace_id, source_id=source_id, interval_minutes=int(interval), enabled=True)
    return ""
