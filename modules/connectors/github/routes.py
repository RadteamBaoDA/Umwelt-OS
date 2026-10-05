"""Owner-session routes for browser-bound GitHub App authorization and explicit grant revocation."""

from datetime import UTC, datetime, timedelta
import asyncio
import hashlib
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.dependencies import SESSION_COOKIE
from core.auth.models import AuthSession
from core.config import Settings
from core.database import get_session
from modules.connectors import provisioning, registry
from modules.connectors.github import oauth
from modules.connectors.github.schemas import GitHubCursor, project_github_source_config
from modules.connectors.github.webhooks import MAX_GITHUB_WEBHOOK_BYTES, parse_github_delivery, verify_github_signature
from modules.connectors.models import ConnectorProvisioning, GithubOAuthAttempt, GithubOAuthCoordinator, GithubOAuthGrant, GithubOAuthOperation, GithubSyncReset, GithubWebhookCapacity, GithubWebhookDelivery, GithubWebhookOutbox, GithubSourceHint
from modules.connectors.provisioning import activation_status
from modules.sources import public as sources

router = APIRouter(prefix="/api/v1/connectors", tags=["github-oauth"])
RECOVERY_GRACE = timedelta(minutes=2)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


class GitHubWebhookStatus(BaseModel):
    """Expose receiver readiness and bounded backlog counts without delivery or source identities."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    receiver_configured: bool
    receiver_revision: str
    digest_count: int = Field(ge=0, le=100_000)
    pending_count: int = Field(ge=0, le=100_000)
    pending_deliveries: int = Field(ge=0, le=100_000)
    pending_hints: int = Field(ge=0, le=100_000)
    needs_attention: int = Field(ge=0, le=200_000)
    oldest_pending_at: datetime | None = None


@router.post("/github/webhook")
async def receive_github_webhook(request: Request, session: Session) -> JSONResponse:
    """Authenticate exact GitHub request bytes, bound parsing, then durably admit a source-less delivery.

    The route intentionally has no owner-session dependency. It verifies HMAC before JSON parsing,
    checks persisted connector availability only after signature validation, and acknowledges only
    after the digest, extracted identifiers, and dispatch slot commit. Event content is a hint;
    current API reads and grants remain authoritative.
    """
    settings: Settings = request.app.state.settings
    app_id = settings.github_app_id
    secret = settings.github_app_webhook_secret.get_secret_value()
    if not app_id or not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook receiver is unavailable")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="GitHub webhook requires application/json")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isdecimal():
            raise HTTPException(status_code=400, detail="GitHub webhook length is invalid")
        if int(raw_length) > MAX_GITHUB_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="GitHub webhook body exceeds 256 KiB")
    delivery_id = request.headers.get("x-github-delivery", "")
    event = request.headers.get("x-github-event", "")
    signature = request.headers.get("x-hub-signature-256")
    if (
        not 1 <= len(delivery_id) <= 128
        or any(ord(char) < 33 or ord(char) > 126 for char in delivery_id)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event) is None
    ):
        raise HTTPException(status_code=400, detail="GitHub webhook headers are invalid")
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_GITHUB_WEBHOOK_BYTES:
                    raise HTTPException(status_code=413, detail="GitHub webhook body exceeds 256 KiB")
                body.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="GitHub webhook ingress timed out") from exc
    if not verify_github_signature(bytes(body), signature, secret):
        raise HTTPException(status_code=401, detail="GitHub webhook signature is invalid")
    try:
        delivery = parse_github_delivery(
            raw_body=bytes(body), receiver_revision=settings.github_webhook_receiver_revision,
            delivery_id=delivery_id, event=event, app_id=app_id,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="GitHub webhook payload is invalid") from exc
    from modules.connectors import public as connectors
    from modules.settings.public import module_is_enabled

    if not await module_is_enabled(session, "connectors"):
        raise HTTPException(status_code=404, detail="Webhook unavailable")

    receipt = await connectors.persist_verified_github_delivery(session, delivery)
    code = 202 if receipt.disposition == "received" else 200
    return JSONResponse(status_code=code, content=receipt.model_dump(mode="json"))


@router.get("/github/webhook-status", response_model=GitHubWebhookStatus)
async def get_github_webhook_status(
    request: Request, session: Session, _owner: OwnerRead,
) -> GitHubWebhookStatus:
    """Return owner-visible receiver readiness and global durable backlog without source inventory."""
    capacity = await session.get(GithubWebhookCapacity, 1)
    if capacity is None:
        raise HTTPException(status_code=503, detail="GitHub webhook capacity is unavailable")
    pending_deliveries = int(await session.scalar(select(func.count()).select_from(GithubWebhookOutbox).where(
        GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")),
    )) or 0)
    pending_hints = int(await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
        GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
    )) or 0)
    terminal_attention = int(await session.scalar(select(func.count()).select_from(GithubWebhookOutbox).where(
        GithubWebhookOutbox.state == "needs_attention",
    )) or 0)
    terminal_attention += int(await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
        GithubSourceHint.state == "needs_attention",
    )) or 0)
    oldest_delivery = await session.scalar(select(func.min(GithubWebhookDelivery.received_at)).join(
        GithubWebhookOutbox, GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id,
    ).where(GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention"))))
    oldest_hint = await session.scalar(select(func.min(GithubSourceHint.updated_at)).where(
        GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
    ))
    oldest_values = [value for value in (oldest_delivery, oldest_hint) if value is not None]
    oldest = min(oldest_values) if oldest_values else None
    settings: Settings = request.app.state.settings
    return GitHubWebhookStatus(
        receiver_configured=bool(settings.github_app_id and settings.github_app_webhook_secret.get_secret_value()),
        receiver_revision=settings.github_webhook_receiver_revision,
        digest_count=capacity.digest_count, pending_count=capacity.pending_count,
        pending_deliveries=pending_deliveries, pending_hints=pending_hints,
        needs_attention=terminal_attention, oldest_pending_at=oldest,
    )


class StartRequest(BaseModel):
    """Bind OAuth initiation to the editor's exact source and configuration revisions."""
    model_config = ConfigDict(extra="forbid")
    expected_source_generation: int = Field(ge=1)
    expected_revision: int = Field(ge=0)


class ResetSyncRequest(BaseModel):
    """Bind an explicit history restart to reviewed source revisions and scope digest."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_source_generation: int = Field(ge=1)
    expected_connector_revision: int = Field(ge=1)
    expected_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PeerRevision(BaseModel):
    """Represent owner-reviewed source and grant revisions for app/user-wide disconnect."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str = Field(min_length=36, max_length=36)
    source_generation: int = Field(ge=1)
    configuration_revision: int = Field(ge=1)
    token_revision: int = Field(ge=1)


class DisconnectRequest(BaseModel):
    """Require the UI to submit its complete peer inventory before revoking GitHub authority."""
    model_config = ConfigDict(extra="forbid")
    reviewed_peers: tuple[PeerRevision, ...] = Field(min_length=1, max_length=100)
    operation_id: UUID | None = None


class ReconnectReconciliationRequest(BaseModel):
    """Require explicit acknowledgement of one owner-wide authorization or refresh operation tombstone."""
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    acknowledge_unresolved_cleanup: Literal[True]


async def _require_current_owner_session(request: Request, session: AsyncSession, owner: AuthSession) -> None:
    """Re-read the exact token_hash-bound browser session so revoked sessions cannot publish OAuth state."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or hashlib.sha256(token.encode()).hexdigest() != owner.token_hash:
        raise HTTPException(status_code=401, detail="Owner session changed during GitHub operation")
    current = await session.scalar(select(AuthSession).where(AuthSession.token_hash == owner.token_hash).execution_options(populate_existing=True))
    if current is None or current.owner_id != owner.owner_id or current.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Owner session expired during GitHub operation")


async def _mark_github_reconciliation(
    session: AsyncSession, source_id: UUID, owner_id: int, operation_id: UUID, error_code: str,
) -> None:
    """Fence one uncertain OAuth operation under source, provisioning, grant, then owner-coordinator locks."""
    source_fence, _provisioning, _slots = await provisioning.lock_connector(session, source_id, provisioning._ALL_CREDENTIAL_SLOTS)
    grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id).with_for_update().execution_options(populate_existing=True))
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == owner_id).with_for_update())
    if coordinator is None or coordinator.operation_id != operation_id:
        await session.rollback()
        return
    coordinator.state, coordinator.error_code = "reconciliation_required", error_code
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == operation_id).with_for_update())
    if operation is not None:
        operation.state = "reconciliation_required"
        operation.error_code = error_code
    should_fence_grant = error_code != "stale_authorization_outcome_unknown" and (
        not error_code.startswith("stale_refresh_") or grant is not None and grant.refresh_operation_id == operation_id
    )
    if grant is not None and source_fence is not None and grant.state in {"ready", "refreshing"} and should_fence_grant:
        grant.state, grant.error_code = "reconciliation_required", error_code
    await session.commit()


def _configured(settings: Settings) -> None:
    """Fail closed unless App credentials and one exact registered callback are configured."""
    callback = urlsplit(settings.github_app_callback_url)
    if not settings.github_app_client_id or not settings.github_app_client_secret.get_secret_value() or callback.scheme not in {"http", "https"} or callback.path != oauth.CALLBACK_PATH or callback.query or callback.fragment or callback.username:
        raise HTTPException(status_code=503, detail="GitHub App connection is unavailable")


@router.post("/{source_id}/github/oauth/start")
async def start_github_authorization(source_id: str, payload: StartRequest, session: Session, request: Request, owner: OwnerWrite) -> dict[str, str]:
    """Create an expiring, owner-session and browser-cookie-bound S256 PKCE attempt.

    The attempt stores only hashes of state and browser nonce; the verifier is encrypted with
    the connector key and fenced to the source generation and saved configuration revision.
    """
    _configured(request.app.state.settings)
    try:
        parsed_id = __import__("uuid").UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    source = await sources.get_connector_source(session, parsed_id)
    if source is None or source.provider != "github" or source.status != "active" or source.generation != payload.expected_source_generation:
        raise HTTPException(status_code=409, detail="GitHub source changed; reload before connecting")
    row = await activation_status(session, parsed_id)
    revision = row.desired_revision if row else 0
    if revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="GitHub configuration changed; reload before connecting")
    try:
        project_github_source_config(source.configuration)
        registry.validate(source)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Save a valid GitHub repository and read scope first") from exc
    settings: Settings = request.app.state.settings
    key = settings.connector_credential_encryption_key.get_secret_value()
    state, verifier, browser_nonce = oauth.new_pkce_pair()
    attempt_id = uuid4()
    grant = await session.get(GithubOAuthGrant, parsed_id)
    token_revision = grant.token_revision if grant else 0
    encrypted = oauth._token_cipher(key, parsed_id, attempt_id, source.generation, revision, {"verifier": verifier})
    session.add(GithubOAuthAttempt(
        id=attempt_id, source_id=parsed_id, session_hash=owner.token_hash, state_hash=oauth.digest(state),
        browser_hash=oauth.digest(browser_nonce), encrypted_verifier=encrypted,
        source_generation=source.generation, configuration_revision=revision,
        expected_token_revision=token_revision, expires_at=datetime.now(UTC) + timedelta(minutes=10),
    ))
    request.session["github_oauth"] = {"state_hash": oauth.digest(state), "browser_nonce": browser_nonce}
    await session.commit()
    return {"authorization_url": oauth.authorization_url(settings, state=state, verifier=verifier)}


@router.get("/{source_id}/github/status")
async def github_connection_status(source_id: str, session: Session, _owner: OwnerRead) -> dict[str, object]:
    """Return a safe Settings grant, scan, and uncertainty summary without exposing token material."""
    source_uuid = UUID(source_id)
    grant = await session.get(GithubOAuthGrant, source_uuid)
    source = await sources.get_connector_source(session, source_uuid)
    cursor = None
    cursor_invalid = False
    scope_sha256 = None
    current_fence = None
    last_reset = await session.scalar(
        select(GithubSyncReset).where(GithubSyncReset.source_id == source_uuid)
        .order_by(GithubSyncReset.reset_at.desc()).limit(1)
    )
    if source is not None and source.provider == "github":
        from modules.connectors import public as connectors
        from modules.ingestion import public as ingestion

        if grant is not None:
            current_fence = await connectors.get_github_binding_fence(
                session, source_uuid, source_generation=source.generation,
                connector_revision=grant.configuration_revision,
            )
            scope_sha256 = current_fence.scope_sha256 if current_fence is not None else None

        raw_cursor = await ingestion.get_source_cursor(session, source_uuid)
        if raw_cursor is not None:
            try:
                cursor = GitHubCursor.model_validate_json(raw_cursor)
                if (
                    cursor.source_id != source.id or cursor.source_generation != source.generation
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
    sync_status = {
        "history_days": project_github_source_config(source.configuration).github_history_days
        if source is not None and source.provider == "github" else 90,
        "scope_sha256": scope_sha256,
        "last_reset_at": last_reset.reset_at if last_reset else None,
        "gap_recorded": last_reset is not None,
        "cursor_invalid": cursor_invalid,
        "unverified_hints": await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
            GithubSourceHint.source_id == source_uuid,
            GithubSourceHint.state == "visibility_unverified",
        )) or 0,
        "reconcile_exhausted": await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
            GithubSourceHint.source_id == source_uuid,
            GithubSourceHint.intent == "reconcile",
            GithubSourceHint.reconcile_page > 100,
            GithubSourceHint.state == "needs_attention",
        )) or 0,
        "resources": [
            {
                "resource": item.resource, "phase": item.phase,
                "page": item.page, "next_page": item.next_page,
                "sweep_revision": item.sweep_revision,
                "floor": item.floor,
                "upper": item.upper,
                "completed_upper": item.completed_upper,
                "completed_sweep_revision": item.completed_sweep_revision,
                "incomplete": item.phase == "exhausted",
            }
            for item in cursor.resources
        ] if cursor is not None else [],
    }
    coordinator = await session.get(GithubOAuthCoordinator, _owner.owner_id)
    operation = await session.get(GithubOAuthOperation, coordinator.operation_id) if coordinator and coordinator.operation_id else None
    if operation is not None and operation.owner_id != _owner.owner_id:
        operation = None
    recovery_available = bool(
        coordinator is not None and coordinator.operation_id is not None
        and (coordinator.state == "reconciliation_required" or coordinator.updated_at <= datetime.now(UTC) - RECOVERY_GRACE
             and coordinator.state in {"authorizing", "refreshing", "revoking"})
    )
    operation_details = {
        "operation_kind": operation.operation_kind if operation else None,
        "operation_source_id": str(operation.source_id) if operation and operation.source_id else None,
        "operation_source_generation": operation.source_generation if operation else None,
        "operation_configuration_revision": operation.configuration_revision if operation else None,
        "operation_state": operation.state if operation else None,
    }
    if grant is None:
        return {"state": "not_connected", "expires_at": None, "error_code": None, "coordinator_state": coordinator.state if coordinator else "idle", "operation_id": str(coordinator.operation_id) if coordinator and coordinator.operation_id else None, "coordinator_error_code": coordinator.error_code if coordinator else None, "recovery_available": recovery_available, "sync": sync_status, **operation_details}
    return {"state": grant.state, "expires_at": grant.expires_at, "error_code": grant.error_code, "coordinator_state": coordinator.state if coordinator else "idle", "operation_id": str(coordinator.operation_id) if coordinator and coordinator.operation_id else None, "recovery_available": recovery_available, "sync": sync_status, **operation_details}


@router.post("/{source_id}/github/sync/reset")
async def reset_github_sync(source_id: str, payload: ResetSyncRequest, session: Session, _owner: OwnerWrite) -> dict[str, str]:
    """Restart selected GitHub resource sweeps after matching reviewed scope and idle ingestion state."""
    from modules.connectors import public as connectors

    await connectors.reset_github_collection_cursor(
        session, source_id=UUID(source_id),
        source_generation=payload.expected_source_generation,
        connector_revision=payload.expected_connector_revision,
        expected_scope_sha256=payload.expected_scope_sha256,
    )
    return {"status": "reset"}


@router.post("/{source_id}/github/reconcile/reconnect")
async def acknowledge_github_reconnect(source_id: str, payload: ReconnectReconciliationRequest, session: Session, request: Request, owner: OwnerWrite) -> dict[str, str]:
    """Acknowledge one exact owner-wide auth/refresh tombstone even if its original source is paused or deleted.

    The path source is only the editor from which the owner initiated recovery. Stored origin identity
    authorizes no action on that source; current grants are fenced only when their recorded revisions
    still match the unresolved operation.
    """
    __import__("uuid").UUID(source_id)
    operation = await session.get(GithubOAuthOperation, payload.operation_id)
    if operation is None or operation.owner_id != owner.owner_id or operation.operation_kind not in {"authorization", "refresh"} or operation.source_id is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub recovery operation is unavailable; reload connection status")
    source_fence = await sources.lock_source(session, operation.source_id)
    grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == operation.source_id).with_for_update().execution_options(populate_existing=True))
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == owner.owner_id).with_for_update())
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == payload.operation_id).with_for_update().execution_options(populate_existing=True))
    if operation is None or operation.owner_id != owner.owner_id or coordinator is None or coordinator.operation_id != payload.operation_id or coordinator.state not in {"reconciliation_required", "authorizing", "refreshing"}:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub reconciliation operation changed; reload connection status")
    if coordinator.state != "reconciliation_required" and coordinator.updated_at > datetime.now(UTC) - RECOVERY_GRACE:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub operation is still within its recovery window")
    if operation.state not in {"in_progress", "reconciliation_required"}:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub recovery operation was already resolved")
    recovery_error = coordinator.error_code or operation.error_code or ("authorization_outcome_unknown" if operation.operation_kind == "authorization" else "token_refresh_outcome_unknown")
    await _require_current_owner_session(request, session, owner)
    if (grant is not None and source_fence is not None
        and (grant.source_generation, grant.configuration_revision, grant.token_revision)
        == (operation.source_generation, operation.configuration_revision, operation.token_revision)
        and grant.state in {"ready", "refreshing"}
        and (operation.operation_kind != "refresh" or grant.refresh_operation_id == payload.operation_id)):
        grant.state = "reconciliation_required"
        grant.error_code = recovery_error
    coordinator.state, coordinator.operation_id = "idle", None
    coordinator.error_code = recovery_error
    operation.state = "acknowledged"
    operation.error_code = recovery_error
    operation.resolved_at = datetime.now(UTC)
    # Keep nonsecret operation origin and outcome after releasing the owner-wide coordinator.
    await session.commit()
    return {"state": "reconnect_required"}


@router.post("/{source_id}/github/oauth/refresh")
async def refresh_github_authorization(source_id: str, session: Session, request: Request, _owner: OwnerWrite) -> dict[str, str]:
    """Rotate a source's expiring tokens under the owner-wide coordinator and revision fences."""
    _configured(request.app.state.settings)
    source_uuid = __import__("uuid").UUID(source_id)
    source_fence, connector, _slots = await provisioning.lock_connector(session, source_uuid, provisioning._ALL_CREDENTIAL_SLOTS)
    source = await sources.get_connector_source(session, source_uuid) if source_fence is not None else None
    grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_uuid).with_for_update().execution_options(populate_existing=True))
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _owner.owner_id).with_for_update())
    if source is None or source.provider != "github" or source.status != "active" or grant is None or grant.state != "ready" or grant.encrypted_tokens is None:
        raise HTTPException(status_code=409, detail="GitHub grant is unavailable")
    if connector is None or connector.desired_revision != grant.configuration_revision or source.generation != grant.source_generation:
        raise HTTPException(status_code=409, detail="GitHub source changed; reconnect before refreshing")
    if coordinator is None or coordinator.state != "idle":
        await session.rollback()
        raise HTTPException(status_code=409, detail="Another GitHub connection operation needs reconciliation")
    opened = oauth._open_token_cipher(request.app.state.settings.connector_credential_encryption_key.get_secret_value(), grant.encrypted_tokens, grant.source_id, grant.operation_id, grant.source_generation, grant.configuration_revision)
    refresh_token = opened.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(status_code=409, detail="GitHub grant requires reconnection")
    operation_id = uuid4()
    expected_token_revision = grant.token_revision
    expected_grant_operation = grant.operation_id
    coordinator.state, coordinator.operation_id = "refreshing", operation_id
    coordinator.error_code = None
    session.add(GithubOAuthOperation(
        operation_id=operation_id, owner_id=_owner.owner_id, operation_kind="refresh",
        source_id=source_uuid, source_generation=grant.source_generation,
        configuration_revision=grant.configuration_revision, token_revision=grant.token_revision,
        state="in_progress",
    ))
    grant.refresh_operation_id = operation_id
    grant.state = "refreshing"
    await session.commit()
    try:
        await _require_current_owner_session(request, session, _owner)
        tokens = await oauth.refresh_github_grant(request.app.state.settings, refresh_token)
        await _require_current_owner_session(request, session, _owner)
    except Exception:
        await session.rollback()
        await _mark_github_reconciliation(session, source_uuid, _owner.owner_id, operation_id, "token_refresh_outcome_unknown")
        raise HTTPException(status_code=503, detail="GitHub token refresh failed; reconnect to restore collection") from None
    source_fence = await sources.lock_source(session, source_uuid)
    source = await sources.get_connector_source(session, source_uuid) if source_fence is not None else None
    current_provisioning = await session.scalar(select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == source_uuid).with_for_update().execution_options(populate_existing=True))
    grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_uuid).with_for_update().execution_options(populate_existing=True))
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _owner.owner_id).with_for_update())
    if source is None or source.provider != "github" or source.status != "active" or grant is None or current_provisioning is None or source.generation != grant.source_generation or current_provisioning.desired_revision != grant.configuration_revision or grant.token_revision != expected_token_revision or grant.operation_id != expected_grant_operation or grant.state != "refreshing" or grant.refresh_operation_id != operation_id or coordinator is None or coordinator.operation_id != operation_id:
        await session.rollback()
        await _mark_github_reconciliation(session, source_uuid, _owner.owner_id, operation_id, "stale_refresh_outcome_unknown")
        raise HTTPException(status_code=409, detail="GitHub source changed during refresh")
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == operation_id).with_for_update())
    if operation is None or operation.owner_id != _owner.owner_id or operation.operation_kind != "refresh" or operation.state != "in_progress":
        await session.rollback()
        await _mark_github_reconciliation(session, source_uuid, _owner.owner_id, operation_id, "stale_refresh_outcome_unknown")
        raise HTTPException(status_code=409, detail="GitHub refresh operation changed")
    # This must remain the final awaited authorization check after every lifecycle lock.
    try:
        await _require_current_owner_session(request, session, _owner)
    except HTTPException:
        await session.rollback()
        await _mark_github_reconciliation(session, source_uuid, _owner.owner_id, operation_id, "token_refresh_outcome_unknown")
        raise
    grant.operation_id = operation_id
    grant.token_revision += 1
    grant.refresh_operation_id = None
    grant.encrypted_tokens = oauth._token_cipher(request.app.state.settings.connector_credential_encryption_key.get_secret_value(), source_uuid, operation_id, grant.source_generation, grant.configuration_revision, tokens)
    grant.expires_at = tokens["access_expires_at"]
    grant.state, grant.error_code, grant.validated_at = "ready", None, datetime.now(UTC)
    coordinator.state, coordinator.operation_id = "idle", None
    coordinator.error_code = None
    operation.state = "completed"
    operation.error_code = None
    operation.resolved_at = datetime.now(UTC)
    await session.commit()
    return {"state": "ready"}


@router.get("/github/oauth/callback")
async def complete_github_authorization(request: Request, session: Session, owner: OwnerRead) -> RedirectResponse:
    """Consume one PKCE callback and publish tokens only if every source/session fence is current.

    Token exchange is dispatched once after the attempt is durably consumed. A successful explicit reauthorization may rearm paused dirty hints only after the new current binding is established. Any uncertain
    exchange outcome requires a fresh authorization; stale callback results never replace a
    newer grant or automatically revoke an app-wide user authorization.
    """
    settings: Settings = request.app.state.settings
    _configured(settings)
    browser = request.session.pop("github_oauth", None)
    state = request.query_params.get("state", "")
    if not isinstance(browser, dict) or not state or browser.get("state_hash") != oauth.digest(state) or not isinstance(browser.get("browser_nonce"), str):
        raise HTTPException(status_code=400, detail="GitHub authorization state is invalid")
    attempt = await session.scalar(select(GithubOAuthAttempt).where(GithubOAuthAttempt.state_hash == oauth.digest(state)).with_for_update())
    now = datetime.now(UTC)
    if attempt is None or attempt.consumed_at is not None or attempt.expires_at <= now or attempt.session_hash != owner.token_hash or attempt.browser_hash != oauth.digest(browser["browser_nonce"]):
        raise HTTPException(status_code=400, detail="GitHub authorization expired or was already used")
    source_id, attempt_id = attempt.source_id, attempt.id
    key = settings.connector_credential_encryption_key.get_secret_value()
    try:
        binding = oauth._open_token_cipher(key, attempt.encrypted_verifier, source_id, attempt_id, attempt.source_generation, attempt.configuration_revision)
        verifier = binding.get("verifier")
        if not isinstance(verifier, str):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="GitHub authorization attempt is unavailable") from exc
    attempt.consumed_at = now
    await session.commit()
    if "error" in request.query_params:
        return RedirectResponse(url="/settings/sources?github=denied", status_code=303)
    callback_config = urlsplit(settings.github_app_callback_url)
    callback_actual = urlsplit(str(request.url))
    if (callback_actual.scheme, callback_actual.netloc, callback_actual.path) != (callback_config.scheme, callback_config.netloc, callback_config.path):
        raise HTTPException(status_code=400, detail="GitHub callback URL does not match registration")
    source_fence = await sources.lock_source(session, source_id)
    _source_row, connector_row, _slots = await provisioning.lock_connector(session, source_id, provisioning._ALL_CREDENTIAL_SLOTS)
    if source_fence is None or source_fence.status != "active" or source_fence.generation != attempt.source_generation or connector_row is not None and connector_row.desired_revision != attempt.configuration_revision:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub source changed before authorization")
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == owner.owner_id).with_for_update())
    if coordinator is None:
        coordinator = GithubOAuthCoordinator(owner_id=owner.owner_id, state="idle")
        session.add(coordinator)
        await session.flush()
    if coordinator.state != "idle":
        await session.rollback()
        raise HTTPException(status_code=409, detail="Another GitHub connection operation needs reconciliation")
    coordinator.state = "authorizing"
    coordinator.operation_id = attempt_id
    coordinator.error_code = None
    session.add(GithubOAuthOperation(
        operation_id=attempt_id, owner_id=owner.owner_id, operation_kind="authorization",
        source_id=source_id, source_generation=attempt.source_generation,
        configuration_revision=attempt.configuration_revision, token_revision=attempt.expected_token_revision,
        state="in_progress",
    ))
    await session.commit()
    try:
        await _require_current_owner_session(request, session, owner)
        tokens = await oauth.exchange_code(settings, str(request.url), state=state, verifier=verifier)
        source = await sources.get_connector_source(session, source_id)
        if source is None or source.provider != "github" or source.status != "active" or source.generation != attempt.source_generation:
            raise ValueError("github_source_fence_stale")
        config = project_github_source_config(source.configuration)
        identity = await oauth.validate_granted_repository(
            config, tokens["access_token"], before_request=lambda: _require_current_owner_session(request, session, owner),
        )
        # Exercise each enabled read permission against the verified installation repository.
        for enabled, resource in ((config.include_issues, "issues?per_page=1"), (config.include_pulls, "pulls?per_page=1"), (config.include_commits, "commits?per_page=1"), (config.include_releases, "releases?per_page=1")):
            if enabled:
                await _require_current_owner_session(request, session, owner)
                await oauth.probe_resource(f"/repositories/{identity['repository_id']}/{resource}", tokens["access_token"])
        await _require_current_owner_session(request, session, owner)
    except Exception:
        # No exception text is returned or logged because provider libraries may include sensitive request detail.
        await session.rollback()
        await _mark_github_reconciliation(session, source_id, owner.owner_id, attempt_id, "authorization_outcome_unknown")
        raise HTTPException(status_code=503, detail="GitHub authorization could not be verified; reconnect to retry") from None
    source_fence = await sources.lock_source(session, source_id)
    current_source = await sources.get_connector_source(session, source_id) if source_fence is not None else None
    if source_fence is None or source_fence.status != "active" or source_fence.generation != attempt.source_generation or current_source is None or current_source.provider != "github":
        await session.rollback()
        await _mark_github_reconciliation(session, source_id, owner.owner_id, attempt_id, "stale_authorization_outcome_unknown")
        raise HTTPException(status_code=409, detail="GitHub source changed during authorization")
    current = await session.scalar(select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == source_id).with_for_update())
    current_source = await sources.get_connector_source(session, source_id)
    current_revision = current.desired_revision if current else 0
    grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id).with_for_update())
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == owner.owner_id).with_for_update())
    actual_revision = grant.token_revision if grant else 0
    if current_source is None or current_source.provider != "github" or current_source.status != "active" or current_source.generation != attempt.source_generation or current_revision != attempt.configuration_revision or actual_revision != attempt.expected_token_revision or coordinator is None or coordinator.state != "authorizing" or coordinator.operation_id != attempt_id:
        await session.rollback()
        await _mark_github_reconciliation(session, source_id, owner.owner_id, attempt_id, "stale_authorization_outcome_unknown")
        raise HTTPException(status_code=409, detail="A newer GitHub configuration or connection requires reconnecting")
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == attempt_id).with_for_update())
    if operation is None or operation.owner_id != owner.owner_id or operation.operation_kind != "authorization" or operation.state != "in_progress":
        await session.rollback()
        await _mark_github_reconciliation(session, source_id, owner.owner_id, attempt_id, "stale_authorization_outcome_unknown")
        raise HTTPException(status_code=409, detail="GitHub authorization operation changed")
    # Revalidate after all locked rows, immediately before publishing the grant.
    try:
        await _require_current_owner_session(request, session, owner)
    except HTTPException:
        await session.rollback()
        await _mark_github_reconciliation(session, source_id, owner.owner_id, attempt_id, "authorization_outcome_unknown")
        raise
    operation_id = uuid4()
    encrypted_tokens = oauth._token_cipher(key, source_id, operation_id, attempt.source_generation, attempt.configuration_revision, tokens)
    if grant is None:
        grant = GithubOAuthGrant(source_id=source_id, operation_id=operation_id, github_user_id=identity["github_user_id"], repository_id=identity["repository_id"], installation_id=identity["installation_id"], app_id=identity["app_id"], binding_revision=1, source_generation=attempt.source_generation, configuration_revision=attempt.configuration_revision, token_revision=1, encrypted_tokens=encrypted_tokens, state="ready", expires_at=tokens["access_expires_at"], validated_at=datetime.now(UTC))
        session.add(grant)
    else:
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
    from modules.connectors import public as connectors

    await connectors.reconcile_github_source_hints_lifecycle(
        session, source_id=source_id, source_generation=attempt.source_generation, active=True,
    )
    coordinator.state = "idle"
    coordinator.operation_id = None
    coordinator.error_code = None
    operation.state = "completed"
    operation.error_code = None
    operation.resolved_at = datetime.now(UTC)
    await session.commit()
    return RedirectResponse(url="/settings/sources?github=connected", status_code=303)


@router.get("/{source_id}/github/summary")
async def github_project_summary(source_id: str, session: Session, _owner: OwnerRead) -> dict[str, object]:
    """Return canonical mapped GitHub counts and the latest observation time for one github source.

    Counts come only from the Timeline public API, i.e. from records that completed collection and
    mapping. ``live_verified`` is always false: no live provider check backs these numbers.
    """
    source_uuid = UUID(source_id)
    source = await sources.get_connector_source(session, source_uuid)
    if source is None or source.provider != "github":
        raise HTTPException(status_code=404, detail="GitHub source not found")
    from modules.timeline import public as timeline

    counts, latest = await timeline.summarize_source_events(session, source_uuid, "github_")
    return {
        "resource_counts": {
            "repositories": 1 if counts else 0, "issues": counts.get("github_issue", 0),
            "pull_requests": counts.get("github_pull", 0), "commits": counts.get("github_commit", 0),
            "releases": counts.get("github_release", 0),
        },
        "live_verified": False,
        "last_event_at": latest.isoformat() if latest else None,
    }


@router.get("/{source_id}/github/peers")
async def list_github_grant_peers(source_id: str, session: Session, _owner: OwnerRead) -> dict[str, object]:
    """Return a bounded, secret-free inventory that must be reviewed before app-wide revocation."""
    parsed_id = __import__("uuid").UUID(source_id)
    current_user_id = await session.scalar(select(GithubOAuthGrant.github_user_id).where(GithubOAuthGrant.source_id == parsed_id))
    if current_user_id is None:
        return {"complete": True, "peers": []}
    rows = list((await session.execute(select(
        GithubOAuthGrant.source_id, GithubOAuthGrant.source_generation,
        GithubOAuthGrant.configuration_revision, GithubOAuthGrant.token_revision, GithubOAuthGrant.state,
    ).where(GithubOAuthGrant.github_user_id == current_user_id, GithubOAuthGrant.encrypted_tokens.is_not(None)).order_by(GithubOAuthGrant.source_id).limit(101))).all())
    if len(rows) > 100:
        return {"complete": False, "peers": []}
    return {"complete": True, "github_user_id": current_user_id, "peers": [{"source_id": str(item.source_id), "source_generation": item.source_generation, "configuration_revision": item.configuration_revision, "token_revision": item.token_revision, "state": item.state} for item in rows]}


@router.post("/{source_id}/github/disconnect")
async def revoke_github_grant(source_id: str, payload: DisconnectRequest, session: Session, request: Request, _owner: OwnerWrite) -> dict[str, str]:
    """Fence every reviewed local peer atomically before GitHub's app/user-wide grant revoke.

    Peer inventory is bounded and rechecked under row locks; source rows are locked in sorted
    order before provisioning and grant rows. Remote uncertainty leaves local access fenced.
    """
    _configured(request.app.state.settings)
    target_id = __import__("uuid").UUID(source_id)
    target_user_id = await session.scalar(select(GithubOAuthGrant.github_user_id).where(GithubOAuthGrant.source_id == target_id))
    if target_user_id is None:
        return {"state": "revoked"}
    peer_inventory = select(
        GithubOAuthGrant.source_id, GithubOAuthGrant.source_generation,
        GithubOAuthGrant.configuration_revision, GithubOAuthGrant.token_revision,
    ).where(GithubOAuthGrant.github_user_id == target_user_id, GithubOAuthGrant.encrypted_tokens.is_not(None)).order_by(GithubOAuthGrant.source_id).limit(101)
    peer_rows = list((await session.execute(peer_inventory)).all())
    if len(peer_rows) > 100:
        raise HTTPException(status_code=409, detail="GitHub grant peer inventory is too large; revocation is unavailable")
    expected = {item.source_id: item for item in payload.reviewed_peers}
    actual_ids = {item.source_id for item in peer_rows}
    if set(map(str, actual_ids)) != {item.source_id for item in payload.reviewed_peers}:
        raise HTTPException(status_code=409, detail="GitHub grant peers changed; review the current affected sources")
    for item in sorted(peer_rows, key=lambda row: str(row.source_id)):
        reviewed = expected.get(str(item.source_id))
        if reviewed is None or (item.source_generation, item.configuration_revision, item.token_revision) != (reviewed.source_generation, reviewed.configuration_revision, reviewed.token_revision):
            raise HTTPException(status_code=409, detail="A GitHub source revision changed; review the current affected sources")
        fence = await sources.lock_source(session, item.source_id)
        if fence is None or fence.status == "archived" or fence.status == "active" and fence.generation != item.source_generation or fence.status == "paused" and fence.generation not in {item.source_generation, item.source_generation + 1}:
            raise HTTPException(status_code=409, detail="A GitHub source lifecycle changed; review current affected sources")
    key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _owner.owner_id).with_for_update())
    explicit_revoke_retry = bool(
        coordinator is not None and coordinator.state in {"reconciliation_required", "revoking"}
        and coordinator.error_code in {"provider_revoke_outcome_unknown", "provider_revoke_pending"}
        and payload.operation_id == coordinator.operation_id
        and (coordinator.state == "reconciliation_required" or coordinator.updated_at <= datetime.now(UTC) - RECOVERY_GRACE)
    )
    if coordinator is not None and coordinator.state != "idle" and not explicit_revoke_retry:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A GitHub connection operation needs reconciliation before disconnect")
    if coordinator is None:
        coordinator = GithubOAuthCoordinator(owner_id=_owner.owner_id, state="idle")
        session.add(coordinator)
        await session.flush()
    # A newly authorized peer may have appeared while this request waited for the
    # app/user operation lock; recheck the complete inventory before fencing anything.
    locked_inventory = list((await session.execute(peer_inventory.execution_options(populate_existing=True))).all())
    if len(locked_inventory) > 100 or {
        (row.source_id, row.source_generation, row.configuration_revision, row.token_revision)
        for row in locked_inventory
    } != {
        (row.source_id, row.source_generation, row.configuration_revision, row.token_revision)
        for row in peer_rows
    }:
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub grant peers changed; review the current affected sources")
    coordinator.state = "revoking"
    coordinator.error_code = "provider_revoke_pending"
    revocation_operation = coordinator.operation_id if explicit_revoke_retry else uuid4()
    coordinator.operation_id = revocation_operation
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == revocation_operation).with_for_update())
    origin = next((row for row in peer_rows if row.source_id == target_id), peer_rows[0])
    peer_snapshot = [{
        "source_id": str(item.source_id), "source_generation": item.source_generation,
        "configuration_revision": item.configuration_revision, "token_revision": item.token_revision,
    } for item in peer_rows]
    if operation is None:
        operation = GithubOAuthOperation(
            operation_id=revocation_operation, owner_id=_owner.owner_id, operation_kind="revoke",
            source_id=origin.source_id, source_generation=origin.source_generation,
            configuration_revision=origin.configuration_revision, token_revision=origin.token_revision,
            peer_inventory=peer_snapshot,
        )
        session.add(operation)
    operation.state, operation.error_code, operation.resolved_at = "in_progress", "provider_revoke_pending", None
    token = None
    for item in sorted(peer_rows, key=lambda row: str(row.source_id)):
        source_fence = await sources.lock_source(session, item.source_id)
        current_source = await sources.get_connector_source(session, item.source_id) if source_fence is not None else None
        row = await session.scalar(select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == item.source_id).with_for_update().execution_options(populate_existing=True))
        if row is None or row.desired_revision != item.configuration_revision:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A GitHub source configuration changed")
        locked = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == item.source_id).with_for_update().execution_options(populate_existing=True))
        try:
            if current_source is not None:
                registry.configuration(current_source)
                project_github_source_config(current_source.configuration)
        except (ValueError, TypeError):
            current_source = None
        lifecycle_matches = bool(
            current_source is not None and locked is not None
            and (current_source.generation == locked.source_generation if current_source.status == "active"
                 else current_source.status == "paused" and current_source.generation in {locked.source_generation, locked.source_generation + 1})
        )
        if current_source is None or current_source.id != item.source_id or current_source.provider != "github" or current_source.status == "archived" or not lifecycle_matches or locked is None or locked.source_generation != item.source_generation or locked.configuration_revision != row.desired_revision or locked.token_revision != item.token_revision or locked.state not in {"ready", "reconciliation_required", "revoked"} or locked.encrypted_tokens is None:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A GitHub grant changed; review current affected sources")
        if token is None:
            opened = oauth._open_token_cipher(key, locked.encrypted_tokens, locked.source_id, locked.operation_id, locked.source_generation, locked.configuration_revision)
            token = opened.get("access_token")
        source = await sources.pause_source_for_connector(session, item.source_id)
        if source is None:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A GitHub source lifecycle changed")
        locked.state = "revoked"
        locked.error_code = "provider_revoke_pending"
    await session.commit()
    if not isinstance(token, str):
        coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
        operation.state, operation.error_code, operation.resolved_at = "completed", None, datetime.now(UTC)
        await session.commit()
        return {"state": "revoked"}
    await _require_current_owner_session(request, session, _owner)
    try:
        await oauth.revoke_github_grant(request.app.state.settings, token)
    except Exception as exc:
        # Local grants remain fenced; the owner can see cleanup is unresolved without exposing credentials.
        coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _owner.owner_id).with_for_update())
        if coordinator is not None and coordinator.operation_id == revocation_operation:
            coordinator.state = "reconciliation_required"
            coordinator.error_code = "provider_revoke_outcome_unknown"
            operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == revocation_operation).with_for_update())
            if operation is not None:
                operation.state = "review_required"
                operation.error_code = "provider_revoke_outcome_unknown"
            await session.commit()
        raise HTTPException(status_code=503, detail="Local GitHub sources are disconnected; provider revocation outcome is unknown") from None
    for item in peer_rows:
        locked = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == item.source_id).with_for_update())
        if locked is not None and locked.state == "revoked":
            locked.encrypted_tokens = None
            locked.error_code = None
    coordinator = await session.scalar(select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _owner.owner_id).with_for_update())
    if coordinator is not None and coordinator.operation_id == revocation_operation:
        coordinator.state = "idle"
        coordinator.operation_id = None
        coordinator.error_code = None
    operation = await session.scalar(select(GithubOAuthOperation).where(GithubOAuthOperation.operation_id == revocation_operation).with_for_update())
    if operation is not None:
        operation.state, operation.error_code, operation.resolved_at = "completed", None, datetime.now(UTC)
    await session.commit()
    return {"state": "revoked"}
