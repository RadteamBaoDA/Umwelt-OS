import copy
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.connectors import provider_terms, provisioning, scheduler
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    N8nCredentials,
    encrypt_credential_input,
    secret_fingerprint,
)
from modules.connectors.models import (
    ConnectorNativeCredential,
    ConnectorOAuthGrant,
    ConnectorProvisioning,
    ConnectorRestCredential,
    GithubOAuthGrant,
)
from modules.connectors.n8n import N8nApi, build_workflow, workflow_name
from modules.connectors.provider_specs import min_interval_minutes
from modules.sources import public as sources
from modules.sources.public import SourceFence


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

    operation_id = uuid7()
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


@dataclass
class _CredentialScan:
    """Outcome of scanning an activation intent's required credentials.

    ``stop`` means the caller returns ``False`` (already rolled back or committed);
    ``pending`` names a prepared credential operation to drive; otherwise ``ready_ids``
    maps each slot to its ready n8n credential id.
    """

    stop: bool = False
    pending: tuple[str, UUID] | None = None
    ready_ids: dict[str, str] = field(default_factory=dict)


async def _intent_is_current(
    session: AsyncSession, intent: dict[str, Any], required: Any, row: ConnectorProvisioning,
    source_fence: SourceFence, access_fence: AccessFence, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Return whether the prepared intent still matches the locked rows and original fence.

    Read-only under the held locks; the single awaited check is
    ``provisioning._operation_matches``. Any mismatch (workspace, actor, membership, revision,
    generation, state) means a newer operation owns the connector.
    """
    return bool(
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


def _classify_required_slot(
    value: Any, credential: Any, intent: dict[str, Any],
) -> tuple[str, UUID | None]:
    """Classify one required credential slot without touching the database.

    Returns ``(kind, operation_id)`` with kind ``invalid`` (malformed intent), ``pending``
    (a prepared operation to drive), ``in_flight`` (a dispatched operation: stop), ``unresolved``
    (binding/operation not satisfied) or ``ready``.
    """
    if not isinstance(value, dict):
        return "invalid", None
    operation_id = value.get("operation_id")
    expected_operation_id: UUID | None = None
    if operation_id is not None:
        try:
            expected_operation_id = UUID(str(operation_id))
        except ValueError:
            return "invalid", None
        envelope = credential.operation_envelope if credential is not None else None
        if (
            isinstance(envelope, dict)
            and envelope.get("id") == str(expected_operation_id)
            and envelope.get("activation_id") == intent.get("id")
            and envelope.get("state") == "prepared"
        ):
            return "pending", expected_operation_id
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
            return "in_flight", expected_operation_id
    if (
        credential is None or credential.state != "ready"
        or not credential.credential_id
        or credential.resolved_binding != value.get("binding")
        or (value.get("credential_id") is not None and credential.credential_id != value.get("credential_id"))
    ):
        return "unresolved", None
    if operation_id is not None:
        envelope = credential.operation_envelope
        if (
            not isinstance(envelope, dict)
            or envelope.get("id") != str(operation_id)
            or envelope.get("state") != "succeeded"
            or envelope.get("activation_id") != intent.get("id")
        ):
            return "unresolved", None
    return "ready", None


async def _scan_required_credentials(
    session: AsyncSession, intent: dict[str, Any], required: Any, row: ConnectorProvisioning,
    slots: dict[str, Any], before: Any, *, scope: Scope, multi_workspace_enabled: bool,
) -> _CredentialScan:
    """Walk the required slots in order and settle the first non-ready one.

    Malformed or unresolved slots mark the row ``reconciliation_required`` and commit the
    observation (one commit, then stop); an in-flight dispatched operation rolls back and stops;
    a prepared operation is returned as ``pending`` (first wins); otherwise every slot's ready
    credential id is collected. Mutates ``row`` only on the two reconciliation paths.
    """
    scan = _CredentialScan()
    for slot, value in (required or {}).items():
        credential = slots.get(slot)
        kind, operation_id = _classify_required_slot(value, credential, intent)
        if kind in ("invalid", "unresolved"):
            row.state = "reconciliation_required"
            row.error_code = (
                "activation_credential_intent_invalid" if kind == "invalid"
                else "activation_credential_binding_unresolved"
            )
            await provisioning.commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
            scan.stop = True
            return scan
        if kind == "in_flight":
            await session.rollback()
            scan.stop = True
            return scan
        if kind == "pending":
            assert operation_id is not None  # noqa: S101  # mypy narrowing invariant
            scan.pending = (slot, operation_id)
            return scan
        assert credential is not None  # kind == "ready" implies a resolved credential row  # noqa: S101  # mypy narrowing invariant
        scan.ready_ids[slot] = credential.credential_id
    return scan


async def _begin_workflow_enable(
    session: AsyncSession, source_id: UUID, row: ConnectorProvisioning, intent: dict[str, Any],
    required: Any, ready_ids: dict[str, str], before: Any,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> bool:
    """Build the workflow body and persist the prepared enable operation (commits the observation).

    Returns ``False`` after a rollback when the source disappeared or ``begin_enable_in_uow``
    refused; ``True`` after the observation commit so the driver loop re-reads the locked state.
    """
    source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        await session.rollback()
        return False
    operation_id = uuid7()
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
    return True


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
        # Dead read kept for SQL/lock-order parity with the pre-split implementation (values unused).
        await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        source_fence, row, slots = await provisioning.lock_connector(
            session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, expected_access_fence=access_fence,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        if source_fence is None or row is None or not isinstance(row.activation_intent, dict):
            await session.rollback()
            return False
        intent = row.activation_intent
        required = intent.get("required_credentials")
        if not await _intent_is_current(
            session, intent, required, row, source_fence, access_fence,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            await session.rollback()
            return False
        before = provisioning._connector_observation(source_fence, row, slots, access_fence)

        scan = await _scan_required_credentials(
            session, intent, required, row, slots, before,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if scan.stop:
            return False
        if scan.pending is not None:
            await session.rollback()
            progressed = await provisioning.drive_credential_operation(
                session, source_id, scan.pending[0], credentials, encryption_key,
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

        if not await _begin_workflow_enable(
            session, source_id, row, intent, required, scan.ready_ids, before,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        ):
            return False
    # No mutable envelope is reconstructed after transport: this is still pre-dispatch.
    source_fence, row, slots = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, expected_access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    original_operation_late = copy.deepcopy(row.workflow_operation) if row is not None and row.workflow_operation is not None else None
    await session.rollback()
    if not isinstance(original_operation_late, dict):
        return False
    return await provisioning.drive_workflow_operation(
        session, source_id, api, original_operation=original_operation_late, access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )


async def activate_native_in_uow(
    session: AsyncSession, source_id: UUID, source_fence: SourceFence, row: ConnectorProvisioning,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Activate the native backend on already-locked Source/provisioning rows; the caller commits.

    Validates active/private-allowed source, native support, terms eligibility and a ready credential
    for the exact generation/revision. Google and Reddit grants are row-locked and checked against
    the source, workspace, owner, provider and credential revision; GitHub, Telegram, and header
    credentials retain their provider-specific checks. On success, persists desired+applied revision,
    applied backend revision and schedule in the caller's transaction and returns ""; otherwise
    returns a bounded owner-action code (``local_only_source`` for privacy-blocked sources) and changes nothing. No n8n credential, workflow or key is involved.
    """
    from modules.connectors import collection, public
    from modules.settings.public import module_is_enabled

    source = await sources.get_connector_source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is not None and source.status == "active" and source_fence.local_only:
        # Privacy boundary: local-only sources (Home Assistant, FHIR, ...) never leave the device, so no collection may be scheduled.
        return "local_only_source"
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
    except (HTTPException, DomainError):
        return "terms_not_accepted"
    desired = row.desired_configuration or {}
    keyed_header = collection.KEYED_PROVIDERS.get(source.provider or "")  # fixed header, no owner-set auth config
    if keyed_header or desired.get("auth_method") == "http_header":
        credential = await session.get(ConnectorRestCredential, source_id, with_for_update=True)
        if (
            credential is None or credential.state != "ready" or credential.encrypted_secret is None
            or credential.source_generation != source.generation
            or credential.configuration_revision != row.desired_revision
            or credential.header_name != (keyed_header or desired.get("auth_header_name"))
        ):
            return "invalid_credential"
    elif (row.desired_configuration or {}).get("auth_method") not in (None, "none"):
        return "native_unsupported"
    if source.provider == "github":  # GitHub needs the owner's grant for this exact revision
        grant = await session.get(GithubOAuthGrant, source_id)
        if (grant is None or grant.state != "ready" or grant.encrypted_tokens is None
                or grant.source_generation != source.generation
                or grant.configuration_revision != row.desired_revision):
            return "invalid_credential"
    elif source.provider == "telegram":  # Telegram needs the bot credential for this exact revision
        native_credential = await session.get(ConnectorNativeCredential, source_id)
        if (native_credential is None or native_credential.state != "ready"
                or native_credential.source_generation != source.generation
                or native_credential.configuration_revision != row.desired_revision):
            return "invalid_credential"
    if source.provider in {"google_mail", "google_calendar", "google_drive", "reddit"}:
        oauth_grant = await session.get(ConnectorOAuthGrant, source_id, with_for_update=True)
        owner_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
        if (oauth_grant is None or oauth_grant.state != "ready" or oauth_grant.encrypted_tokens is None
                or oauth_grant.provider != source.provider or oauth_grant.workspace_id != source.workspace_id
                or oauth_grant.owner_id != owner_id or oauth_grant.source_generation != source.generation
                or oauth_grant.configuration_revision != row.desired_revision
                or oauth_grant.credential_revision != row.credential_revision):
            return "invalid_credential"
    row.execution_backend = "native"
    row.desired_enabled = True
    row.state = "active"
    row.applied_revision = row.desired_revision
    row.error_code = None
    provisioning.finalize_backend(row)
    interval = source.configuration.get("schedule_interval_minutes")
    if interval not in {15, 30, 60, 360, 1440}:
        interval = public.default_schedule_interval_minutes(source.type)
    # Sources saved before the per-provider floor existed are clamped here instead of migrated.
    interval = max(int(interval), min_interval_minutes(source.provider))
    await scheduler.upsert_schedule(
        session, workspace_id=source.workspace_id, source_id=source_id, interval_minutes=interval, enabled=True)
    return ""
