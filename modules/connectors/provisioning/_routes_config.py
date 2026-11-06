"""Connector configuration, draft validation, catalog and n8n workflow routes (registered on the shared router)."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator
from core.ids import uuid7
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.connectors import (
    catalog,
    coin_catalog,
    collection,
    provider_terms,
    provisioning,
    quota,
    registry,
)
from modules.connectors import public as connector_owner
from modules.connectors.backends import native_dispatch_supported
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    decrypt_native_token,
    decrypt_rest_secret,
)
from modules.connectors.models import (
    ConnectorProvisioning,
    ConnectorRestCredential,
    ConnectorSchedule,
)
from modules.connectors.providers import rest
from modules.connectors.provisioning._routes_common import (
    ActivationRead,
    ConnectorConfigurationRead,
    ConnectorSettingsRequest,
    DraftValidationRead,
    DraftValidationRequest,
    OwnerRead,
    OwnerWrite,
    Session,
    _activation_read,
    _invalid_detail,
    _owner_access,
    _source,
    _validation_send_fence,
    router,
)
from modules.connectors.public import (
    NativeCredentialSnapshot,
    ProviderRateLimited,
    serialize_source_configuration,
    validate_public_url,
)
from modules.settings.public import module_dependency
from modules.sources import public as sources
from modules.sources.public import ConnectorSource


@router.get("/{source_id}/configuration", response_model=ConnectorConfigurationRead)
async def get_configuration(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ConnectorConfigurationRead:
    """Read a managed connector configuration for an authorized owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    source = await sources.get_connector_source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.type == "mcp":
        row = await provisioning.activation_status(
            session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
        )
        configuration = dict(source.configuration or {})
        configuration.setdefault("schedule_interval_minutes", 60)
        configuration.setdefault("timezone", "Asia/Ho_Chi_Minh")
        return ConnectorConfigurationRead(
            workspace_id=source.workspace_id,
            source_id=source.id,
            source_type=source.type,
            provider=source.provider,
            source_generation=source.generation,
            configuration=configuration,
            expected_revision=row.desired_revision if row is not None else 0,
            auth_method="none",
            auth_header_name=None,
            desired_enabled=bool(row and row.desired_enabled),
            activation_state=row.state if row is not None else "saved_not_active",
            activation_error_code=row.error_code if row is not None else None,
            provider_credential_configured=False,
            provider_credential_state=None,
        )
    snapshot = await connector_owner.get_connector_configuration(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if snapshot.source_type not in registry.SUPPORTED_TYPES:
        raise HTTPException(
            status_code=409, detail="This source has no managed connector configuration"
        )
    return ConnectorConfigurationRead(
        workspace_id=snapshot.workspace_id,
        source_id=snapshot.source_id,
        source_type=snapshot.source_type,
        provider=snapshot.provider,
        source_generation=snapshot.source_generation,
        configuration=snapshot.configuration,
        expected_revision=snapshot.expected_revision,
        auth_method=snapshot.auth_method,
        auth_header_name=snapshot.auth_header_name,
        desired_enabled=snapshot.desired_enabled,
        activation_state=snapshot.activation_state,
        activation_error_code=snapshot.activation_error_code,
        provider_credential_configured=snapshot.provider_credential_configured,
        provider_credential_state=snapshot.provider_credential_state,
        execution_backend=snapshot.execution_backend,
        transition_phase=snapshot.transition_phase,
    )


@router.post("/{source_id}/validate-draft", response_model=DraftValidationRead)
async def validate_draft_configuration(
    source_id: UUID,
    payload: DraftValidationRequest,
    session: Session,
    request: Request,
    _owner: OwnerRead,
) -> DraftValidationRead:
    """Validate a draft and recheck its source, revision, and provider-specific credential mode.

    Retained-token reads lock source, provisioning, managed credentials, and the
    native row before decryption. Locks are released for provider requests, then
    reacquired to ensure the result still describes the requested active draft. FHIR,
    Home Assistant, Notion and Slack drafts require an Authorization bearer header but are never contacted here.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    if payload.expected_source_generation != source.generation:
        raise HTTPException(
            status_code=409, detail="Source generation changed; reload before validating"
        )
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    current_revision = row.desired_revision if row is not None else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail="Connector configuration revision changed; reload before validating",
        )
    _check_draft_auth_mode(source, payload)
    try:
        source_configuration = serialize_source_configuration(source, payload.configuration)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=_invalid_detail("Draft provider configuration is invalid", exc)
        ) from exc
    candidate = source.model_copy(update={"configuration": source_configuration})
    try:
        data = registry.validate(candidate)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_invalid_detail("Draft connector configuration is invalid", exc)
        ) from exc
    bot_id: str | None = None
    scope_verified: bool | None = None
    if source.provider == "telegram":
        bot_id = await _validate_telegram_draft(
            session,
            request,
            source_id,
            source,
            payload,
            candidate,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        scope_verified = True
        checks: tuple[str, ...] = (
            "configuration",
            "provider_identity",
            "provider_scope",
            "receive_mode",
        )
    elif source.provider is not None:
        checks = ("configuration", "provider_identity", "provider_scope", "receive_mode")
        scope_verified = True
    else:
        if payload.auth_method == "http_header" and source.type != "api":
            raise HTTPException(
                status_code=422, detail="Header authentication is supported only for REST sources"
            )
        try:
            await session.rollback()
            await validate_public_url(data["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Draft connector configuration is invalid"
            ) from exc
        checks = ("configuration", "public_url_policy")
    return DraftValidationRead(
        source_id=source_id,
        source_generation=source.generation,
        expected_revision=payload.expected_revision,
        validated_at=datetime.now(UTC),
        validation_status="valid",
        checks=checks,
        verified_bot_id=bot_id,
        scope_verified=scope_verified,
    )


def _check_draft_auth_mode(source: ConnectorSource, payload: DraftValidationRequest) -> None:
    """Reject draft authentication modes that do not match the provider or source type (HTTP 422).

    Pure validation: no session access. Mastodon's bearer token is optional, so an explicit
    ``none`` is accepted for it. Header-auth providers must use the ``Authorization`` header.
    """
    expected_auth = (
        "telegram_bot_token"
        if source.provider == "telegram"
        else "http_header"
        if source.provider
        in collection.HEADER_AUTH_PROVIDERS | collection.REVISIONED_REST_PROVIDERS
        else "none"
        if source.provider
        else None
    )
    if source.provider == "mastodon" and payload.auth_method == "none":
        expected_auth = (
            "none"  # Mastodon's bearer token is optional: public reads need no credential.
        )
    if expected_auth is not None and payload.auth_method != expected_auth:
        raise HTTPException(
            status_code=422, detail="Authentication mode does not match the provider"
        )
    if (
        expected_auth == "http_header"
        and source.provider
        in collection.HEADER_AUTH_PROVIDERS | collection.REVISIONED_REST_PROVIDERS
        and payload.auth_header_name != "Authorization"
    ):
        raise HTTPException(
            status_code=422, detail="Provider bearer credentials require the Authorization header"
        )
    if source.provider is None and payload.auth_method == "telegram_bot_token":
        raise HTTPException(
            status_code=422, detail="Telegram authentication requires a Telegram source"
        )
    if payload.auth_method == "http_header" and source.type != "api":
        raise HTTPException(
            status_code=422, detail="Header authentication is supported only for REST sources"
        )


async def _telegram_validation_token(
    request: Request,
    payload: DraftValidationRequest,
    previous: NativeCredentialSnapshot | None,
) -> str:
    """Return the bot token to validate: the submitted replacement or the decrypted retained one.

    Raises 409 when a retained token is required but not ready and 503 when it cannot be
    decrypted. Does not commit; the caller rolls back before provider I/O.
    """
    if payload.secret_action == "replace":  # noqa: S105  # label/URL/enum value, not a credential
        return payload.secret.get_secret_value() if payload.secret is not None else ""
    if previous is None or previous.state != "ready":
        raise HTTPException(status_code=409, detail="A validated Telegram token is required")
    key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
    try:
        return decrypt_native_token(key, previous)
    except CredentialEncryptionUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Stored Telegram credential is unavailable"
        ) from exc


async def _validate_telegram_draft(
    session: AsyncSession,
    request: Request,
    source_id: UUID,
    source: ConnectorSource,
    payload: DraftValidationRequest,
    candidate: ConnectorSource,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> str:
    """Verify the Telegram bot identity and chat scope for a draft; return the verified bot id.

    Reads the retained credential under locks, rolls back before contacting Telegram, then
    re-fences (``_validation_send_fence``) so the result still describes the requested draft.
    Errors: 409 for a changed configuration/access/bot, 422/503 for provider verification failures.
    """
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
            status_code=409, detail="Connector configuration changed; reload before validating"
        ) from exc
    validation_access_fence = previous.access_fence if previous is not None else access_fence
    if validation_access_fence != access_fence:
        raise HTTPException(status_code=409, detail="Original Telegram access changed")
    token = await _telegram_validation_token(request, payload, previous)
    await session.rollback()
    from modules.connectors.providers.telegram import validate_telegram_scope

    try:
        verified = await validate_telegram_scope(
            token,
            tuple(cast("list[str]", candidate.configuration["telegram_chat_ids"])),
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
    await _validation_send_fence(
        session,
        request,
        source,
        payload.expected_revision,
        scope=scope,
        access_fence=validation_access_fence,
        native_snapshot=previous,
    )
    if previous is not None and previous.bound_bot_id not in (None, verified.verified_bot_id):
        raise HTTPException(
            status_code=409, detail="A different Telegram bot requires a new source"
        )
    return verified.verified_bot_id


class CoinSearchRead(BaseModel):
    """CoinGecko catalog matches; IDs are the source configuration keys."""

    items: list[coin_catalog.Coin]


@router.get("/{source_id}/coingecko/coins", response_model=CoinSearchRead)
async def search_coingecko_coins(
    source_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerRead,
    q: str = Query(min_length=2, max_length=80),
) -> CoinSearchRead:
    """Search CoinGecko's public catalog for one owner source with a current Demo key.

    A cache miss uses the connector terms gate and commits one shared quota debit immediately
    before the fixed-host request. Source, access, terms, and credential revisions are checked
    again after DNS resolution; no key or provider payload is written to the quota ledger.
    """
    settings = request.app.state.settings
    multi = settings.multi_workspace_enabled
    scope = _owner
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, scope=scope, multi_workspace_enabled=multi)
    if source.provider != "coingecko" or source.status not in {"active", "paused"}:
        raise HTTPException(
            status_code=422, detail="Source is not an editable CoinGecko datasource"
        )
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi, scope=scope
    )
    credential = await session.get(ConnectorRestCredential, source_id)
    if (
        row is None
        or credential is None
        or credential.state != "ready"
        or credential.encrypted_secret is None
        or credential.header_name != "x-cg-demo-api-key"
        or credential.source_generation != source.generation
        or credential.configuration_revision != row.desired_revision
    ):
        raise HTTPException(
            status_code=409, detail="Save a CoinGecko Demo API key before searching coins"
        )
    terms_revision = await provider_terms.require_terms_eligible(session, source)
    source_generation, configuration_revision = source.generation, row.desired_revision
    credential_revision, operation_id = row.credential_revision, credential.operation_id
    ciphertext = credential.encrypted_secret
    try:
        encryption_key = settings.connector_credential_encryption_key.get_secret_value()
        secret = decrypt_rest_secret(
            encryption_key,
            ciphertext,
            source_id=source_id,
            operation_id=operation_id,
            source_generation=source_generation,
            configuration_revision=configuration_revision,
            header_name="x-cg-demo-api-key",
        )
    except CredentialEncryptionUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Stored CoinGecko key is unavailable; re-enter it"
        ) from exc
    fingerprint = quota.credential_fingerprint(secret, deployment_key=encryption_key)
    await (
        session.rollback()
    )  # Release the owner/Source read transaction before DNS or provider I/O.

    async def before_send() -> None:
        """Recheck the original owner, source, terms and key then debit the shared budget before wire send."""
        try:
            await _owner_access(session, request, scope, expected=access_fence)
            current = await _source(session, source_id, scope=scope, multi_workspace_enabled=multi)
            current_row = await provisioning.activation_status(
                session, source_id, multi_workspace_enabled=multi, scope=scope
            )
            current_credential = await session.get(ConnectorRestCredential, source_id)
            if (
                current.status not in {"active", "paused"}
                or current.provider != "coingecko"
                or current.generation != source_generation
                or current_row is None
                or current_row.desired_revision != configuration_revision
                or current_row.credential_revision != credential_revision
                or current_credential is None
                or current_credential.state != "ready"
                or current_credential.operation_id != operation_id
                or current_credential.header_name != "x-cg-demo-api-key"
            ):
                raise HTTPException(
                    status_code=409, detail="CoinGecko source or key changed; search again"
                )
            if await provider_terms.require_terms_eligible(session, current) != terms_revision:
                raise HTTPException(status_code=409, detail="CoinGecko terms changed; search again")
            await quota.debit_send(
                session,
                provider_id="coingecko",
                workspace_id=scope.workspace_id,
                request_id=uuid7(),
                admission_token=uuid7(),
                attempt=0,
                send_sequence=1,
                request_target="coingecko:coins/list",
                credential=fingerprint,
            )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise

    try:
        coins = await coin_catalog.load_catalog(secret, before_send)
    except quota.QuotaDeferred as exc:
        raise HTTPException(
            status_code=429, detail="CoinGecko request budget is exhausted; try again later"
        ) from exc
    except ProviderRateLimited as exc:
        await quota.defer_provider(
            session, provider_id="coingecko", until=exc.next_eligible_at, credential=fingerprint
        )
        await session.commit()
        raise HTTPException(
            status_code=429, detail="CoinGecko is rate limiting catalog requests; try again later"
        ) from exc
    except rest.ProviderHttpError as exc:
        detail = (
            "CoinGecko rejected the Demo API key"
            if exc.status_code in {401, 403}
            else "CoinGecko coin catalog is unavailable"
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    except (
        rest.CollectionIncomplete,
        rest.UnsafeDestination,
        ValueError,
        TimeoutError,
        httpx.HTTPError,
    ) as exc:
        raise HTTPException(
            status_code=502, detail="CoinGecko coin catalog is unavailable"
        ) from exc
    finally:
        await session.rollback()
    return CoinSearchRead(items=coin_catalog.search_catalog(coins, q))


@router.get("/catalog")
async def get_catalog(
    session: Session, request: Request, _owner: OwnerRead
) -> list[catalog.CatalogEntry]:
    """Return connector catalog entries to an authorized owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    await _owner_access(session, request, scope)
    return list(catalog.list_catalog())


class N8nWorkflowRead(BaseModel):
    """One n8n-backed source with its workflow binding, provisioning state and collection health."""

    source_id: UUID
    source_name: str
    source_type: str
    provider: str | None
    workflow_id: str | None
    state: str
    error_code: str | None
    transition_phase: str | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    collected_at: datetime | None
    next_due_at: datetime | None


@router.get(
    "/n8n/workflows",
    response_model=list[N8nWorkflowRead],
    dependencies=[Depends(module_dependency("connectors"))],
)
async def list_n8n_workflows(
    session: Session, request: Request, _owner: OwnerRead
) -> list[N8nWorkflowRead]:
    """List up to 200 of the owner workspace's sources whose execution backend is n8n.

    Owner-only (members get 403) and workspace-scoped: provisioning rows are restricted to Source ids
    the Sources owner projects for this workspace, and names/health come from the Sources public read
    projection. Read-only; no n8n API call, no locks. Ordered by source id for a stable page.
    """
    scope = _owner
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    flag = request.app.state.settings.multi_workspace_enabled
    in_scope = sources.ingestion_lifecycle_projection(scope=scope).subquery()
    rows = list(
        (
            await session.scalars(
                select(ConnectorProvisioning)
                .where(
                    ConnectorProvisioning.execution_backend == "n8n",
                    ConnectorProvisioning.source_id.in_(select(in_scope.c.id)),
                )
                .order_by(ConnectorProvisioning.source_id)
                .limit(200)
            )
        ).all()
    )
    status = await sources.list_connector_status(
        session, [row.source_id for row in rows], scope=scope, multi_workspace_enabled=flag
    )
    due = (
        dict(
            (
                await session.execute(
                    select(ConnectorSchedule.source_id, ConnectorSchedule.next_due_at).where(
                        ConnectorSchedule.source_id.in_(list(status)),
                        ConnectorSchedule.workspace_id == scope.workspace_id,
                    )
                )
            )
            .tuples()
            .all()
        )
        if status
        else {}
    )
    return [
        N8nWorkflowRead(
            source_id=row.source_id,
            source_name=status[row.source_id].name,
            source_type=status[row.source_id].type,
            provider=status[row.source_id].provider,
            workflow_id=row.workflow_id,
            state=row.state,
            error_code=row.error_code,
            transition_phase=row.transition_phase,
            last_success_at=status[row.source_id].last_success_at,
            last_error_at=status[row.source_id].last_error_at,
            last_error_code=status[row.source_id].last_error_code,
            collected_at=status[row.source_id].collected_at,
            next_due_at=due.get(row.source_id),
        )
        for row in rows
        if row.source_id in status
    ]


@router.put("/{source_id}/configuration", response_model=ActivationRead)
async def put_configuration(
    source_id: UUID,
    payload: ConnectorSettingsRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Save revision-checked desired settings without starting a new activation.

    Reconciles/cancels prior activation and workflow intent as needed, commits
    the saved configuration, and returns the resulting activation projection. FHIR and
    Home Assistant, Notion and Slack use a fixed Authorization bearer header plus native endpoint
    or scope fields.
    Owner-write authorization is enforced by the route dependency.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    # Local-service providers fetch private addresses; only the instance operator's LAN is in scope.
    # Collection re-checks as the backstop, but fail clearly at save time.
    if source.provider in collection.HEADER_AUTH_PROVIDERS and not is_instance_operator(
        request.state.auth_session
    ):
        raise HTTPException(
            status_code=403,
            detail="Local-service connectors are restricted to the instance operator",
        )
    pending = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if (
        pending is not None
        and pending.state == "disabled"
        and pending.error_code == "deactivation_pending"
    ):
        raise HTTPException(
            status_code=409, detail="Wait for source deactivation to finish before saving"
        )
    if pending is not None and pending.transition_phase != "idle":
        raise HTTPException(
            status_code=409, detail="Wait for the backend transition to finish before saving"
        )
    expected_auth = (
        "telegram_bot_token"
        if source.provider == "telegram"
        else "http_header"
        if source.provider
        in collection.HEADER_AUTH_PROVIDERS | collection.REVISIONED_REST_PROVIDERS
        else "none"
        if source.provider
        else None
    )
    if source.provider == "mastodon" and payload.auth_method == "none":
        expected_auth = (
            "none"  # Mastodon's bearer token is optional: public reads need no credential.
        )
    if expected_auth is not None and payload.auth_method != expected_auth:
        raise HTTPException(
            status_code=422, detail="Authentication mode does not match the provider"
        )
    if (
        expected_auth == "http_header"
        and source.provider
        in collection.HEADER_AUTH_PROVIDERS | collection.REVISIONED_REST_PROVIDERS
        and payload.auth_header_name != "Authorization"
    ):
        raise HTTPException(
            status_code=422, detail="Provider bearer credentials require the Authorization header"
        )
    if source.provider is None and payload.auth_method == "telegram_bot_token":
        raise HTTPException(
            status_code=422, detail="Telegram authentication requires a Telegram source"
        )
    if payload.auth_method == "http_header" and source.type != "api":
        raise HTTPException(
            status_code=422, detail="Header authentication is supported only for REST sources"
        )
    try:
        source_configuration = serialize_source_configuration(source, payload.configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    desired_configuration = dict(source_configuration)
    desired_configuration["auth_method"] = payload.auth_method
    if payload.auth_header_name:
        desired_configuration["auth_header_name"] = payload.auth_header_name
    candidate = source.model_copy(update={"configuration": source_configuration})
    try:
        data = registry.validate(candidate)
        if "url" in data:
            await session.rollback()
            await validate_public_url(data["url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    saved_result = await connector_owner.save_connector_configuration(
        session,
        source,
        payload.expected_revision,
        source_configuration,
        desired_configuration,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    if saved_result is None:
        raise HTTPException(
            status_code=409,
            detail="Source or connector revision changed while configuration was validated",
        )
    saved, row = saved_result
    unresolved = await provisioning.unresolved_credential_error(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    row.state = "reconciliation_required" if unresolved else "saved_not_active"
    row.error_code = unresolved
    await commit_with_replay(
        session,
        [
            make_source_change(
                saved.id, saved.generation, saved.status, connector_state=row.state, scope=scope
            ),
        ],
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        scope=scope,
    )
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


@router.post("/{source_id}/validate", response_model=ActivationRead)
async def validate_configuration(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ActivationRead:
    """Validate saved connector settings against configuration and URL policy.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    source = await _source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    try:
        data = registry.validate(source)
        if "url" in data:
            await session.rollback()
            await validate_public_url(data["url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=_invalid_detail("Connector configuration is invalid", exc)
        ) from exc
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if row is None:
        raise HTTPException(
            status_code=409, detail="Save connector configuration before validation"
        )
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )


@router.get("/{source_id}/activation", response_model=ActivationRead)
async def get_activation(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ActivationRead:
    """Read activation status for an authorized source owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    source = await _source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    row = await provisioning.activation_status(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if row is None:
        # No row yet: report the backend a first save would pick (mirrors public.py), not a fixed n8n default.
        return ActivationRead(
            workspace_id=scope.workspace_id,
            source_id=source_id,
            desired_revision=0,
            applied_revision=0,
            state="saved_not_active",
            error_code=None,
            execution_backend="native"
            if native_dispatch_supported(source.type, source.provider)
            else "n8n",
        )
    return await _activation_read(
        session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
