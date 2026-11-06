"""Connector activation, deactivation, credential and backend-transition routes (registered on the shared router)."""

from typing import Annotated, Any, Literal, cast
from uuid import UUID

import httpx
from fastapi import HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError
from core.ids import uuid7
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.connectors import (
    collection,
    provider_terms,
    provisioning,
    registry,
    scheduler,
)
from modules.connectors import public as connector_owner
from modules.connectors.activation import (
    activate_native_in_uow,
    drive_activation,
    prepare_credential_assignment,
)
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    N8nCredentials,
    decrypt_native_token,
    encrypt_native_token,
    encrypt_rest_secret,
    secret_fingerprint,
)
from modules.connectors.models import (
    ConnectorProvisioning,
    ConnectorRestCredential,
)
from modules.connectors.n8n import N8nApi
from modules.connectors.provider_specs import TERMS_INELIGIBLE
from modules.connectors.provisioning._routes import (
    config as _routes_config,  # noqa: F401  (import first: config routes register before activation routes, preserving the recorded route order)
)
from modules.connectors.provisioning._routes.common import (
    ActivationRead,
    ActivationRequest,
    BackendResolveRequest,
    BackendTransitionRequest,
    NativeRestCredentialRequest,
    OwnerWrite,
    Session,
    _activation_read,
    _owner_access,
    _source,
    _validation_send_fence,
    router,
)
from modules.connectors.public import (
    NativeCredentialRevocationSnapshot,
    ProviderRateLimited,
)
from modules.ingestion import public as ingestion
from modules.settings.public import resolve_n8n
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence


@router.post("/{source_id}/activate", response_model=ActivationRead)
async def activate_source(
    source_id: UUID,
    payload: ActivationRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Persist a revision-fenced activation bundle, then drive n8n operations.

    Owner-write authorization is enforced by the route dependency. The intent is
    committed before external calls. After dispatch, HTTP 503 can mean durable
    recovery work remains and a known n8n rejection maps to HTTP 422; validation
    failures can also return 422 before persistence. A non-2xx response after
    dispatch does not prove no external side effect occurred.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    row = await _check_activation_preconditions(
        session,
        source_id,
        source,
        payload,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    backend = _choose_backend(payload.execution_backend, row, source)
    if backend == "native":
        return await activate_native(
            session, request, scope, source, payload, access_fence=access_fence
        )
    if backend != row.execution_backend and not _is_fresh(row):
        raise HTTPException(status_code=409, detail="Use backend-transition to change the backend")
    settings = request.app.state.settings
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    api_key, webhook_token, encryption_key = _require_n8n_activation_settings(n8n, settings, source)

    activation_id = uuid7()
    validation_access_fence = access_fence
    if source.provider == "telegram":
        validation_access_fence = await _stage_telegram_activation(
            session,
            request,
            source_id,
            source,
            payload,
            activation_id=activation_id,
            encryption_key=encryption_key,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    elif (
        payload.secret_action == "replace"  # noqa: S105  # label/URL/enum value, not a credential
        and row.desired_configuration.get("auth_method") != "http_header"
    ):
        raise HTTPException(
            status_code=422, detail="This provider does not accept a replacement token"
        )

    # Save/validation may have held native rows; restart from original admission
    # before staging all earlier bundle parents. Commit native save separately.
    if source.provider == "telegram":
        await commit_with_replay(
            session,
            [],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=validation_access_fence,
        )
    else:
        await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    source_fence, locked_row, _slots = await provisioning.lock_connector(
        session,
        source_id,
        provisioning._ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
    )
    if (
        source_fence is None
        or locked_row is None
        or source_fence.generation != source.generation
        or source_fence.status != "active"
        or locked_row.desired_revision != payload.expected_revision
    ):
        raise HTTPException(status_code=409, detail="Original activation configuration changed")
    row = locked_row
    if (
        row.execution_backend != "n8n"
    ):  # fresh native->n8n switch (checked above); a new backend revision fences old work
        row.execution_backend = "n8n"
        row.backend_revision += 1
    try:
        required_credentials, credential_intents = await _stage_bundle_credentials(
            session,
            source_id,
            source,
            row,
            payload,
            activation_id=activation_id,
            encryption_key=encryption_key,
            webhook_token=webhook_token,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
        )
    except (CredentialEncryptionUnavailable, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not await provisioning.begin_activation_bundle_in_uow(
        session,
        source_id,
        source.generation,
        payload.expected_revision,
        dict(row.desired_configuration),
        activation_id,
        required_credentials,
        credential_intents,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
        access_fence=access_fence,
    ):
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Connector activation changed; reload and retry"
        )
    await commit_with_replay(
        session,
        [
            make_source_change(
                source.id, source.generation, source.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )

    credentials = N8nCredentials(n8n.service_url, api_key)
    api = N8nApi(n8n.service_url, api_key, settings)
    await drive_activation(
        session,
        source_id,
        api,
        credentials,
        encryption_key,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
        access_fence=access_fence,
    )
    return await _read_activation_outcome(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


async def _read_activation_outcome(
    session: AsyncSession,
    source_id: UUID,
    *,
    multi_workspace_enabled: bool,
    scope: WorkspaceContext,
) -> ActivationRead:
    """Read the post-drive activation state and map non-active outcomes to HTTP errors.

    Read-only. 404 if the state vanished, 422 when n8n rejected a credential, 503 while
    reconciliation is still pending; otherwise the projected ``ActivationRead``.
    """
    latest = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="Connector state disappeared during activation")
    if latest.state != "active":
        if latest.state == "saved_not_active" and latest.error_code == "n8n_credential_rejected":
            raise HTTPException(
                status_code=422, detail="n8n rejected a connector credential; review it and retry"
            )
        raise HTTPException(
            status_code=503, detail="Connector activation is pending reconciliation"
        )
    return await _activation_read(
        session, source_id, latest, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


async def _check_activation_preconditions(
    session: AsyncSession,
    source_id: UUID,
    source: ConnectorSource,
    payload: ActivationRequest,
    *,
    multi_workspace_enabled: bool,
    scope: WorkspaceContext,
) -> ConnectorProvisioning:
    """Read the activation row and reject stale, in-flight, or changed state (HTTP 409).

    Read-only (no lock, no commit). Order matters for the reported detail: stale revision,
    reconciling, pending deactivation, backend transition, unresolved credential operation,
    then source status/generation. Returns the row narrowed to non-``None``.
    """
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if row is None or row.desired_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if row.state == "provisioning":
        raise HTTPException(
            status_code=409, detail="Connector activation is already being reconciled"
        )
    if row.state == "disabled" and row.error_code == "deactivation_pending":
        raise HTTPException(
            status_code=409, detail="Wait for source deactivation to finish before enabling"
        )
    if row.transition_phase not in ("idle", "activating_new"):
        raise HTTPException(status_code=409, detail="A backend transition is in progress")
    if await provisioning.unresolved_credential_error(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    ):
        raise HTTPException(
            status_code=409,
            detail="An n8n credential operation is pending or requires recovery",
        )
    if source.status != "active" or source.generation != row.source_generation:
        raise HTTPException(
            status_code=409, detail="Source changed; save its current configuration before enabling"
        )
    return row


def _require_n8n_activation_settings(
    n8n: Any, settings: Any, source: ConnectorSource
) -> tuple[str, str, str]:
    """Return ``(api_key, webhook_token, encryption_key)`` or raise the matching HTTP 503.

    The owner kill switch blocks new n8n activation (existing workflows are left as they are).
    Pure: no session access. Probes the encryption key so a missing key fails before any write.
    """
    if not n8n.enabled:
        raise HTTPException(status_code=503, detail="n8n_disabled")
    api_key = n8n.api_key
    if not api_key:
        if connector_owner.is_native_provider(source.provider):
            raise HTTPException(status_code=503, detail="Native activation contract is pending")
        raise HTTPException(status_code=503, detail="n8n provisioning is not configured")
    webhook_token = n8n.webhook_token
    if not webhook_token:
        raise HTTPException(
            status_code=503, detail="Manual trigger authentication is not configured"
        )
    encryption_key = settings.connector_credential_encryption_key.get_secret_value()
    try:
        secret_fingerprint(encryption_key, "connector-key-validation")
    except CredentialEncryptionUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Connector credential encryption is not configured"
        ) from exc
    return api_key, webhook_token, encryption_key


async def _stage_telegram_activation(
    session: AsyncSession,
    request: Request,
    source_id: UUID,
    source: ConnectorSource,
    payload: ActivationRequest,
    *,
    activation_id: UUID,
    encryption_key: str,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> AccessFence:
    """Verify the Telegram bot, then save its encrypted native credential for this activation.

    Releases locks (rollback) before Telegram I/O, re-fences, then stages the credential
    through ``save_native_credential``; the caller commits it. Returns the access fence the
    validation ran under (the retained credential's fence when one exists). Errors: 409 for a
    changed configuration/access/bot/identity, 503/422 for credential or provider failures.
    """
    from modules.connectors.providers.telegram import validate_telegram_scope

    try:
        previous = await provisioning.get_retained_native_credential_snapshot(
            session,
            source_id,
            source_generation=source.generation,
            connector_revision=payload.expected_revision,
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Connector configuration changed; reload before enabling"
        ) from exc
    validation_access_fence = previous.access_fence if previous is not None else access_fence
    if validation_access_fence != access_fence:
        raise HTTPException(status_code=409, detail="Original Telegram access changed")
    if payload.secret_action == "keep":  # noqa: S105  # label/URL/enum value, not a credential
        if previous is None or previous.state != "ready":
            raise HTTPException(status_code=409, detail="A validated Telegram token is required")
        try:
            native_token = decrypt_native_token(encryption_key, previous)
        except CredentialEncryptionUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="Stored Telegram credential is unavailable"
            ) from exc
    else:
        native_token = payload.secret.get_secret_value() if payload.secret else ""
    await session.rollback()
    try:
        verified = await validate_telegram_scope(
            native_token,
            tuple(cast("list[str]", source.configuration.get("telegram_chat_ids", ()))),
            before_request=lambda: _validation_send_fence(
                session,
                request,
                source,
                payload.expected_revision,
                scope=scope,
                access_fence=validation_access_fence,
                native_snapshot=previous,
            ),
        )
    except ProviderRateLimited as exc:
        raise HTTPException(status_code=503, detail="Telegram provider rate limit reached") from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=503, detail="Telegram validation outcome is unknown"
        ) from exc
    except (ValueError, httpx.HTTPError) as exc:
        status = 503 if getattr(exc, "code", None) == "telegram_provider_unavailable" else 422
        detail = (
            "Telegram validation outcome is unknown"
            if status == 503
            else "Telegram bot identity and channel scope could not be verified"
        )
        raise HTTPException(status_code=status, detail=detail) from exc
    if previous is not None and previous.bound_bot_id not in (None, verified.verified_bot_id):
        raise HTTPException(
            status_code=409, detail="A different Telegram bot requires a new source"
        )
    await _validation_send_fence(
        session,
        request,
        source,
        payload.expected_revision,
        scope=scope,
        access_fence=validation_access_fence,
        native_snapshot=previous,
    )
    native_bot_id = verified.verified_bot_id
    ciphertext = encrypt_native_token(
        encryption_key,
        source_id=source_id,
        operation_id=activation_id,
        source_generation=source.generation,
        configuration_revision=payload.expected_revision,
        token=native_token,
        verified_bot_id=native_bot_id,
    )
    try:
        await provisioning.save_native_credential(
            session,
            source_id=source_id,
            operation_id=activation_id,
            source_generation=source.generation,
            connector_revision=payload.expected_revision,
            encrypted_token=ciphertext,
            token_fingerprint=secret_fingerprint(encryption_key, native_token),
            verified_bot_id=native_bot_id,
            validated_at=verified.validated_at,
            access_fence=validation_access_fence,
            expected_native_operation_id=previous.operation_id if previous is not None else None,
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Telegram credential identity or revision changed"
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Telegram bot identity is already reserved"
        ) from exc
    return validation_access_fence


async def _stage_bundle_credentials(
    session: AsyncSession,
    source_id: UUID,
    source: ConnectorSource,
    row: ConnectorProvisioning,
    payload: ActivationRequest,
    *,
    activation_id: UUID,
    encryption_key: str,
    webhook_token: str,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Prepare the collector, manual-trigger and (for header auth) provider credential assignments.

    Called under the connector lock. May mint a collector credential via Ingestion (flush-only)
    and reads existing managed credentials; it never commits. Returns
    ``(required_credentials, credential_intents)`` keyed by slot. Raises ``ValueError`` /
    ``CredentialEncryptionUnavailable``; the caller rolls back and maps them to HTTP 422.
    """
    credential_intents: dict[str, dict[str, object]] = {}
    required_credentials: dict[str, dict[str, object]] = {}

    collector = await provisioning.get_managed_credential(
        session,
        source_id,
        "collector",
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    collector_token: str | None = None
    collector_binding = collector.resolved_binding if collector is not None else None
    collector_scope: Literal["mcp:collect", "ingestion:write"] = (
        "mcp:collect" if source.type == "mcp" else "ingestion:write"
    )
    if not (
        collector is not None
        and collector.state == "ready"
        and collector.credential_id
        and isinstance(collector_binding, dict)
        and collector_binding.get("source_generation") == source.generation
        and collector_binding.get("scope") == collector_scope
    ):
        collector_token = await ingestion.create_collector_credential_in_uow(
            session,
            source_id,
            credential_scope=collector_scope,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
        )
        collector_binding = {
            "source_generation": source.generation,
            "scope": collector_scope,
            "token_fingerprint": secret_fingerprint(encryption_key, collector_token),
        }
    required, intent = prepare_credential_assignment(
        source_id=source_id,
        activation_id=activation_id,
        slot="collector",
        credential_name=f"Umwelt-OS source collector {source_id}",
        header_name="Authorization",
        secret=f"Bearer {collector_token}" if collector_token is not None else None,
        binding=collector_binding if isinstance(collector_binding, dict) else {},
        existing=collector,
        encryption_key=encryption_key,
    )
    required_credentials["collector"] = required
    if intent is not None:
        credential_intents["collector"] = intent

    manual = await provisioning.get_managed_credential(
        session,
        source_id,
        "manual_trigger",
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    required, intent = prepare_credential_assignment(
        source_id=source_id,
        activation_id=activation_id,
        slot="manual_trigger",
        credential_name="Umwelt-OS manual trigger",
        header_name="X-BBD-Webhook-Token",
        secret=webhook_token,
        binding={"binding_kind": "manual_trigger"},
        existing=manual,
        encryption_key=encryption_key,
    )
    required_credentials["manual_trigger"] = required
    if intent is not None:
        credential_intents["manual_trigger"] = intent

    if row.desired_configuration.get("auth_method") == "http_header":
        provider = await provisioning.get_managed_credential(
            session,
            source_id,
            "provider",
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        provider_header = str(row.desired_configuration.get("auth_header_name", ""))
        if payload.secret_action == "keep":  # noqa: S105  # label/URL/enum value, not a credential
            provider_binding = provider.resolved_binding if provider is not None else None
            if (
                provider is None
                or provider.state != "ready"
                or not provider.credential_id
                or not isinstance(provider_binding, dict)
                or provider_binding.get("header_name") != provider_header
            ):
                raise ValueError("Provide a provider credential for the current header binding")
            provider_secret = None
            binding = dict(provider_binding)
        else:
            provider_secret = payload.secret.get_secret_value() if payload.secret else None
            binding = {"binding_kind": "provider"}
        required, intent = prepare_credential_assignment(
            source_id=source_id,
            activation_id=activation_id,
            slot="provider",
            credential_name=f"Umwelt-OS REST source {source_id}",
            header_name=provider_header,
            secret=provider_secret,
            binding=binding,
            existing=provider,
            encryption_key=encryption_key,
        )
        required_credentials["provider"] = required
        if intent is not None:
            credential_intents["provider"] = intent
    return required_credentials, credential_intents


@router.post("/{source_id}/deactivate", response_model=ActivationRead)
async def deactivate_source(
    source_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Pause connector collection and persist the resulting activation state.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await sources.pause_source_for_connector(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if row is None:
        raise HTTPException(status_code=409, detail="No connector provisioning state exists")
    await commit_with_replay(
        session,
        [
            make_source_change(
                source.id, source.generation, source.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


async def _capture_telegram_revocation(
    session: AsyncSession,
    request: Request,
    source_id: UUID,
    source: ConnectorSource,
    row: ConnectorProvisioning,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> NativeCredentialRevocationSnapshot:
    """Capture the Telegram credential revocation snapshot, then restart from the original access.

    Reads the snapshot under locks, rolls back, and re-admits with the snapshot's own fence so
    the later save CAS rechecks the captured Source generation/revision. 409 when the
    credential revision or the original access changed (rolled back first).
    """
    try:
        revocation_snapshot = await provisioning.get_native_credential_revocation_snapshot(
            session,
            source_id,
            source_generation=source.generation,
            connector_revision=row.desired_revision,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram credential revision changed") from exc
    if revocation_snapshot.access_fence != access_fence:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Original credential revocation access changed")
    await session.rollback()
    # Restart from the unchanged original browser/session/access; the save CAS below
    # rechecks the captured Source G/R. No replacement capture is ever taken.
    await _owner_access(session, request, scope, expected=revocation_snapshot.access_fence)
    return revocation_snapshot


@router.delete("/{source_id}/credentials/provider", response_model=ActivationRead)
async def remove_provider_credential(
    source_id: UUID,
    expected_revision: Annotated[int, Query(ge=1)],
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Disable provider authentication and queue or perform a fenced credential deletion.

    Owner-write authorization is enforced by the route dependency. The local
    configuration and delete intent commit before reconciliation; the external
    n8n credential remains until the later delete acknowledgement clears it. Native local-service
    bearer tokens have no external operation and are erased in the same transaction.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await sources.get_connector_source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source is None or row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    if row.desired_revision != expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if (
        source.status != "paused"
        or row.state != "disabled"
        or row.error_code == "deactivation_pending"
    ):
        raise HTTPException(
            status_code=409, detail="Pause the source before removing its provider credential"
        )
    # Detach everything needed before any rollback so no expired ORM row is read afterwards.
    desired = dict(row.desired_configuration)
    desired["auth_method"] = "none"
    desired.pop("auth_header_name", None)
    revocation_snapshot = None
    if source.provider == "telegram":
        revocation_snapshot = await _capture_telegram_revocation(
            session,
            request,
            source_id,
            source,
            row,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    saved = await connector_owner.save_connector_configuration(
        session,
        source,
        expected_revision,
        dict(source.configuration),
        desired,
        allow_paused=True,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    if saved is None:
        raise HTTPException(status_code=409, detail="Connector configuration revision changed")
    saved_source, updated = saved
    updated.state = "disabled"
    if source.provider in collection.HEADER_AUTH_PROVIDERS | collection.REVISIONED_REST_PROVIDERS:
        local_credential = await session.get(
            ConnectorRestCredential, source_id, with_for_update=True
        )
        if local_credential is not None:
            # Native local/workspace-app tokens have no external credential operation to reconcile.
            await session.delete(local_credential)
            updated.credential_revision += 1
    if revocation_snapshot is not None:
        try:
            await provisioning.revoke_native_credential_in_uow(
                session,
                source_id,
                source_generation=saved_source.generation,
                connector_revision=updated.desired_revision,
                release_bot_reservation=True,
                access_fence=revocation_snapshot.access_fence,
                expected_native_operation_id=revocation_snapshot.operation_id,
                multi_workspace_enabled=multi_workspace_enabled,
                scope=scope,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="Telegram credential revision changed"
            ) from exc
        await commit_with_replay(
            session,
            [
                make_source_change(
                    saved_source.id,
                    saved_source.generation,
                    saved_source.status,
                    connector_state=updated.state,
                    scope=scope,
                ),
            ],
            access_fence=access_fence,
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        return await _activation_read(
            session,
            source_id,
            updated,
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
    intent = await provisioning.create_delete_intent_in_uow(
        session,
        source_id,
        "provider",
        updated.desired_revision,
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    if intent is None:
        existing = await provisioning.get_managed_credential(
            session,
            source_id,
            "provider",
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        if existing is not None and existing.credential_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Provider credential operation cannot be changed until its current operation settles",
            )
        updated.error_code = None
    else:
        updated, _, _ = intent
        updated.error_code = "credential_delete_pending"
    await commit_with_replay(
        session,
        [
            make_source_change(
                saved_source.id,
                saved_source.generation,
                saved_source.status,
                connector_state=updated.state,
                scope=scope,
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    return await _activation_read(
        session, source_id, updated, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


def _is_fresh(row: ConnectorProvisioning) -> bool:
    """A row that was never activated may switch backend without a transition."""
    return (
        row.applied_revision == 0
        and row.applied_backend_revision == 0
        and not row.workflow_id
        and row.transition_phase == "idle"
    )


def _choose_backend(
    requested: str | None, row: ConnectorProvisioning, source: ConnectorSource
) -> str:
    """Omitted backend preserves an activated source; a fresh supported source defaults to native."""
    if row.transition_phase == "activating_new" and row.target_backend:
        if requested not in (None, row.target_backend):
            raise HTTPException(
                status_code=409, detail="Finish the pending backend transition first"
            )
        return row.target_backend
    if requested is not None:
        return requested
    if _is_fresh(row) and collection.supports_native(source):
        return "native"
    return row.execution_backend


_NATIVE_ERRORS = {
    "terms_not_accepted": (409, "terms_not_accepted"),
    "invalid_credential": (409, "invalid_credential"),
    "native_unsupported": (422, "This source cannot use the native backend"),
    "local_only_source": (409, {"code": "local_only_source", "message": "local_only_source"}),
    "source_inactive": (409, "Source is inactive or private; it cannot be activated"),
}


async def activate_native(
    session: AsyncSession,
    request: Request,
    scope: WorkspaceContext,
    source: ConnectorSource,
    payload: ActivationRequest,
    *,
    access_fence: AccessFence,
) -> ActivationRead:
    """Activate the native backend in one transaction; no n8n key, credential or workflow is involved.

    Shared by the generic activate route and the built-in source toggle (``builtins.py``). The caller
    supplies a source whose generation and the row's ``desired_revision`` must still match under the lock,
    otherwise 409 "Original activation configuration changed".
    """
    gate = request.app.state.settings.multi_workspace_enabled
    if payload.secret_action == "replace":  # noqa: S105  # label/URL/enum value, not a credential
        raise HTTPException(
            status_code=422, detail="Native credentials are entered with the credential endpoint"
        )
    await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    source_fence, row, slots = await provisioning.lock_connector(
        session,
        source.id,
        provisioning._ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=gate,
        expected_access_fence=access_fence,
    )
    if (
        source_fence is None
        or row is None
        or source_fence.generation != source.generation
        or row.desired_revision != payload.expected_revision
        or row.transition_phase not in ("idle", "activating_new")
        or row.state == "provisioning"
        or any(
            slot.state in {"dispatching", "reconciliation_required", "delete_pending"}
            for slot in slots.values()
        )
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Original activation configuration changed")
    if row.transition_phase == "idle" and row.execution_backend != "native":
        if not _is_fresh(row):
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="Use backend-transition to change the backend"
            )
        row.execution_backend = "native"
        row.backend_revision += 1
    error = await activate_native_in_uow(
        session, source.id, source_fence, row, scope=scope, multi_workspace_enabled=gate
    )
    if error:
        await session.rollback()
        status, detail = _NATIVE_ERRORS[error]
        raise HTTPException(status_code=status, detail=detail)
    await scheduler.clear_collection_block(
        session,
        source.id,
        connector_revision=row.applied_revision,
        credential_revision=row.credential_revision,
        workspace_id=scope.workspace_id,
    )
    await commit_with_replay(
        session,
        [
            make_source_change(
                source.id, source.generation, source.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=gate,
        scope=scope,
    )
    return await _activation_read(
        session, source.id, row, multi_workspace_enabled=gate, scope=scope
    )


async def _drive_transition(
    session: AsyncSession,
    request: Request,
    scope: WorkspaceContext,
    source_id: UUID,
    access_fence: AccessFence,
) -> ActivationRead:
    """Advance a persisted transition with the n8n API when it is configured, then project its state."""
    settings = request.app.state.settings
    gate = settings.multi_workspace_enabled
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    api = N8nApi(n8n.service_url, n8n.api_key, settings) if n8n.api_key else None
    await provisioning.advance_backend_transition(
        session,
        source_id,
        api,
        scope=scope,
        multi_workspace_enabled=gate,
        access_fence=access_fence,
    )
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=gate, scope=scope
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Connector state disappeared during transition")
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=gate, scope=scope
    )


@router.post("/{source_id}/backend-transition", response_model=ActivationRead, status_code=202)
async def start_backend_transition(
    source_id: UUID,
    payload: BackendTransitionRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Invalidate the backend revision, stop admission, fence work, stop the old backend, then activate the target.

    The same call upgrades a stale n8n template (target equals the current backend). Eligibility is checked
    before either backend can be activated. HTTP 202 means the transition is persisted; poll ``/activation``.
    """
    scope = _owner
    gate = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=gate, scope=scope)
    if source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    if payload.target_backend == "native" and not collection.supports_native(source):
        raise HTTPException(status_code=422, detail="This source cannot use the native backend")
    try:
        await provider_terms.require_terms_eligible(session, source)
    except (HTTPException, DomainError) as exc:
        if exc.detail == TERMS_INELIGIBLE:
            raise HTTPException(status_code=409, detail="terms_not_accepted") from exc
        raise
    await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    row = await provisioning.begin_backend_transition_in_uow(
        session,
        source_id,
        payload.expected_revision,
        payload.target_backend,
        scope=scope,
        multi_workspace_enabled=gate,
        access_fence=access_fence,
    )
    await commit_with_replay(
        session,
        [
            make_source_change(
                source.id, source.generation, source.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=gate,
        scope=scope,
    )
    return await _drive_transition(session, request, scope, source_id, access_fence)


@router.post(
    "/{source_id}/backend-transition/resolve", response_model=ActivationRead, status_code=202
)
async def resolve_backend_transition(
    source_id: UUID,
    payload: BackendResolveRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Resolve reconciliation_required: ``retry`` re-sends the idempotent n8n stop; ``confirm_inactive`` is the
    owner attestation, made after checking in n8n that the old workflow is inactive. Neither admits a backend."""
    scope = _owner
    gate = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    await _source(session, source_id, multi_workspace_enabled=gate, scope=scope)
    await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    await provisioning.resolve_backend_transition_in_uow(
        session,
        source_id,
        payload.expected_revision,
        payload.action,
        scope=scope,
        multi_workspace_enabled=gate,
        access_fence=access_fence,
    )
    await commit_with_replay(
        session, [], access_fence=access_fence, multi_workspace_enabled=gate, scope=scope
    )
    return await _drive_transition(session, request, scope, source_id, access_fence)


@router.put("/{source_id}/credentials/native-rest", response_model=ActivationRead)
async def reenter_native_rest_credential(
    source_id: UUID,
    payload: NativeRestCredentialRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Owner re-enters a native REST or local-service header secret (write-only, encrypted, never echoed).

    Bumps the credential revision and lifts an ``invalid_credential`` schedule gate; the secret is bound to
    the current source generation and configuration revision, so a later settings save needs re-entry.
    FHIR and Home Assistant accept only the fixed ``Authorization`` header. Notion, Slack, Mastodon and X use
    the same encrypted write-only credential owner for their fixed bearer-token header; their
    adapters apply the provider-required Bearer scheme when making requests.
    """
    scope = _owner
    settings = request.app.state.settings
    gate = settings.multi_workspace_enabled
    key = settings.connector_credential_encryption_key.get_secret_value()
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=gate, scope=scope)
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=gate, scope=scope
    )
    desired = dict(row.desired_configuration) if row is not None else {}
    keyed_header = collection.KEYED_PROVIDERS.get(
        source.provider or ""
    )  # keyed preset: fixed placement (header | query:<param> | path:<token>)
    workspace_app_header = (
        "Authorization" if source.provider in {"notion", "slack", "mastodon", "x"} else None
    )
    local_header = (
        "Authorization"
        if source.provider in collection.HEADER_AUTH_PROVIDERS
        and desired.get("auth_method") == "http_header"
        and desired.get("auth_header_name") == "Authorization"
        else None
    )
    if (
        source.status != "active"
        or source.type != "api"
        or row is None
        or (
            keyed_header is None
            and local_header is None
            and workspace_app_header is None
            and (
                source.provider is not None
                or desired.get("auth_method") != "http_header"
                or not desired.get("auth_header_name")
            )
        )
    ):
        raise HTTPException(
            status_code=422, detail="This source does not use REST header authentication"
        )
    if row.desired_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if keyed_header is not None:
        from modules.connectors.providers.keyed import validate_key
        from modules.connectors.providers.macro import ProviderPayloadError

        try:
            validate_key(str(source.provider), payload.secret.get_secret_value())
        except ProviderPayloadError as exc:
            raise HTTPException(
                status_code=422, detail="The API key is not acceptable for this provider"
            ) from exc
    header_name = (
        keyed_header or local_header or workspace_app_header or str(desired["auth_header_name"])
    )
    operation_id = uuid7()
    try:
        ciphertext = encrypt_rest_secret(
            key,
            source_id=source_id,
            operation_id=operation_id,
            source_generation=source.generation,
            configuration_revision=payload.expected_revision,
            header_name=header_name,
            secret=payload.secret.get_secret_value(),
        )
        fingerprint = secret_fingerprint(key, payload.secret.get_secret_value())
    except CredentialEncryptionUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Connector credential encryption is not configured"
        ) from exc
    await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    source_fence, row, _slots = await provisioning.lock_connector(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=gate,
        expected_access_fence=access_fence,
    )
    if (
        source_fence is None
        or row is None
        or source_fence.generation != source.generation
        or row.desired_revision != payload.expected_revision
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Original credential configuration changed")
    credential = await session.get(ConnectorRestCredential, source_id, with_for_update=True)
    if credential is None:
        credential = ConnectorRestCredential(source_id=source_id)
        session.add(credential)
    credential.source_generation = source.generation
    credential.configuration_revision = payload.expected_revision
    credential.operation_id = operation_id
    credential.header_name = header_name
    credential.encrypted_secret = ciphertext
    credential.secret_fingerprint = fingerprint
    credential.state = "ready"
    row.credential_revision += 1
    await scheduler.clear_collection_block(
        session,
        source_id,
        credential_revision=row.credential_revision,
        workspace_id=scope.workspace_id,
    )
    await commit_with_replay(
        session,
        [
            make_source_change(
                source.id, source.generation, source.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=gate,
        scope=scope,
    )
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=gate, scope=scope
    )
