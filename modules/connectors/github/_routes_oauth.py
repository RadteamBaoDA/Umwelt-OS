"""Owner-session GitHub App OAuth routes: start, status, sync reset, reconnect, refresh and callback."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.errors import DomainError
from core.ids import uuid7
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import provisioning, registry
from modules.connectors.github import oauth
from modules.connectors.github._routes_common import (
    RECOVERY_GRACE,
    OwnerRead,
    OwnerWrite,
    ReconnectReconciliationRequest,
    ResetSyncRequest,
    Session,
    StartRequest,
    WorkspaceRead,
    WorkspaceWrite,
    _admit,
    _configured,
    _flag,
    _FlowCtx,
    _mark_github_reconciliation,
    _publish,
    _require_current_owner_session,
    router,
)
from modules.connectors.github.schemas import GitHubCursor, project_github_source_config
from modules.connectors.models import (
    GithubOAuthAttempt,
    GithubOAuthCoordinator,
    GithubOAuthGrant,
    GithubOAuthOperation,
    GithubSourceHint,
    GithubSyncReset,
)
from modules.connectors.provisioning import activation_status
from modules.sources import public as sources


@router.post("/{source_id}/github/oauth/start")
async def start_github_authorization(
    source_id: str,
    payload: StartRequest,
    session: Session,
    request: Request,
    owner: OwnerWrite,
    scope: WorkspaceWrite,
) -> dict[str, str]:
    """Create an expiring, owner-session and browser-cookie-bound S256 PKCE attempt.

    The attempt stores only hashes of state and browser nonce; the verifier is encrypted with
    the connector key and fenced to the source generation and saved configuration revision.
    The Source must belong to the selected workspace; access is admitted before any read.
    """
    _configured(request.app.state.settings)
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    flag = _flag(request)
    fence = await _admit(session, request, scope)
    source = await sources.get_connector_source(
        session, parsed_id, scope=scope, multi_workspace_enabled=flag
    )
    if (
        source is None
        or source.provider != "github"
        or source.status != "active"
        or source.generation != payload.expected_source_generation
    ):
        raise HTTPException(
            status_code=409, detail="GitHub source changed; reload before connecting"
        )
    row = await activation_status(session, parsed_id, scope=scope, multi_workspace_enabled=flag)
    revision = row.desired_revision if row else 0
    if revision != payload.expected_revision:
        raise HTTPException(
            status_code=409, detail="GitHub configuration changed; reload before connecting"
        )
    try:
        project_github_source_config(source.configuration)
        registry.validate(source)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="Save a valid GitHub repository and read scope first"
        ) from exc
    settings: Settings = request.app.state.settings
    key = settings.connector_credential_encryption_key.get_secret_value()
    state, verifier, browser_nonce = oauth.new_pkce_pair()
    attempt_id = uuid7()
    grant = await session.get(GithubOAuthGrant, parsed_id)
    token_revision = grant.token_revision if grant else 0
    encrypted = oauth._token_cipher(
        key, parsed_id, attempt_id, source.generation, revision, {"verifier": verifier}
    )
    session.add(
        GithubOAuthAttempt(
            id=attempt_id,
            source_id=parsed_id,
            session_hash=owner.token_hash,
            state_hash=oauth.digest(state),
            browser_hash=oauth.digest(browser_nonce),
            encrypted_verifier=encrypted,
            source_generation=source.generation,
            configuration_revision=revision,
            expected_token_revision=token_revision,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    request.session["github_oauth"] = {
        "state_hash": oauth.digest(state),
        "browser_nonce": browser_nonce,
    }
    await _publish(session, request, scope, fence)
    return {"authorization_url": oauth.authorization_url(settings, state=state, verifier=verifier)}


async def _status_cursor(
    session: AsyncSession,
    source: Any,
    grant: GithubOAuthGrant | None,
    scope: WorkspaceContext,
    flag: bool,
) -> tuple[GitHubCursor | None, bool, str | None]:
    """Load the stored GitHub cursor and report whether it still matches the live binding fence.

    Read-only: fetches the binding fence (when a grant exists) and then the raw source cursor in
    that order. A cursor that fails to parse or differs from the fence/grant revision is dropped
    and reported invalid.

    Returns:
        ``(cursor, cursor_invalid, scope_sha256)``.
    """
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion

    cursor = None
    cursor_invalid = False
    scope_sha256 = None
    current_fence = None
    if grant is not None:
        current_fence = await connectors.get_github_binding_fence(
            session,
            source.id,
            source_generation=source.generation,
            connector_revision=grant.configuration_revision,
            scope=scope,
            multi_workspace_enabled=flag,
        )
        scope_sha256 = current_fence.scope_sha256 if current_fence is not None else None

    raw_cursor = await ingestion.get_source_cursor(
        session, source.id, scope=scope, multi_workspace_enabled=flag
    )
    if raw_cursor is not None:
        try:
            cursor = GitHubCursor.model_validate_json(raw_cursor)
            if (
                cursor.source_id != source.id
                or cursor.source_generation != source.generation
                or cursor.connector_revision != (grant.configuration_revision if grant else -1)
                or current_fence is None
                or cursor.repository_id != current_fence.repository_id
                or cursor.installation_id != current_fence.installation_id
                or cursor.app_id != current_fence.app_id
                or cursor.binding_revision != current_fence.binding_revision
                or cursor.scope_sha256 != current_fence.scope_sha256
            ):
                cursor = None
                cursor_invalid = True
        except ValueError:
            cursor_invalid = True
    return cursor, cursor_invalid, scope_sha256


@router.get("/{source_id}/github/status")
async def github_connection_status(
    source_id: str,
    session: Session,
    request: Request,
    _owner: OwnerRead,
    scope: WorkspaceRead,
) -> dict[str, object]:
    """Return a safe Settings grant, scan, and uncertainty summary without exposing token material.

    The Source must belong to the selected workspace (otherwise 404); the OAuth coordinator and
    operation shown are the actor's own and the operation must carry this workspace.
    """
    flag = _flag(request)
    await _admit(session, request, scope, lock=False)
    source_uuid = UUID(source_id)
    source = await sources.get_connector_source(
        session, source_uuid, scope=scope, multi_workspace_enabled=flag
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    grant = await session.get(GithubOAuthGrant, source_uuid)
    last_reset = await session.scalar(
        select(GithubSyncReset)
        .where(GithubSyncReset.source_id == source_uuid)
        .order_by(GithubSyncReset.reset_at.desc())
        .limit(1)
    )
    cursor, cursor_invalid, scope_sha256 = None, False, None
    if source.provider == "github":
        cursor, cursor_invalid, scope_sha256 = await _status_cursor(
            session, source, grant, scope, flag
        )
    history_days = 90
    if source.provider == "github":
        try:
            history_days = project_github_source_config(source.configuration).github_history_days
        except ValidationError:
            # A draft whose repository scope is not chosen yet has no valid config; the status poll must still answer.
            pass
    sync_status = {
        "history_days": history_days,
        "scope_sha256": scope_sha256,
        "last_reset_at": last_reset.reset_at if last_reset else None,
        "gap_recorded": last_reset is not None,
        "cursor_invalid": cursor_invalid,
        "unverified_hints": await session.scalar(
            select(func.count())
            .select_from(GithubSourceHint)
            .where(
                GithubSourceHint.source_id == source_uuid,
                GithubSourceHint.state == "visibility_unverified",
            )
        )
        or 0,
        "reconcile_exhausted": await session.scalar(
            select(func.count())
            .select_from(GithubSourceHint)
            .where(
                GithubSourceHint.source_id == source_uuid,
                GithubSourceHint.intent == "reconcile",
                GithubSourceHint.reconcile_page > 100,
                GithubSourceHint.state == "needs_attention",
            )
        )
        or 0,
        "resources": [
            {
                "resource": item.resource,
                "phase": item.phase,
                "page": item.page,
                "next_page": item.next_page,
                "sweep_revision": item.sweep_revision,
                "floor": item.floor,
                "upper": item.upper,
                "completed_upper": item.completed_upper,
                "completed_sweep_revision": item.completed_sweep_revision,
                "incomplete": item.phase == "exhausted",
            }
            for item in cursor.resources
        ]
        if cursor is not None
        else [],
    }
    coordinator = await session.get(GithubOAuthCoordinator, scope.user_id)
    operation = (
        await session.scalar(
            select(GithubOAuthOperation).where(
                GithubOAuthOperation.operation_id == coordinator.operation_id,
                GithubOAuthOperation.workspace_id == scope.workspace_id,
                GithubOAuthOperation.owner_id == scope.user_id,
            )
        )
        if coordinator and coordinator.operation_id
        else None
    )
    recovery_available = bool(
        coordinator is not None
        and coordinator.operation_id is not None
        and (
            coordinator.state == "reconciliation_required"
            or coordinator.updated_at <= datetime.now(UTC) - RECOVERY_GRACE
            and coordinator.state in {"authorizing", "refreshing", "revoking"}
        )
    )
    operation_details = {
        "operation_kind": operation.operation_kind if operation else None,
        "operation_source_id": str(operation.source_id)
        if operation and operation.source_id
        else None,
        "operation_source_generation": operation.source_generation if operation else None,
        "operation_configuration_revision": operation.configuration_revision if operation else None,
        "operation_state": operation.state if operation else None,
    }
    if grant is None:
        return {
            "state": "not_connected",
            "expires_at": None,
            "error_code": None,
            "coordinator_state": coordinator.state if coordinator else "idle",
            "operation_id": str(coordinator.operation_id)
            if coordinator and coordinator.operation_id
            else None,
            "coordinator_error_code": coordinator.error_code if coordinator else None,
            "recovery_available": recovery_available,
            "sync": sync_status,
            **operation_details,
        }
    return {
        "state": grant.state,
        "expires_at": grant.expires_at,
        "error_code": grant.error_code,
        "coordinator_state": coordinator.state if coordinator else "idle",
        "operation_id": str(coordinator.operation_id)
        if coordinator and coordinator.operation_id
        else None,
        "recovery_available": recovery_available,
        "sync": sync_status,
        **operation_details,
    }


@router.post("/{source_id}/github/sync/reset")
async def reset_github_sync(
    source_id: str,
    payload: ResetSyncRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
    scope: WorkspaceWrite,
) -> dict[str, str]:
    """Restart selected GitHub resource sweeps after matching reviewed scope and idle ingestion state."""
    from modules.connectors import public as connectors

    await connectors.reset_github_collection_cursor(
        session,
        source_id=UUID(source_id),
        source_generation=payload.expected_source_generation,
        connector_revision=payload.expected_connector_revision,
        expected_scope_sha256=payload.expected_scope_sha256,
        scope=scope,
        multi_workspace_enabled=_flag(request),
    )
    return {"status": "reset"}


@router.post("/{source_id}/github/reconcile/reconnect")
async def acknowledge_github_reconnect(
    source_id: str,
    payload: ReconnectReconciliationRequest,
    session: Session,
    request: Request,
    owner: OwnerWrite,
    scope: WorkspaceWrite,
) -> dict[str, str]:
    """Acknowledge one exact owner-wide auth/refresh tombstone even if its original source is paused or deleted.

    The path source is only the editor from which the owner initiated recovery. Stored origin identity
    authorizes no action on that source; current grants are fenced only when their recorded revisions
    still match the unresolved operation. The operation must carry the selected workspace and actor.
    """
    UUID(source_id)
    flag = _flag(request)
    actor = scope.user_id
    fence = await _admit(session, request, scope)
    operation = await session.scalar(
        select(GithubOAuthOperation).where(
            GithubOAuthOperation.operation_id == payload.operation_id,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
    )
    if (
        operation is None
        or operation.owner_id != actor
        or operation.operation_kind not in {"authorization", "refresh"}
        or operation.source_id is None
    ):
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="GitHub recovery operation is unavailable; reload connection status",
        )
    source_fence = await sources.lock_source(
        session,
        operation.source_id,
        scope=scope,
        multi_workspace_enabled=flag,
        expected_access_fence=fence,
    )
    grant = await session.scalar(
        select(GithubOAuthGrant)
        .where(GithubOAuthGrant.source_id == operation.source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == payload.operation_id,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        operation is None
        or operation.owner_id != actor
        or coordinator is None
        or coordinator.operation_id != payload.operation_id
        or coordinator.state not in {"reconciliation_required", "authorizing", "refreshing"}
    ):
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="GitHub reconciliation operation changed; reload connection status",
        )
    if (
        coordinator.state != "reconciliation_required"
        and coordinator.updated_at > datetime.now(UTC) - RECOVERY_GRACE
    ):
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="GitHub operation is still within its recovery window"
        )
    if operation.state not in {"in_progress", "reconciliation_required"}:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="GitHub recovery operation was already resolved"
        )
    recovery_error = (
        coordinator.error_code
        or operation.error_code
        or (
            "authorization_outcome_unknown"
            if operation.operation_kind == "authorization"
            else "token_refresh_outcome_unknown"
        )
    )
    await _require_current_owner_session(request, session, owner)
    if (
        grant is not None
        and source_fence is not None
        and (grant.source_generation, grant.configuration_revision, grant.token_revision)
        == (operation.source_generation, operation.configuration_revision, operation.token_revision)
        and grant.state in {"ready", "refreshing"}
        and (
            operation.operation_kind != "refresh"
            or grant.refresh_operation_id == payload.operation_id
        )
    ):
        grant.state = "reconciliation_required"
        grant.error_code = recovery_error
    coordinator.state, coordinator.operation_id = "idle", None
    coordinator.error_code = recovery_error
    operation.state = "acknowledged"
    operation.error_code = recovery_error
    operation.resolved_at = datetime.now(UTC)
    # Keep nonsecret operation origin and outcome after releasing the owner-wide coordinator.
    await _publish(session, request, scope, fence)
    return {"state": "reconnect_required"}


@router.post("/{source_id}/github/oauth/refresh")
async def refresh_github_authorization(
    source_id: str,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
    scope: WorkspaceWrite,
) -> dict[str, str]:
    """Rotate a source's expiring tokens under the actor-wide coordinator and revision fences.

    Phase 1 captures the access fence and commits the in-progress operation (with its real workspace
    and actor). Phase 3, after the provider call, re-admits that ORIGINAL fence before relocking
    Source, provisioning, grant and coordinator; a lost fence marks the operation uncertain and
    publishes nothing.
    """
    _configured(request.app.state.settings)
    source_uuid = UUID(source_id)
    flag = _flag(request)
    actor = scope.user_id
    fence = await _admit(session, request, scope)
    ctx = _FlowCtx(request, session, _owner, scope, flag, actor, fence, request.app.state.settings)
    (
        refresh_token,
        operation_id,
        expected_token_revision,
        expected_grant_operation,
    ) = await _begin_refresh(ctx, source_uuid)
    try:
        await _require_current_owner_session(request, session, _owner)
        tokens = await oauth.refresh_github_grant(request.app.state.settings, refresh_token)
        await _require_current_owner_session(request, session, _owner)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_uuid,
            operation_id,
            "token_refresh_outcome_unknown",
        )
        raise HTTPException(
            status_code=503, detail="GitHub token refresh failed; reconnect to restore collection"
        ) from None
    try:
        await _admit(session, request, scope, expected=fence)
        source_fence, current_provisioning, _no_slots = await provisioning.lock_connector(
            session,
            source_uuid,
            (),
            scope=scope,
            multi_workspace_enabled=flag,
            expected_access_fence=fence,
        )
    except (DomainError, HTTPException):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_uuid,
            operation_id,
            "token_refresh_outcome_unknown",
        )
        raise
    source = (
        await sources.get_connector_source(
            session, source_uuid, scope=scope, multi_workspace_enabled=flag
        )
        if source_fence is not None
        else None
    )
    grant = await session.scalar(
        select(GithubOAuthGrant)
        .where(GithubOAuthGrant.source_id == source_uuid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    if (
        source is None
        or source.provider != "github"
        or source.status != "active"
        or grant is None
        or current_provisioning is None
        or source.generation != grant.source_generation
        or current_provisioning.desired_revision != grant.configuration_revision
        or grant.token_revision != expected_token_revision
        or grant.operation_id != expected_grant_operation
        or grant.state != "refreshing"
        or grant.refresh_operation_id != operation_id
        or coordinator is None
        or coordinator.operation_id != operation_id
    ):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_uuid,
            operation_id,
            "stale_refresh_outcome_unknown",
        )
        raise HTTPException(status_code=409, detail="GitHub source changed during refresh")
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == operation_id,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if (
        operation is None
        or operation.owner_id != actor
        or operation.operation_kind != "refresh"
        or operation.state != "in_progress"
    ):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_uuid,
            operation_id,
            "stale_refresh_outcome_unknown",
        )
        raise HTTPException(status_code=409, detail="GitHub refresh operation changed")
    # This must remain the final awaited authorization check after every lifecycle lock.
    try:
        await _require_current_owner_session(request, session, _owner)
    except (DomainError, HTTPException):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_uuid,
            operation_id,
            "token_refresh_outcome_unknown",
        )
        raise
    grant.operation_id = operation_id
    grant.token_revision += 1
    grant.refresh_operation_id = None
    grant.encrypted_tokens = oauth._token_cipher(
        request.app.state.settings.connector_credential_encryption_key.get_secret_value(),
        source_uuid,
        operation_id,
        grant.source_generation,
        grant.configuration_revision,
        tokens,
    )
    grant.expires_at = tokens["access_expires_at"]
    grant.state, grant.error_code, grant.validated_at = "ready", None, datetime.now(UTC)
    coordinator.state, coordinator.operation_id = "idle", None
    coordinator.error_code = None
    operation.state = "completed"
    operation.error_code = None
    operation.resolved_at = datetime.now(UTC)
    await _publish(session, request, scope, fence)
    return {"state": "ready"}


def _stage_refresh_operation(
    session: AsyncSession,
    scope: WorkspaceContext,
    actor: int,
    source_uuid: UUID,
    grant: GithubOAuthGrant,
    coordinator: GithubOAuthCoordinator,
    operation_id: UUID,
) -> None:
    """Mark the coordinator and grant as refreshing and add the in-progress refresh operation row.

    Mutates the already locked ``grant``/``coordinator`` and adds the operation to ``session``; the
    caller commits through the replay gate. Performs no I/O and raises nothing.
    """
    coordinator.state, coordinator.operation_id = "refreshing", operation_id
    coordinator.error_code = None
    session.add(
        GithubOAuthOperation(
            operation_id=operation_id,
            workspace_id=scope.workspace_id,
            owner_id=actor,
            operation_kind="refresh",
            source_id=source_uuid,
            source_generation=grant.source_generation,
            configuration_revision=grant.configuration_revision,
            token_revision=grant.token_revision,
            state="in_progress",
        )
    )
    grant.refresh_operation_id = operation_id
    grant.state = "refreshing"


async def _begin_refresh(ctx: _FlowCtx, source_uuid: UUID) -> tuple[str, UUID, int, UUID]:
    """Lock source/connector/grant/coordinator, validate them and commit the in-progress refresh operation.

    Lock order: connector (all credential slots), grant, then the owner's coordinator. The
    coordinator must be idle. Commits once through the replay gate with the ORIGINAL fence.

    Returns:
        ``(refresh_token, operation_id, expected_token_revision, expected_grant_operation)`` for the
        later relock to verify nothing changed during the provider call.

    Raises:
        HTTPException: 409 for an unavailable/changed grant, busy coordinator or unusable refresh token.
    """
    session, request, scope, actor, flag, fence = (
        ctx.session,
        ctx.request,
        ctx.scope,
        ctx.actor,
        ctx.flag,
        ctx.fence,
    )
    source_fence, connector, _slots = await provisioning.lock_connector(
        session,
        source_uuid,
        provisioning._ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=flag,
        expected_access_fence=fence,
    )
    source = (
        await sources.get_connector_source(
            session, source_uuid, scope=scope, multi_workspace_enabled=flag
        )
        if source_fence is not None
        else None
    )
    grant = await session.scalar(
        select(GithubOAuthGrant)
        .where(GithubOAuthGrant.source_id == source_uuid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    if (
        source is None
        or source.provider != "github"
        or source.status != "active"
        or grant is None
        or grant.state != "ready"
        or grant.encrypted_tokens is None
    ):
        raise HTTPException(status_code=409, detail="GitHub grant is unavailable")
    if (
        connector is None
        or connector.desired_revision != grant.configuration_revision
        or source.generation != grant.source_generation
    ):
        raise HTTPException(
            status_code=409, detail="GitHub source changed; reconnect before refreshing"
        )
    if coordinator is None or coordinator.state != "idle":
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Another GitHub connection operation needs reconciliation"
        )
    opened = oauth._open_token_cipher(
        ctx.settings.connector_credential_encryption_key.get_secret_value(),
        grant.encrypted_tokens,
        grant.source_id,
        grant.operation_id,
        grant.source_generation,
        grant.configuration_revision,
    )
    refresh_token = opened.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(status_code=409, detail="GitHub grant requires reconnection")
    operation_id = uuid7()
    expected_token_revision = grant.token_revision
    expected_grant_operation = grant.operation_id
    _stage_refresh_operation(session, scope, actor, source_uuid, grant, coordinator, operation_id)
    await _publish(session, request, scope, fence)
    return refresh_token, operation_id, expected_token_revision, expected_grant_operation


async def _consume_callback_attempt(
    ctx: _FlowCtx,
    state: str,
    browser: dict[str, Any],
) -> tuple[GithubOAuthAttempt, str]:
    """Lock the PKCE attempt, validate ownership/expiry, open its verifier and mark it consumed.

    Commits (through the replay gate) the consumption so a retried callback cannot reuse the
    attempt; the returned verifier is only used for the single token exchange afterwards.

    Raises:
        HTTPException: 400 expired/used/foreign attempt or source outside the workspace; 503 when the
            sealed verifier cannot be opened.
    """
    session, request, scope = ctx.session, ctx.request, ctx.scope
    attempt = await session.scalar(
        select(GithubOAuthAttempt)
        .where(GithubOAuthAttempt.state_hash == oauth.digest(state))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if (
        attempt is None
        or attempt.consumed_at is not None
        or attempt.expires_at <= now
        or attempt.session_hash != ctx.owner.token_hash
        or attempt.browser_hash != oauth.digest(browser["browser_nonce"])
    ):
        raise HTTPException(
            status_code=400, detail="GitHub authorization expired or was already used"
        )
    if (
        await sources.get_connector_source(
            session, attempt.source_id, scope=scope, multi_workspace_enabled=ctx.flag
        )
        is None
    ):
        raise HTTPException(
            status_code=400, detail="GitHub authorization expired or was already used"
        )
    key = ctx.settings.connector_credential_encryption_key.get_secret_value()
    try:
        binding = oauth._open_token_cipher(
            key,
            attempt.encrypted_verifier,
            attempt.source_id,
            attempt.id,
            attempt.source_generation,
            attempt.configuration_revision,
        )
        verifier = binding.get("verifier")
        if not isinstance(verifier, str):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503, detail="GitHub authorization attempt is unavailable"
        ) from exc
    attempt.consumed_at = now
    await _publish(session, request, scope, ctx.fence)
    return attempt, verifier


async def _begin_callback_operation(ctx: _FlowCtx, attempt: GithubOAuthAttempt) -> None:
    """Check the callback URL, lock source and coordinator, and durably record the in-progress operation.

    Lock order: source/connector rows, then the owner's coordinator row. A busy coordinator or a
    changed source rolls back and answers 409.

    Raises:
        HTTPException: 400 callback URL mismatch; 409 source changed or coordinator not idle.
    """
    session, request, scope, actor = ctx.session, ctx.request, ctx.scope, ctx.actor
    callback_config = urlsplit(ctx.settings.github_app_callback_url)
    callback_actual = urlsplit(str(request.url))
    if (callback_actual.scheme, callback_actual.netloc, callback_actual.path) != (
        callback_config.scheme,
        callback_config.netloc,
        callback_config.path,
    ):
        raise HTTPException(
            status_code=400, detail="GitHub callback URL does not match registration"
        )
    await _admit(session, request, scope, expected=ctx.fence)
    source_fence, connector_row, _slots = await provisioning.lock_connector(
        session,
        attempt.source_id,
        provisioning._ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=ctx.flag,
        expected_access_fence=ctx.fence,
    )
    if (
        source_fence is None
        or source_fence.status != "active"
        or source_fence.generation != attempt.source_generation
        or connector_row is not None
        and connector_row.desired_revision != attempt.configuration_revision
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub source changed before authorization")
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    if coordinator is None:
        coordinator = GithubOAuthCoordinator(owner_id=actor, state="idle")
        session.add(coordinator)
        await session.flush()
    if coordinator.state != "idle":
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Another GitHub connection operation needs reconciliation"
        )
    coordinator.state = "authorizing"
    coordinator.operation_id = attempt.id
    coordinator.error_code = None
    session.add(
        GithubOAuthOperation(
            operation_id=attempt.id,
            workspace_id=scope.workspace_id,
            owner_id=actor,
            operation_kind="authorization",
            source_id=attempt.source_id,
            source_generation=attempt.source_generation,
            configuration_revision=attempt.configuration_revision,
            token_revision=attempt.expected_token_revision,
            state="in_progress",
        )
    )
    await _publish(session, request, scope, ctx.fence)


async def _exchange_and_verify(
    ctx: _FlowCtx,
    attempt: GithubOAuthAttempt,
    state: str,
    verifier: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exchange the code once and verify the granted repository and every enabled read permission.

    Any failure rolls back, marks the operation as needing reconciliation and answers 503 without
    exposing or logging provider exception text (it may carry sensitive request detail).

    Returns:
        ``(tokens, identity)`` for the grant that is about to be published.
    """
    session, request, scope, owner = ctx.session, ctx.request, ctx.scope, ctx.owner
    # Plain ids: a rollback expires ORM attributes, so the failure path must not touch ``attempt``.
    source_id, attempt_id = attempt.source_id, attempt.id
    try:
        await _require_current_owner_session(request, session, owner)
        tokens = await oauth.exchange_code(
            ctx.settings, str(request.url), state=state, verifier=verifier
        )
        source = await sources.get_connector_source(
            session, attempt.source_id, scope=scope, multi_workspace_enabled=ctx.flag
        )
        if (
            source is None
            or source.provider != "github"
            or source.status != "active"
            or source.generation != attempt.source_generation
        ):
            raise ValueError("github_source_fence_stale")
        config = project_github_source_config(source.configuration)
        identity = await oauth.validate_granted_repository(
            config,
            tokens["access_token"],
            before_request=lambda: _require_current_owner_session(request, session, owner),
        )
        # Exercise each enabled read permission against the verified installation repository.
        for enabled, resource in (
            (config.include_issues, "issues?per_page=1"),
            (config.include_pulls, "pulls?per_page=1"),
            (config.include_commits, "commits?per_page=1"),
            (config.include_releases, "releases?per_page=1"),
        ):
            if enabled:
                await _require_current_owner_session(request, session, owner)
                await oauth.probe_resource(
                    f"/repositories/{identity['repository_id']}/{resource}", tokens["access_token"]
                )
        await _require_current_owner_session(request, session, owner)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # No exception text is returned or logged because provider libraries may include sensitive request detail.
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            ctx.fence,
            source_id,
            attempt_id,
            "authorization_outcome_unknown",
        )
        raise HTTPException(
            status_code=503, detail="GitHub authorization could not be verified; reconnect to retry"
        ) from None
    return tokens, identity


async def _relock_for_publication(
    ctx: _FlowCtx,
    attempt: GithubOAuthAttempt,
) -> tuple[GithubOAuthGrant | None, GithubOAuthCoordinator, GithubOAuthOperation]:
    """Re-admit and lock source, grant, coordinator and operation, proving nothing changed since consumption.

    Every failed fence rolls back and records reconciliation (``stale_authorization_outcome_unknown``
    or ``authorization_outcome_unknown`` for admission/session failures) before re-raising/answering 409.

    Returns:
        The locked ``(grant or None, coordinator, operation)``.
    """
    session, request, scope, actor, fence = (
        ctx.session,
        ctx.request,
        ctx.scope,
        ctx.actor,
        ctx.fence,
    )
    source_id, attempt_id = attempt.source_id, attempt.id
    try:
        await _admit(session, request, scope, expected=fence)
        source_fence, current, _no_slots = await provisioning.lock_connector(
            session,
            source_id,
            (),
            scope=scope,
            multi_workspace_enabled=ctx.flag,
            expected_access_fence=fence,
        )
    except (DomainError, HTTPException):
        await session.rollback()
        await _mark_github_reconciliation(
            session, request, scope, fence, source_id, attempt_id, "authorization_outcome_unknown"
        )
        raise
    current_source = (
        await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=ctx.flag
        )
        if source_fence is not None
        else None
    )
    if (
        source_fence is None
        or source_fence.status != "active"
        or source_fence.generation != attempt.source_generation
        or current_source is None
        or current_source.provider != "github"
    ):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_id,
            attempt_id,
            "stale_authorization_outcome_unknown",
        )
        raise HTTPException(status_code=409, detail="GitHub source changed during authorization")
    current_revision = current.desired_revision if current else 0
    grant = await session.scalar(
        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id).with_for_update()
    )
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    actual_revision = grant.token_revision if grant else 0
    if (
        current_source is None
        or current_source.provider != "github"
        or current_source.status != "active"
        or current_source.generation != attempt.source_generation
        or current_revision != attempt.configuration_revision
        or actual_revision != attempt.expected_token_revision
        or coordinator is None
        or coordinator.state != "authorizing"
        or coordinator.operation_id != attempt_id
    ):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_id,
            attempt_id,
            "stale_authorization_outcome_unknown",
        )
        raise HTTPException(
            status_code=409,
            detail="A newer GitHub configuration or connection requires reconnecting",
        )
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == attempt_id,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if (
        operation is None
        or operation.owner_id != actor
        or operation.operation_kind != "authorization"
        or operation.state != "in_progress"
    ):
        await session.rollback()
        await _mark_github_reconciliation(
            session,
            request,
            scope,
            fence,
            source_id,
            attempt_id,
            "stale_authorization_outcome_unknown",
        )
        raise HTTPException(status_code=409, detail="GitHub authorization operation changed")
    # Revalidate after all locked rows, immediately before publishing the grant.
    try:
        await _require_current_owner_session(request, session, ctx.owner)
    except (DomainError, HTTPException):
        await session.rollback()
        await _mark_github_reconciliation(
            session, request, scope, fence, source_id, attempt_id, "authorization_outcome_unknown"
        )
        raise
    return grant, coordinator, operation


def _upsert_authorization_grant(
    session: AsyncSession,
    grant: GithubOAuthGrant | None,
    attempt: GithubOAuthAttempt,
    identity: dict[str, Any],
    tokens: dict[str, Any],
    operation_id: UUID,
    encrypted_tokens: Any,
) -> None:
    """Create the grant (token revision 1) or rotate the existing one (binding and token revisions +1).

    Side effects only: adds a new row to ``session`` or mutates the locked ``grant``.
    """
    if grant is None:
        grant = GithubOAuthGrant(
            source_id=attempt.source_id,
            operation_id=operation_id,
            github_user_id=identity["github_user_id"],
            repository_id=identity["repository_id"],
            installation_id=identity["installation_id"],
            app_id=identity["app_id"],
            binding_revision=1,
            source_generation=attempt.source_generation,
            configuration_revision=attempt.configuration_revision,
            token_revision=1,
            encrypted_tokens=encrypted_tokens,
            state="ready",
            expires_at=tokens["access_expires_at"],
            validated_at=datetime.now(UTC),
        )
        session.add(grant)
        return
    grant.operation_id = operation_id
    grant.github_user_id = identity["github_user_id"]
    grant.repository_id = identity["repository_id"]
    grant.installation_id = identity["installation_id"]
    grant.app_id = identity["app_id"]
    grant.binding_revision += 1
    grant.source_generation = attempt.source_generation
    grant.configuration_revision = attempt.configuration_revision
    grant.token_revision += 1
    grant.encrypted_tokens = encrypted_tokens
    grant.state = "ready"
    grant.expires_at = tokens["access_expires_at"]
    grant.validated_at = datetime.now(UTC)
    grant.error_code = None


@router.get("/github/oauth/callback")
async def complete_github_authorization(
    request: Request,
    session: Session,
    owner: OwnerRead,
    scope: WorkspaceRead,
) -> RedirectResponse:
    """Consume one PKCE callback and publish tokens only if every source/session fence is current.

    Token exchange is dispatched once after the attempt is durably consumed. A successful explicit reauthorization may rearm paused dirty hints only after the new current binding is established. Any uncertain
    exchange outcome requires a fresh authorization; stale callback results never replace a
    newer grant or automatically revoke an app-wide user authorization. The callback runs as the
    browser's selected workspace owner; an attempt whose Source is outside it reads as expired.
    Access is locked once, before the attempt, and the same ORIGINAL fence is re-admitted for every
    later transaction.
    """
    settings: Settings = request.app.state.settings
    _configured(settings)
    flag = _flag(request)
    browser = request.session.pop("github_oauth", None)
    state = request.query_params.get("state", "")
    if (
        not isinstance(browser, dict)
        or not state
        or browser.get("state_hash") != oauth.digest(state)
        or not isinstance(browser.get("browser_nonce"), str)
    ):
        raise HTTPException(status_code=400, detail="GitHub authorization state is invalid")
    fence = await _admit(session, request, scope)
    ctx = _FlowCtx(request, session, owner, scope, flag, scope.user_id, fence, settings)
    attempt, verifier = await _consume_callback_attempt(ctx, state, browser)
    if "error" in request.query_params:
        return RedirectResponse(url="/settings/sources?github=denied", status_code=303)
    await _begin_callback_operation(ctx, attempt)
    tokens, identity = await _exchange_and_verify(ctx, attempt, state, verifier)
    grant, coordinator, operation = await _relock_for_publication(ctx, attempt)
    operation_id = uuid7()
    key = settings.connector_credential_encryption_key.get_secret_value()
    encrypted_tokens = oauth._token_cipher(
        key,
        attempt.source_id,
        operation_id,
        attempt.source_generation,
        attempt.configuration_revision,
        tokens,
    )
    _upsert_authorization_grant(
        session, grant, attempt, identity, tokens, operation_id, encrypted_tokens
    )
    from modules.connectors import public as connectors

    await connectors.reconcile_github_source_hints_lifecycle(
        session,
        source_id=attempt.source_id,
        source_generation=attempt.source_generation,
        active=True,
        scope=scope,
        multi_workspace_enabled=flag,
    )
    coordinator.state = "idle"
    coordinator.operation_id = None
    coordinator.error_code = None
    operation.state = "completed"
    operation.error_code = None
    operation.resolved_at = datetime.now(UTC)
    await _publish(session, request, scope, fence)
    return RedirectResponse(url="/settings/sources?github=connected", status_code=303)
