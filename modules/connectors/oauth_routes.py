"""Source-bound Google OAuth routes for read-only Gmail, Calendar and Drive connectors."""

# Boundary validation uses ValueError for malformed provider/cipher data to preserve stable API errors.
# ruff: noqa: TRY004

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.models import AuthSession, Owner
from core.auth.public import authenticated_session_ref
from core.config import Settings
from core.database import get_session
from core.ids import uuid7
from core.realtime import commit_with_replay
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.connectors import provisioning
from modules.connectors.models import (
    ConnectorOAuthAttempt,
    ConnectorOAuthGrant,
    ConnectorProvisioning,
)
from modules.sources import public as sources

router = APIRouter(prefix="/api/v1/connectors", tags=["connector-oauth"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_account)]
OwnerWrite = Annotated[AuthSession, Depends(require_account_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_PROVIDER_SCOPES = {
    "google_mail": "https://www.googleapis.com/auth/gmail.readonly",
    "google_calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "google_drive": "https://www.googleapis.com/auth/drive.readonly",
}
_CALLBACK_PATH = "/api/v1/connectors/oauth/google/callback"


class OAuthStartRequest(BaseModel):
    """Bind a browser authorization attempt to the source version visible in the editor."""
    model_config = ConfigDict(extra="forbid")
    expected_source_generation: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class OAuthDisconnectRequest(BaseModel):
    """Require the current source and token revisions before revoking connector access."""
    model_config = ConfigDict(extra="forbid")
    expected_source_generation: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    expected_token_revision: int = Field(ge=1)


def _digest(value: str) -> str:
    """Hash one opaque OAuth/browser value before it is persisted or compared."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cipher(key: str, source_id: UUID, operation_id: UUID, generation: int, revision: int, value: dict[str, Any]) -> str:
    """Encrypt tokens or a verifier while binding ciphertext to its source operation and revision."""
    def serialize(item: object) -> str:
        """Encode only timestamps used in token expiry claims."""
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("Unsupported OAuth token value")

    payload = json.dumps({
        "source_id": str(source_id), "operation_id": str(operation_id), "generation": generation,
        "revision": revision, "value": value,
    }, sort_keys=True, separators=(",", ":"), default=serialize)
    return Fernet(key.encode("ascii")).encrypt(payload.encode("utf-8")).decode("ascii")


def _open_cipher(key: str, ciphertext: str, source_id: UUID, operation_id: UUID, generation: int, revision: int) -> dict[str, Any]:
    """Decrypt only a credential whose source, operation and configuration binding still matches."""
    try:
        payload = json.loads(Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii")))
    except (ValueError, UnicodeEncodeError, InvalidToken, json.JSONDecodeError) as exc:
        raise ValueError("connector_oauth_cipher_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("connector_oauth_cipher_binding_invalid")
    value = payload.get("value")
    if (
        payload.get("source_id"), payload.get("operation_id"), payload.get("generation"), payload.get("revision")
    ) != (str(source_id), str(operation_id), generation, revision) or not isinstance(value, dict):
        raise ValueError("connector_oauth_cipher_binding_invalid")
    return cast(dict[str, Any], value)


def _configured(settings: Settings) -> None:
    """Fail closed unless a separate connector OAuth app and exact fixed callback are configured."""
    callback = urlsplit(settings.connector_google_callback_url)
    if (
        not settings.connector_google_client_id or not settings.connector_google_client_secret.get_secret_value()
        or not settings.connector_credential_encryption_key.get_secret_value()
        or callback.scheme not in {"http", "https"} or not callback.hostname
        or callback.path != _CALLBACK_PATH or callback.query or callback.fragment or callback.username or callback.password
        or (callback.scheme, callback.netloc) != (urlsplit(str(settings.public_origin)).scheme, urlsplit(str(settings.public_origin)).netloc)
        or (callback.scheme != "https" and not settings.is_dev)
    ):
        raise HTTPException(status_code=503, detail="Google connector authorization is unavailable")


def _authorize_url(settings: Settings, provider: str, state: str, verifier: str) -> str:
    """Create Google authorization URL with read-only provider scope and S256 PKCE."""
    client = AsyncOAuth2Client(client_id=settings.connector_google_client_id, code_challenge_method="S256")
    url, _ = client.create_authorization_url(
        _AUTHORIZE_URL, state=state, code_verifier=verifier, code_challenge_method="S256",
        redirect_uri=settings.connector_google_callback_url, scope=_PROVIDER_SCOPES[provider],
        access_type="offline", prompt="consent",
    )
    return str(url)


async def _exchange(settings: Settings, code: str, state: str, verifier: str, provider: str) -> dict[str, Any]:
    """Exchange one callback code and require the exact read-only scope plus a refresh token."""
    async with AsyncOAuth2Client(
        client_id=settings.connector_google_client_id,
        client_secret=settings.connector_google_client_secret.get_secret_value(),
        timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False,
    ) as client:
        token = await client.fetch_token(
            _TOKEN_URL, code=code, state=state, grant_type="authorization_code", code_verifier=verifier,
            redirect_uri=settings.connector_google_callback_url,
        )
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    expires_in = token.get("expires_in")
    scopes = token.get("scope")
    if (
        not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
        or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not 0 < expires_in <= 7200
        or not isinstance(scopes, str) or _PROVIDER_SCOPES[provider] not in scopes.split()
        or str(token.get("token_type", "Bearer")).casefold() != "bearer"
    ):
        raise ValueError("google_readonly_grant_invalid")
    return {
        "access_token": access, "refresh_token": refresh,
        "access_expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "scopes": sorted(set(scopes.split())),
    }


async def _revoke_token(token: str) -> None:
    """Require Google to confirm revocation; uncertain results stay visible for owner reconciliation."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        response = await client.post(_REVOKE_URL, data={"token": token})
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("google_revoke_unconfirmed")


async def _admit(
    session: AsyncSession, request: Request, scope: WorkspaceContext, *, expected: AccessFence | None = None,
) -> AccessFence:
    """Lock the owner session/workspace fence, optionally requiring the original pre-network version."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await lock_access_fence(
        session, scope=scope, expected=expected,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),),
    )


async def _publish(session: AsyncSession, request: Request, scope: WorkspaceContext, fence: AccessFence) -> None:
    """Commit an OAuth state transition only while the original owner/workspace fence remains valid."""
    await commit_with_replay(
        session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        access_fence=fence,
    )


async def _require_current_session(
    request: Request, session: AsyncSession, owner_token_hash: str, owner_id: int,
) -> None:
    """Recheck a snapshotted cookie-backed owner session without reading expired ORM state after rollback."""
    from core.auth.dependencies import SESSION_COOKIE

    token = request.cookies.get(SESSION_COOKIE)
    if not token or not hmac.compare_digest(_digest(token), owner_token_hash):
        raise HTTPException(status_code=401, detail="Owner session changed during authorization")
    current = await session.scalar(select(AuthSession).join(Owner, Owner.id == AuthSession.owner_id).where(
        AuthSession.token_hash == owner_token_hash, AuthSession.owner_id == owner_id,
        AuthSession.expires_at > datetime.now(UTC), Owner.account_state == "active",
    ))
    if current is None:
        raise HTTPException(status_code=401, detail="Owner session expired during authorization")


async def _current_source(
    session: AsyncSession, request: Request, source_id: UUID, provider: str | None,
    scope: WorkspaceContext, fence: AccessFence, *, allow_paused: bool = False,
) -> tuple[Any, ConnectorProvisioning]:
    """Lock source/provisioning/credential fences before returning an owner-visible Google connector."""
    multi = request.app.state.settings.multi_workspace_enabled
    source_fence, row, _slots = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi, expected_access_fence=fence,
    )
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi)
    allowed_status = {"active", "paused"} if allow_paused else {"active"}
    if (
        source is None or source.provider not in _PROVIDER_SCOPES
        or provider is not None and source.provider != provider
        or source.status not in allowed_status or source_fence is None or row is None
    ):
        raise HTTPException(status_code=404, detail="Connector source not found")
    return source, row


@router.post("/{source_id}/oauth/google/{provider}/start")
async def start_google_authorization(
    source_id: str, provider: str, payload: OAuthStartRequest, session: Session, request: Request,
    owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, str]:
    """Persist a short-lived browser/session-bound PKCE attempt for one configured Google source."""
    settings = request.app.state.settings
    _configured(settings)
    if provider not in _PROVIDER_SCOPES:
        raise HTTPException(status_code=404, detail="Connector provider not found")
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Connector source not found") from exc
    fence = await _admit(session, request, scope)
    source, row = await _current_source(session, request, parsed_id, provider, scope, fence)
    if source.generation != payload.expected_source_generation or row.desired_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration changed; reload before connecting")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    operation_id = uuid7()
    grant = await session.get(ConnectorOAuthGrant, parsed_id)
    if grant is not None and (
        grant.provider != provider or grant.workspace_id != scope.workspace_id or grant.owner_id != owner.owner_id
    ):
        raise HTTPException(status_code=409, detail="Connector credential ownership changed; reload before connecting")
    token_revision = grant.token_revision if grant else 0
    key = settings.connector_credential_encryption_key.get_secret_value()
    attempt = ConnectorOAuthAttempt(
        id=operation_id, source_id=parsed_id, workspace_id=scope.workspace_id, owner_id=owner.owner_id,
        provider=provider, session_hash=owner.token_hash, state_hash=_digest(state),
        encrypted_verifier=_cipher(key, parsed_id, operation_id, source.generation, row.desired_revision, {"verifier": verifier}),
        source_generation=source.generation, configuration_revision=row.desired_revision,
        credential_revision=row.credential_revision, expected_token_revision=token_revision,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(attempt)
    await _publish(session, request, scope, fence)
    return {"authorization_url": _authorize_url(settings, provider, state, verifier), "attempt_id": str(operation_id), "token_revision": str(token_revision)}


@router.get("/oauth/google/callback")
async def complete_google_authorization(
    request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
) -> RedirectResponse:
    """Consume one state-bound callback and store tokens only if the owner, source and revisions still match.

    Durable state is bound to the exact AuthSession hash, workspace, source and captured revisions,
    so parallel browser tabs need no mutable client-side state map. Snapshot ORM fence values before
    committing or rolling back because rollback expires persistent objects in AsyncSession.
    """
    settings = request.app.state.settings
    _configured(settings)
    callback = urlsplit(settings.connector_google_callback_url)
    actual = urlsplit(str(request.url))
    if (actual.scheme, actual.netloc, actual.path) != (callback.scheme, callback.netloc, callback.path):
        raise HTTPException(status_code=400, detail="Google callback URL does not match registration")
    state = request.query_params.get("state", "")
    if not state or len(state) > 256:
        raise HTTPException(status_code=400, detail="Google authorization state is invalid")
    owner_token_hash, owner_id = owner.token_hash, owner.owner_id
    fence = await _admit(session, request, scope)
    attempt = await session.scalar(select(ConnectorOAuthAttempt).where(
        ConnectorOAuthAttempt.state_hash == _digest(state),
    ).with_for_update())
    now = datetime.now(UTC)
    if (
        attempt is None or attempt.consumed_at is not None or attempt.expires_at <= now
        or attempt.session_hash != owner_token_hash or attempt.owner_id != owner_id
        or attempt.workspace_id != scope.workspace_id or attempt.provider not in _PROVIDER_SCOPES
    ):
        raise HTTPException(status_code=400, detail="Google authorization expired or was already used")
    source_id, operation_id, provider = attempt.source_id, attempt.id, attempt.provider
    attempt_source_generation = attempt.source_generation
    attempt_configuration_revision = attempt.configuration_revision
    attempt_credential_revision = attempt.credential_revision
    attempt_expected_token_revision = attempt.expected_token_revision
    try:
        verifier = _open_cipher(
            settings.connector_credential_encryption_key.get_secret_value(), attempt.encrypted_verifier,
            source_id, operation_id, attempt_source_generation, attempt_configuration_revision,
        ).get("verifier")
        if not isinstance(verifier, str):
            raise ValueError("google_verifier_missing")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Google authorization attempt is unavailable") from exc
    attempt.consumed_at = now
    await _publish(session, request, scope, fence)
    if "error" in request.query_params:
        return RedirectResponse(url="/settings/sources?oauth=denied", status_code=303)
    code = request.query_params.get("code", "")
    if not code or len(code) > 4096:
        raise HTTPException(status_code=400, detail="Google authorization code is invalid")
    await _require_current_session(request, session, owner_token_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, source_id, provider, scope, fence)
    if (
        source.generation != attempt_source_generation
        or row.desired_revision != attempt_configuration_revision
        or row.credential_revision != attempt_credential_revision
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Connector changed before authorization; reconnect to continue")
    await session.rollback()
    try:
        tokens = await _exchange(settings, code, state, verifier, provider)
    except Exception:  # noqa: BLE001  # provider errors may contain credentials or authorization URLs
        await session.rollback()
        raise HTTPException(status_code=503, detail="Google authorization could not be verified; reconnect to retry") from None
    await _require_current_session(request, session, owner_token_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, source_id, provider, scope, fence)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == source_id,
    ).with_for_update())
    token_revision = grant.token_revision if grant else 0
    if (
        source.generation != attempt_source_generation or row.desired_revision != attempt_configuration_revision
        or row.credential_revision != attempt_credential_revision or token_revision != attempt_expected_token_revision
        or grant is not None and (
            grant.provider != provider or grant.workspace_id != scope.workspace_id or grant.owner_id != owner_id
        )
    ):
        # A grant may have been replaced while Google was exchanging the code; discard this result.
        await session.rollback()
        try:
            await _revoke_token(tokens["refresh_token"])
        except Exception:  # noqa: BLE001, S110
            pass  # A stale callback can never be stored; an unconfirmed remote cleanup cannot restore local authority.
        raise HTTPException(status_code=409, detail="Connector changed during authorization; reconnect to continue")
    row.credential_revision += 1
    next_token_revision = token_revision + 1
    encrypted = _cipher(
        settings.connector_credential_encryption_key.get_secret_value(), source_id, operation_id,
        source.generation, row.desired_revision,
        {**tokens, "credential_revision": row.credential_revision},
    )
    if grant is None:
        grant = ConnectorOAuthGrant(
            source_id=source_id, workspace_id=scope.workspace_id, owner_id=owner_id, provider=provider,
            operation_id=operation_id, source_generation=source.generation,
            configuration_revision=row.desired_revision, credential_revision=row.credential_revision,
            token_revision=next_token_revision, encrypted_tokens=encrypted, granted_scopes=tokens["scopes"],
        token_expires_at=datetime.fromisoformat(tokens["access_expires_at"]), state="ready",
        )
        session.add(grant)
    else:
        grant.workspace_id, grant.owner_id, grant.provider = scope.workspace_id, owner_id, provider
        grant.operation_id, grant.source_generation = operation_id, source.generation
        grant.configuration_revision, grant.credential_revision = row.desired_revision, row.credential_revision
        grant.token_revision, grant.encrypted_tokens = next_token_revision, encrypted
        grant.granted_scopes = tokens["scopes"]
        grant.token_expires_at = datetime.fromisoformat(tokens["access_expires_at"])
        grant.state, grant.error_code = "ready", None
    await _publish(session, request, scope, fence)
    return RedirectResponse(url=f"/settings/sources?oauth=connected&source={source_id}", status_code=303)


@router.get("/{source_id}/oauth/google/status")
async def google_connection_status(
    source_id: str, session: Session, request: Request, _owner: OwnerRead, scope: WorkspaceRead,
) -> dict[str, object]:
    """Return safe connection metadata without exposing access or refresh tokens."""
    if request.app.state.settings.multi_workspace_enabled and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Connector source not found") from exc
    source = await sources.get_connector_source(
        session, parsed_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if source is None or source.provider not in _PROVIDER_SCOPES:
        raise HTTPException(status_code=404, detail="Connector source not found")
    grant = await session.get(ConnectorOAuthGrant, parsed_id)
    if grant is None:
        return {"state": "not_connected", "scopes": [], "token_revision": 0}
    if grant.provider != source.provider or grant.workspace_id != scope.workspace_id or grant.owner_id != scope.user_id:
        raise HTTPException(status_code=404, detail="Connector source not found")
    return {
        "state": grant.state, "scopes": list(grant.granted_scopes),
        "token_revision": grant.token_revision, "expires_at": grant.token_expires_at,
        "error_code": grant.error_code,
    }


async def _google_discovery_access(
    source_id: UUID,
    provider: str,
    expected_generation: int,
    expected_revision: int,
    expected_token_revision: int,
    *,
    session: AsyncSession,
    request: Request,
    owner: AuthSession,
    scope: WorkspaceContext,
) -> tuple[str, AccessFence, UUID, int, int]:
    """Open or refresh an owner-only Google grant bound to this source's current saved revision.

    The OAuth grant row is locked while an expired access token is refreshed, preventing parallel
    discovery calls from overwriting a rotated refresh token. A refresh advances only token state;
    source configuration and credential revisions remain unchanged. Missing, stale, revoked, or
    mismatched grant bindings produce a conflict, and provider failures expose no token response.
    """
    from modules.connectors.google_oauth_tokens import refresh_google_token

    fence = await _admit(session, request, scope)
    source, row = await _current_source(session, request, source_id, provider, scope, fence)
    if source.generation != expected_generation or row.desired_revision != expected_revision:
        raise HTTPException(status_code=409, detail="Connector changed; reload before discovering Google data")
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == source_id,
    ).with_for_update())
    if (
        grant is None or grant.state != "ready" or grant.provider != provider
        or grant.workspace_id != scope.workspace_id or grant.owner_id != owner.owner_id
        or grant.source_generation != source.generation or grant.configuration_revision != row.desired_revision
        or grant.credential_revision != row.credential_revision
        or grant.token_revision != expected_token_revision or not grant.encrypted_tokens
        or _PROVIDER_SCOPES[provider] not in grant.granted_scopes
    ):
        raise HTTPException(status_code=409, detail="Google access changed; reconnect or reload the source")
    operation_id, credential_revision = grant.operation_id, row.credential_revision
    try:
        key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
        token_set = _open_cipher(
            key, grant.encrypted_tokens, source_id, operation_id, source.generation, row.desired_revision,
        )
        access = token_set.get("access_token")
        refresh = token_set.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ValueError("google_token_missing")
        expiry = grant.token_expires_at
        token_revision = grant.token_revision
        if expiry is None or expiry <= datetime.now(UTC) + timedelta(seconds=60):
            refreshed = await refresh_google_token(request.app.state.settings, provider, refresh)
            token_set.update(refreshed)
            access = refreshed["access_token"]
            grant.token_revision += 1
            token_revision = grant.token_revision
            grant.encrypted_tokens = _cipher(
                key, source_id, operation_id, source.generation, row.desired_revision, token_set,
            )
            grant.token_expires_at = datetime.fromisoformat(refreshed["access_expires_at"])
            await _publish(session, request, scope, fence)
        else:
            await session.rollback()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001  # Authlib transport/protocol failures have provider-specific types.
        await session.rollback()
        raise HTTPException(status_code=503, detail="Google access could not be refreshed") from None
    return access, fence, operation_id, token_revision, credential_revision


async def _check_google_discovery_fence(
    source_id: UUID,
    provider: str,
    generation: int,
    revision: int,
    credential_revision: int,
    operation_id: UUID,
    token_revision: int,
    *,
    session: AsyncSession,
    request: Request,
    owner: AuthSession,
    scope: WorkspaceContext,
    fence: AccessFence,
) -> None:
    """Abort a discovery send unless the owner, source and exact Google grant still match.

    Called immediately before each DNS-pinned API request and once after the final response. It
    checks workspace access plus source generation, desired/credential revisions, grant operation,
    provider, state and token revision; it does not modify credentials and rolls back its read UoW.
    """
    current_fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, source_id, provider, scope, current_fence)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == source_id,
    ))
    if (
        source.generation != generation or row.desired_revision != revision
        or row.credential_revision != credential_revision or grant is None
        or grant.owner_id != owner.owner_id or grant.workspace_id != scope.workspace_id
        or grant.provider != provider or grant.state != "ready"
        or grant.operation_id != operation_id or grant.token_revision != token_revision
        or grant.source_generation != generation or grant.configuration_revision != revision
        or grant.credential_revision != credential_revision
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Google access changed during discovery")
    await session.rollback()


@router.get("/{source_id}/oauth/google/{provider}/resources")
async def discover_google_source_resources(
    source_id: str,
    provider: str,
    response: Response,
    session: Session,
    request: Request,
    owner: OwnerRead,
    scope: WorkspaceRead,
    expected_source_generation: int = Query(ge=1),
    expected_revision: int = Query(ge=1),
    expected_token_revision: int = Query(ge=1),
    parent_id: str | None = Query(default=None, max_length=512),
) -> dict[str, object]:
    """Return secret-free resource choices for the exact owner, source and OAuth grant revisions.

    Gmail labels, Calendar lists and Drive children are read through fixed Google hosts with pinned
    public transport, response/page/item bounds, and a fresh owner/source/grant check before each
    send. Drive folders are selectable and browsable. The response contains only bounded IDs,
    labels, MIME types and timestamps; stale authority, revoked access, rate limits and provider
    schema/cap failures return stable HTTP errors without echoing provider response bodies.
    """
    response.headers["Cache-Control"] = "private, no-store"
    if provider not in _PROVIDER_SCOPES:
        raise HTTPException(status_code=404, detail="Connector provider not found")
    if provider != "google_drive" and parent_id is not None:
        raise HTTPException(status_code=422, detail="Parent selection is only valid for Drive")
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Connector source not found") from exc
    access, fence, operation_id, token_revision, credential_revision = await _google_discovery_access(
        parsed_id, provider, expected_source_generation, expected_revision, expected_token_revision,
        session=session, request=request, owner=owner, scope=scope,
    )

    async def before_request() -> None:
        """Revalidate the owner and exact OAuth/source bindings immediately before each pinned send."""
        await _require_current_session(request, session, owner.token_hash, owner.owner_id)
        await _check_google_discovery_fence(
            parsed_id, provider, expected_source_generation, expected_revision, credential_revision,
            operation_id, token_revision, session=session, request=request, owner=owner, scope=scope,
            fence=fence,
        )

    try:
        from modules.connectors.google_discovery import discover_google_resources
        rows, truncated = await discover_google_resources(
            provider, access, parent_id=parent_id, before_request=before_request,
        )
        await before_request()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001  # Normalize transport and provider failures before returning to the UI.
        from modules.connectors.providers import rest
        from modules.connectors.public import ProviderRateLimited

        if isinstance(exc, ProviderRateLimited):
            raise HTTPException(status_code=429, detail="Google resource discovery is rate limited") from None
        if isinstance(exc, rest.ProviderHttpError) and exc.status_code in {401, 403}:
            raise HTTPException(status_code=409, detail="Google authorization needs attention; reconnect this source") from None
        if isinstance(exc, rest.ProviderHttpError) and exc.status_code == 429:
            raise HTTPException(status_code=429, detail="Google resource discovery is rate limited") from None
        if isinstance(exc, rest.CollectionIncomplete):
            raise HTTPException(status_code=422, detail="Google resource list exceeds the bounded discovery limit") from None
        raise HTTPException(status_code=503, detail="Google resources could not be discovered") from None
    return {"items": rows, "token_revision": token_revision, "truncated": truncated}


@router.post("/{source_id}/oauth/google/disconnect")
async def disconnect_google_source(
    source_id: str, payload: OAuthDisconnectRequest, session: Session, request: Request,
    owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, str]:
    """Fence existing collection work before revoking Google tokens; uncertainty retains a disabled retry record."""
    settings = request.app.state.settings
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Connector source not found") from exc
    owner_token_hash, owner_id = owner.token_hash, owner.owner_id
    fence = await _admit(session, request, scope)
    source, row = await _current_source(session, request, parsed_id, None, scope, fence, allow_paused=True)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == parsed_id,
    ).with_for_update())
    if (
        source is None or source.provider not in _PROVIDER_SCOPES or source.status not in {"active", "paused"} or row is None
        or source.generation != payload.expected_source_generation or row.desired_revision != payload.expected_revision
        or grant is None or grant.provider != source.provider or grant.workspace_id != scope.workspace_id
        or grant.owner_id != owner_id or grant.token_revision != payload.expected_token_revision
    ):
        raise HTTPException(status_code=409, detail="Google credential changed; reload before disconnecting")
    key = settings.connector_credential_encryption_key.get_secret_value()
    try:
        token_set = _open_cipher(
            key, grant.encrypted_tokens or "", parsed_id, grant.operation_id,
            grant.source_generation, grant.configuration_revision,
        )
        refresh_token = token_set.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("google_refresh_token_missing")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Google credential requires operator reconciliation") from exc
    row.credential_revision += 1
    grant.credential_revision = row.credential_revision
    grant.state, grant.error_code = "revocation_pending", None
    await _publish(session, request, scope, fence)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
            response = await client.post(_REVOKE_URL, data={"token": refresh_token})
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("google_revoke_unconfirmed")
    except Exception:  # noqa: BLE001  # Reconcile local revocation state for any provider/client failure.
        await session.rollback()
        fence = await _admit(session, request, scope, expected=fence)
        source, row = await _current_source(session, request, parsed_id, None, scope, fence, allow_paused=True)
        grant = await session.scalar(select(ConnectorOAuthGrant).where(
            ConnectorOAuthGrant.source_id == parsed_id,
        ).with_for_update())
        if (
            grant is not None and grant.token_revision == payload.expected_token_revision
            and grant.state == "revocation_pending"
        ):
            grant.state, grant.error_code = "reconciliation_required", "revocation_unconfirmed"
            await _publish(session, request, scope, fence)
        else:
            await session.rollback()
        raise HTTPException(status_code=503, detail="Google access is disabled locally; provider revocation needs retry") from None
    await _require_current_session(request, session, owner_token_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, parsed_id, None, scope, fence, allow_paused=True)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == parsed_id,
    ).with_for_update())
    if (
        grant is None or grant.provider != source.provider or grant.workspace_id != scope.workspace_id
        or grant.owner_id != owner.owner_id or grant.token_revision != payload.expected_token_revision
        or grant.state != "revocation_pending"
    ):
        raise HTTPException(status_code=409, detail="Google credential changed during disconnect")
    grant.state, grant.encrypted_tokens = "revoked", None
    grant.granted_scopes, grant.token_expires_at = [], None
    grant.error_code = None
    await _publish(session, request, scope, fence)
    return {"state": "revoked"}
