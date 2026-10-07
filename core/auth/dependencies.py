import hashlib
import hmac
import time
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import AuthSession
from core.auth.public import get_active_account
from core.config import Settings
from core.database import get_session

SESSION_COOKIE = "bbd_session"
CSRF_COOKIE = "bbd_csrf"
CSRF_MAX_AGE_SECONDS = 600


def _hash(value: str) -> str:
    """Return the SHA-256 digest used to persist opaque authentication tokens."""
    return hashlib.sha256(value.encode()).hexdigest()


def _origin_allowed(origin: str | None, settings: Settings) -> bool:
    """Compare the request Origin with the configured public origin after trimming trailing slashes."""
    return origin is not None and origin.rstrip("/") == str(settings.public_origin).rstrip("/")


def _csrf_signature(token: str, expires_at: int, settings: Settings) -> str:
    """Sign a CSRF token and expiry with the configured secret; fail closed when signing is unconfigured."""
    secret = settings.csrf_signing_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="CSRF protection is not configured")
    signed = f"{token}.{expires_at}"
    return hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()


def _valid_csrf(cookie: str | None, submitted: str | None, settings: Settings) -> bool:
    """Validate CSRF cookie shape, expiry window, submitted token, and constant-time signature equality."""
    if not cookie or not submitted:
        return False
    try:
        token, expiry, signature = cookie.rsplit(".", 2)
        expires_at = int(expiry)
    except ValueError:
        return False
    now = int(time.time())
    if expires_at <= now or expires_at > now + CSRF_MAX_AGE_SECONDS:
        return False
    return compare_digest(token, submitted) and compare_digest(
        signature, _csrf_signature(token, expires_at, settings)
    )


async def _current_session(
    request: Request,
    session: AsyncSession,
    token: str | None,
) -> AuthSession:
    """Resolve only a complete active bootstrap session for unconverted legacy auth paths.

    Nonbootstrap identities always fail here, including when the rollout flag is enabled.
    Account-scoped endpoints must deliberately select require_account instead.
    """
    row = await _account_session(request, session, token)
    if row.owner_id != 1:
        raise HTTPException(status_code=401, detail="Authentication required")
    return row


async def _account_session(request: Request, session: AsyncSession, token: str | None) -> AuthSession:
    """Authenticate a live token and complete active account, subject to the rollout gate.

    Attach a detached account identity for scoped dependencies; no membership or data access
    follows from this authentication. Missing, disabled or incomplete identities return 401.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    row = await session.get(AuthSession, _hash(token))
    if row is None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Authentication required")
    account = await get_active_account(
        session, row.owner_id, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if account is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    request.state.account = account
    request.state.auth_session = row
    return row


async def require_owner(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthSession:
    """Require the active bootstrap operator, permanently excluding other workspace owners."""
    return await _current_session(request, session, request.cookies.get(SESSION_COOKIE))


async def require_account(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthSession:
    """Require an active rollout-admitted account without granting legacy/operator authority."""
    return await _account_session(request, session, request.cookies.get(SESSION_COOKIE))


async def require_account_write(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthSession:
    """Require account-bound CSRF and durable backup admission before scoped write locks.

    This carries no instance privilege. Preserve the existing owner admission/finalizer
    protocol and commit admission before endpoint work. Anonymous invitation redemption
    needs a separately bounded origin/token/admission path in W1b.
    """
    settings: Settings = request.app.state.settings
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    auth_session = await require_account(request, session)
    if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings) or not compare_digest(
        auth_session.csrf_hash, _hash(csrf_token or "")
    ):
        raise HTTPException(status_code=403, detail="CSRF token is invalid", headers={"X-CSRF-Error": "invalid"})
    if getattr(request.state, "backup_activity", None) is None:
        from modules.backup.public import BackupAdmissionDenied
        from modules.settings.public import register_activity

        try:
            receipt = await register_activity(session, "api_account_write", request.url.path)
        except BackupAdmissionDenied as exc:
            raise HTTPException(
                status_code=503, detail="Writes are paused for a consistent backup", headers={"Retry-After": "30"},
            ) from exc
        request.state.backup_activity = receipt
        await session.commit()
    return auth_session


async def require_owner_write(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthSession:
    """Authenticate and record a durable backup-admitted owner request before domain locks.

    The activity row is committed before endpoint work so the backup coordinator can
    drain a request across any internal commits without holding an advisory lock over
    network waits. Its ASGI response finalizer publishes the terminal receipt.
    """
    auth_session = await _authorize_owner_write(request, session, origin, csrf_token)
    if getattr(request.state, "backup_activity", None) is None:
        from modules.backup.public import BackupAdmissionDenied
        from modules.settings.public import register_activity

        try:
            receipt = await register_activity(session, "api_owner_write", request.url.path)
        except BackupAdmissionDenied as exc:
            raise HTTPException(
                status_code=503, detail="Writes are paused for a consistent backup",
                headers={"Retry-After": "30"},
            ) from exc
        request.state.backup_activity = receipt
        # Persist admission before the handler can acquire an owner/source row lock.
        await session.commit()
    return auth_session


async def require_backup_owner_write(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthSession:
    """Authorize backup control writes without applying the ordinary maintenance gate."""
    return await _authorize_owner_write(request, session, origin, csrf_token)


async def _authorize_owner_write(
    request: Request,
    session: AsyncSession,
    origin: str | None,
    csrf_token: str | None,
) -> AuthSession:
    """Validate owner session, same-origin policy, and its session-bound signed CSRF proof."""
    settings: Settings = request.app.state.settings
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    auth_session = await require_owner(request, session)
    if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings) or not compare_digest(
        auth_session.csrf_hash, _hash(csrf_token or "")
    ):
        raise HTTPException(
            status_code=403,
            detail="CSRF token is invalid",
            headers={"X-CSRF-Error": "invalid"},
        )
    return auth_session


async def admit_identity_write(request: Request, session: AsyncSession, kind: str) -> None:
    """Register durable backup admission for anonymous/auth lifecycle writes before row locks.

    This never grants account, workspace or operator permission. Receipt finalization uses
    the existing response middleware; admission commits before identity transaction starts.
    Backup denial is a redacted retryable 503. Call only after origin/CSRF/bearer checks.
    """
    if getattr(request.state, "backup_activity", None) is not None:
        return
    from modules.backup.public import BackupAdmissionDenied
    from modules.settings.public import register_activity

    try:
        receipt = await register_activity(session, kind, request.url.path)
    except BackupAdmissionDenied:
        raise HTTPException(status_code=503, detail="Authentication writes are paused for a consistent backup",
                            headers={"Retry-After": "30"}) from None
    request.state.backup_activity = receipt
    await session.commit()
