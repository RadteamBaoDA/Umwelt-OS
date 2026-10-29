"""Source-bound OAuth lifecycle for read-only Reddit subreddit connectors."""

# Boundary validation uses ValueError for malformed provider/cipher data to preserve stable API errors.
# ruff: noqa: TRY004

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
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
_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REVOKE_URL = "https://www.reddit.com/api/v1/revoke_token"
_CALLBACK_PATH = "/api/v1/connectors/oauth/reddit/callback"
_READ_SCOPE = "read"


class RedditOAuthRequest(BaseModel):
    """Carry the editor's expected saved-source revision into an OAuth transition."""
    model_config = ConfigDict(extra="forbid")
    expected_source_generation: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class RedditDisconnectRequest(RedditOAuthRequest):
    """Require the token revision observed by the owner before disconnecting."""
    expected_token_revision: int = Field(ge=1)


def _digest(value: str) -> str:
    """Hash opaque OAuth state and session tokens before database persistence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seal(
    key: str, source_id: UUID, operation_id: UUID, generation: int, revision: int,
    value: dict[str, Any],
) -> str:
    """Encrypt token material with source, operation, generation and configuration binding."""
    payload = json.dumps({
        "source_id": str(source_id), "operation_id": str(operation_id),
        "generation": generation, "revision": revision, "value": value,
    }, sort_keys=True, separators=(",", ":"))
    return Fernet(key.encode("ascii")).encrypt(payload.encode("utf-8")).decode("ascii")


def _open(
    key: str, ciphertext: str, source_id: UUID, operation_id: UUID,
    generation: int, revision: int,
) -> dict[str, Any]:
    """Decrypt one token set only when its persisted source/configuration binding matches."""
    try:
        payload = json.loads(Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii")))
    except (ValueError, UnicodeEncodeError, InvalidToken, json.JSONDecodeError) as exc:
        raise ValueError("reddit_oauth_cipher_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("reddit_oauth_cipher_binding_invalid")
    value = payload.get("value")
    if (
        payload.get("source_id"), payload.get("operation_id"), payload.get("generation"),
        payload.get("revision"),
    ) != (str(source_id), str(operation_id), generation, revision) or not isinstance(value, dict):
        raise ValueError("reddit_oauth_cipher_binding_invalid")
    return cast(dict[str, Any], value)


def _configured(settings: Settings) -> None:
    """Require a separate Reddit confidential-client app and exact same-origin callback."""
    callback = urlsplit(settings.connector_reddit_callback_url)
    origin = urlsplit(str(settings.public_origin))
    if (
        not settings.connector_reddit_client_id or not settings.connector_reddit_client_secret.get_secret_value()
        or not settings.connector_credential_encryption_key.get_secret_value()
        or callback.scheme not in {"http", "https"} or not callback.hostname
        or callback.path != _CALLBACK_PATH or callback.query or callback.fragment or callback.username or callback.password
        or (callback.scheme, callback.netloc) != (origin.scheme, origin.netloc)
        or (callback.scheme != "https" and not settings.is_dev)
    ):
        raise HTTPException(status_code=503, detail="Reddit connector authorization is unavailable")


def _client_auth(settings: Settings) -> str:
    """Build Reddit's HTTP Basic client authentication header without exposing its secret."""
    raw = f"{settings.connector_reddit_client_id}:{settings.connector_reddit_client_secret.get_secret_value()}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _authorization_url(settings: Settings, state: str) -> str:
    """Create a short-lived Reddit authorization URL restricted to read-only access."""
    from urllib.parse import urlencode

    query = urlencode({
        "client_id": settings.connector_reddit_client_id, "response_type": "code",
        "state": state, "redirect_uri": settings.connector_reddit_callback_url,
        "duration": "permanent", "scope": _READ_SCOPE,
    })
    return f"{_AUTHORIZE_URL}?{query}"


async def _exchange(settings: Settings, code: str) -> dict[str, Any]:
    """Exchange one authorization code and require Reddit's read scope and refresh token."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            _TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                              "redirect_uri": settings.connector_reddit_callback_url},
            headers={"Authorization": _client_auth(settings), "User-Agent": "UmweltOS/1.0 (self-hosted personal intelligence)"},
        )
        response.raise_for_status()
        token = response.json()
    access, refresh, expires, scopes = (
        token.get("access_token"), token.get("refresh_token"), token.get("expires_in"), token.get("scope"),
    )
    try:
        expires_in = int(expires)
    except (TypeError, ValueError):
        expires_in = 0
    if (
        not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
        or not 0 < expires_in <= 86400 or not isinstance(scopes, str)
        or _READ_SCOPE not in scopes.split() or token.get("token_type", "bearer").casefold() != "bearer"
    ):
        raise ValueError("reddit_readonly_grant_invalid")
    return {
        "access_token": access, "refresh_token": refresh,
        "access_expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "scopes": sorted(set(scopes.split())),
    }


async def _refresh(settings: Settings, refresh_token: str) -> dict[str, Any]:
    """Refresh Reddit access while preserving its long-lived refresh token when omitted."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            _TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Authorization": _client_auth(settings), "User-Agent": "UmweltOS/1.0 (self-hosted personal intelligence)"},
        )
        response.raise_for_status()
        token = response.json()
    access, expires = token.get("access_token"), token.get("expires_in")
    try:
        expires_in = int(expires)
    except (TypeError, ValueError):
        expires_in = 0
    if not isinstance(access, str) or not access or not 0 < expires_in <= 86400:
        raise ValueError("reddit_refresh_invalid")
    rotated = token.get("refresh_token", refresh_token)
    if not isinstance(rotated, str) or not rotated:
        raise ValueError("reddit_refresh_invalid")
    return {"access_token": access, "refresh_token": rotated,
            "access_expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()}


async def refresh_reddit_grant(
    session: AsyncSession, settings: Settings, source_id: UUID, *, workspace_id: UUID,
    owner_id: int, source_generation: int, configuration_revision: int, credential_revision: int,
) -> tuple[str, UUID, int]:
    """Read or refresh one Reddit token under its locked grant row and return its collection proof.

    Callers must first hold the scheduler/source admission fence and pass its captured revisions.
    The row lock serializes refresh across workers; a rotated token and incremented token revision
    commit together, so a concurrent worker cannot overwrite a newer refresh result.
    """
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == source_id,
    ).with_for_update())
    if (grant is None or grant.state != "ready" or grant.provider != "reddit"
            or grant.workspace_id != workspace_id or grant.owner_id != owner_id
            or grant.source_generation != source_generation
            or grant.configuration_revision != configuration_revision
            or grant.credential_revision != credential_revision or not grant.encrypted_tokens
            or _READ_SCOPE not in grant.granted_scopes):
        raise ValueError("reddit_grant_stale")
    key = settings.connector_credential_encryption_key.get_secret_value()
    try:
        tokens = _open(key, grant.encrypted_tokens, source_id, grant.operation_id,
                       source_generation, configuration_revision)
        if tokens.get("credential_revision") != credential_revision:
            raise ValueError("reddit_credential_revision_mismatch")
        access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise ValueError("reddit_grant_token_missing")
        if grant.token_expires_at is None or grant.token_expires_at <= datetime.now(UTC) + timedelta(seconds=60):
            rotated = await _refresh(settings, refresh)
            tokens.update(rotated)
            grant.token_revision += 1
            operation_id, token_revision = grant.operation_id, grant.token_revision
            grant.encrypted_tokens = _seal(key, source_id, grant.operation_id,
                source_generation, configuration_revision, tokens)
            grant.token_expires_at = datetime.fromisoformat(rotated["access_expires_at"])
            access = rotated["access_token"]
            await session.commit()
        else:
            token_revision = grant.token_revision
            operation_id = grant.operation_id
            await session.rollback()
            return access, operation_id, token_revision
    except Exception:  # Roll back every failure, then preserve its original type.
        await session.rollback()
        raise
    return access, operation_id, token_revision


async def _revoke(settings: Settings, token: str) -> None:
    """Ask Reddit to revoke one refresh token; callers retain a reconciliation state on failure."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            _REVOKE_URL, data={"token": token, "token_type_hint": "refresh_token"},
            headers={"Authorization": _client_auth(settings), "User-Agent": "UmweltOS/1.0 (self-hosted personal intelligence)"},
        )
        response.raise_for_status()


async def _admit(session: AsyncSession, request: Request, scope: WorkspaceContext, *, expected: AccessFence | None = None) -> AccessFence:
    """Lock the current owner/session/workspace fence before connector OAuth state changes."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await lock_access_fence(
        session, scope=scope, expected=expected,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),),
    )


async def _publish(session: AsyncSession, request: Request, scope: WorkspaceContext, fence: AccessFence) -> None:
    """Commit a Reddit OAuth transition only under its original owner/workspace fence."""
    await commit_with_replay(
        session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        access_fence=fence,
    )


async def _require_session(request: Request, session: AsyncSession, session_hash: str, owner_id: int) -> None:
    """Revalidate the browser session after a provider network call before publishing its response."""
    from core.auth.dependencies import SESSION_COOKIE

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie or not hmac.compare_digest(_digest(cookie), session_hash):
        raise HTTPException(status_code=401, detail="Owner session changed during authorization")
    row = await session.scalar(select(AuthSession).join(Owner, Owner.id == AuthSession.owner_id).where(
        AuthSession.token_hash == session_hash, AuthSession.owner_id == owner_id,
        AuthSession.expires_at > datetime.now(UTC), Owner.account_state == "active",
    ))
    if row is None:
        raise HTTPException(status_code=401, detail="Owner session expired during authorization")


async def _current_source(
    session: AsyncSession, request: Request, source_id: UUID, scope: WorkspaceContext, fence: AccessFence,
    *, allow_paused: bool = False,
) -> tuple[Any, ConnectorProvisioning]:
    """Lock and validate the Reddit source and its provisioning revision under the owner fence."""
    multi = request.app.state.settings.multi_workspace_enabled
    source_fence, row, _slots = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi, expected_access_fence=fence,
    )
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi)
    statuses = {"active", "paused"} if allow_paused else {"active"}
    if source is None or source.provider != "reddit" or source.status not in statuses or source_fence is None or row is None:
        raise HTTPException(status_code=404, detail="Reddit connector source not found")
    return source, row


@router.post("/{source_id}/oauth/reddit/start")
async def start_reddit_authorization(
    source_id: str, payload: RedditOAuthRequest, session: Session, request: Request,
    owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, str]:
    """Persist a ten-minute, one-use OAuth state bound to the current Reddit source and browser session."""
    settings = request.app.state.settings
    _configured(settings)
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Reddit connector source not found") from exc
    fence = await _admit(session, request, scope)
    source, row = await _current_source(session, request, parsed_id, scope, fence)
    if source.generation != payload.expected_source_generation or row.desired_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration changed; reload before connecting")
    grant = await session.get(ConnectorOAuthGrant, parsed_id)
    if grant is not None and (grant.provider != "reddit" or grant.workspace_id != scope.workspace_id or grant.owner_id != owner.owner_id):
        raise HTTPException(status_code=409, detail="Connector credential ownership changed; reload before connecting")
    token_revision = grant.token_revision if grant else 0
    state, operation_id = secrets.token_urlsafe(32), uuid7()
    attempt = ConnectorOAuthAttempt(
        id=operation_id, source_id=parsed_id, workspace_id=scope.workspace_id, owner_id=owner.owner_id,
        provider="reddit", session_hash=owner.token_hash, state_hash=_digest(state),
        # Reddit's confidential-client code flow does not accept PKCE; this encrypted marker maintains the shared
        # attempt model's non-null verifier field without inventing a PKCE guarantee.
        encrypted_verifier=_seal(settings.connector_credential_encryption_key.get_secret_value(), parsed_id,
                                 operation_id, source.generation, row.desired_revision, {"pkce": False}),
        source_generation=source.generation, configuration_revision=row.desired_revision,
        credential_revision=row.credential_revision, expected_token_revision=token_revision,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(attempt)
    await _publish(session, request, scope, fence)
    return {"authorization_url": _authorization_url(settings, state), "attempt_id": str(operation_id),
            "token_revision": str(token_revision)}


@router.get("/oauth/reddit/callback")
async def complete_reddit_authorization(request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite) -> RedirectResponse:
    """Consume a one-use Reddit callback and store tokens only if all captured owner/source revisions still match."""
    settings = request.app.state.settings
    _configured(settings)
    callback, actual = urlsplit(settings.connector_reddit_callback_url), urlsplit(str(request.url))
    if (actual.scheme, actual.netloc, actual.path) != (callback.scheme, callback.netloc, callback.path):
        raise HTTPException(status_code=400, detail="Reddit callback URL does not match registration")
    state = request.query_params.get("state", "")
    if not state or len(state) > 256:
        raise HTTPException(status_code=400, detail="Reddit authorization state is invalid")
    owner_session_hash, owner_id = owner.token_hash, owner.owner_id
    fence = await _admit(session, request, scope)
    attempt = await session.scalar(select(ConnectorOAuthAttempt).where(
        ConnectorOAuthAttempt.state_hash == _digest(state),
    ).with_for_update())
    now = datetime.now(UTC)
    if (attempt is None or attempt.provider != "reddit" or attempt.consumed_at is not None
            or attempt.expires_at <= now or attempt.session_hash != owner_session_hash
            or attempt.owner_id != owner_id or attempt.workspace_id != scope.workspace_id):
        raise HTTPException(status_code=400, detail="Reddit authorization expired or was already used")
    attempt.consumed_at = now
    source_id, operation_id = attempt.source_id, attempt.id
    source_generation, configuration_revision = attempt.source_generation, attempt.configuration_revision
    credential_revision, expected_token_revision = attempt.credential_revision, attempt.expected_token_revision
    await _publish(session, request, scope, fence)
    if "error" in request.query_params:
        return RedirectResponse(url="/settings/sources?oauth=reddit_denied", status_code=303)
    code = request.query_params.get("code", "")
    if not code or len(code) > 4096:
        raise HTTPException(status_code=400, detail="Reddit authorization code is invalid")
    await _require_session(request, session, owner_session_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, source_id, scope, fence)
    if (source.generation != source_generation or row.desired_revision != configuration_revision
            or row.credential_revision != credential_revision):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Connector changed before authorization; reconnect to continue")
    await session.rollback()
    try:
        tokens = await _exchange(settings, code)
    except Exception:  # noqa: BLE001  # Authlib transport/protocol failures have provider-specific types.
        await session.rollback()
        raise HTTPException(status_code=503, detail="Reddit authorization could not be verified; reconnect to retry") from None
    await _require_session(request, session, owner_session_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, source_id, scope, fence)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == source_id,
    ).with_for_update())
    current_token_revision = grant.token_revision if grant else 0
    if (source.generation != source_generation or row.desired_revision != configuration_revision
            or row.credential_revision != credential_revision or current_token_revision != expected_token_revision
            or grant is not None and (grant.provider != "reddit" or grant.workspace_id != scope.workspace_id or grant.owner_id != owner_id)):
        await session.rollback()
        try:
            await _revoke(settings, tokens["refresh_token"])
        except Exception:  # noqa: BLE001, S110
            pass
        raise HTTPException(status_code=409, detail="Connector changed during authorization; reconnect to continue")
    row.credential_revision += 1
    token_revision = current_token_revision + 1
    grant_values = {
        "source_id": source_id,
        "workspace_id": scope.workspace_id,
        "owner_id": owner_id,
        "provider": "reddit",
        "operation_id": operation_id,
        "source_generation": source_generation,
        "configuration_revision": configuration_revision,
        "credential_revision": row.credential_revision,
        "token_revision": token_revision,
        "encrypted_tokens": _seal(
            settings.connector_credential_encryption_key.get_secret_value(), source_id,
            operation_id, source_generation, configuration_revision,
            {**tokens, "credential_revision": row.credential_revision},
        ),
        "granted_scopes": tokens["scopes"],
        "token_expires_at": datetime.fromisoformat(tokens["access_expires_at"]),
        "state": "ready",
        "error_code": None,
    }
    if grant is None:
        session.add(ConnectorOAuthGrant(**grant_values))
    else:
        for name, value in grant_values.items():
            if name != "source_id":
                setattr(grant, name, value)
    await _publish(session, request, scope, fence)
    return RedirectResponse(url=f"/settings/sources?oauth=reddit_connected&source={source_id}", status_code=303)


@router.get("/{source_id}/oauth/reddit/status")
async def reddit_authorization_status(
    source_id: str, session: Session, request: Request, owner: OwnerRead, scope: WorkspaceRead,
) -> dict[str, Any]:
    """Return owner-visible Reddit authorization state and revision without exposing token values."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Reddit connector source not found") from exc
    source = await sources.get_connector_source(session, parsed_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if source is None or source.provider != "reddit":
        raise HTTPException(status_code=404, detail="Reddit connector source not found")
    grant = await session.get(ConnectorOAuthGrant, parsed_id)
    if grant is None:
        return {"state": "not_connected", "scopes": [], "token_revision": 0}
    if grant.provider != "reddit" or grant.workspace_id != scope.workspace_id or grant.owner_id != owner.owner_id:
        raise HTTPException(status_code=404, detail="Reddit connector source not found")
    return {"state": grant.state, "scopes": list(grant.granted_scopes),
            "token_revision": grant.token_revision, "expires_at": grant.token_expires_at,
            "error_code": grant.error_code}


@router.post("/{source_id}/oauth/reddit/disconnect")
async def disconnect_reddit_source(
    source_id: str, payload: RedditDisconnectRequest, session: Session, request: Request,
    owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, str]:
    """Disable collection before revocation; preserve an explicit reconciliation state if provider confirmation fails."""
    settings = request.app.state.settings
    _configured(settings)
    try:
        parsed_id = UUID(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Reddit connector source not found") from exc
    fence = await _admit(session, request, scope)
    source, row = await _current_source(session, request, parsed_id, scope, fence, allow_paused=True)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == parsed_id,
    ).with_for_update())
    if (source.generation != payload.expected_source_generation or row.desired_revision != payload.expected_revision
            or grant is None or grant.provider != "reddit" or grant.workspace_id != scope.workspace_id
            or grant.owner_id != owner.owner_id or grant.token_revision != payload.expected_token_revision
            or not grant.encrypted_tokens or grant.state not in {"ready", "reconciliation_required"}):
        raise HTTPException(status_code=409, detail="Reddit credential changed; reload before disconnecting")
    try:
        tokens = _open(settings.connector_credential_encryption_key.get_secret_value(), grant.encrypted_tokens,
                       parsed_id, grant.operation_id, grant.source_generation, grant.configuration_revision)
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("reddit_refresh_token_missing")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Reddit credential requires operator reconciliation") from exc
    owner_session_hash, owner_id = owner.token_hash, owner.owner_id
    row.credential_revision += 1
    grant.credential_revision = row.credential_revision
    grant.state, grant.error_code = "revocation_pending", None
    await _publish(session, request, scope, fence)
    try:
        await _revoke(settings, refresh_token)
    except Exception:  # noqa: BLE001  # Reconcile local revocation state for any provider/client failure.
        await session.rollback()
        fence = await _admit(session, request, scope, expected=fence)
        source, row = await _current_source(session, request, parsed_id, scope, fence, allow_paused=True)
        grant = await session.scalar(select(ConnectorOAuthGrant).where(
            ConnectorOAuthGrant.source_id == parsed_id,
        ).with_for_update())
        if grant is not None and grant.token_revision == payload.expected_token_revision and grant.state == "revocation_pending":
            grant.state, grant.error_code = "reconciliation_required", "revocation_unconfirmed"
            await _publish(session, request, scope, fence)
        else:
            await session.rollback()
        raise HTTPException(status_code=503, detail="Reddit access is disabled locally; provider revocation needs retry") from None
    await _require_session(request, session, owner_session_hash, owner_id)
    fence = await _admit(session, request, scope, expected=fence)
    source, row = await _current_source(session, request, parsed_id, scope, fence, allow_paused=True)
    grant = await session.scalar(select(ConnectorOAuthGrant).where(
        ConnectorOAuthGrant.source_id == parsed_id,
    ).with_for_update())
    if (grant is None or grant.provider != "reddit" or grant.owner_id != owner_id
            or grant.workspace_id != scope.workspace_id or grant.token_revision != payload.expected_token_revision
            or grant.state != "revocation_pending"):
        raise HTTPException(status_code=409, detail="Reddit credential changed during disconnect")
    grant.state, grant.encrypted_tokens, grant.granted_scopes = "revoked", None, []
    grant.token_expires_at, grant.error_code = None, None
    await _publish(session, request, scope, fence)
    return {"state": "revoked"}
