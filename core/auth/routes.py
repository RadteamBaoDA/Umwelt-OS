import asyncio
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

import httpx
from authlib.common.errors import AuthlibBaseError  # type: ignore[import-untyped]  # no stubs
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from joserfc.errors import JoseError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import (
    CSRF_COOKIE,
    CSRF_MAX_AGE_SECONDS,
    SESSION_COOKIE,
    _csrf_signature,
    _account_session,
    _hash,
    _origin_allowed,
    _valid_csrf,
    require_account_write,
    admit_identity_write,
)
from core.auth.google import GOOGLE_CALLBACK_PATH, GOOGLE_ISSUER, google_client
from core.auth.google_schemas import (
    GoogleStartRequest,
    GoogleStartResponse,
    GoogleStatus,
    ReauthenticateRequest,
)
from core.auth.models import AuthSession, GoogleIdentity, Owner
from core.auth.public import provision_bootstrap_account_in_uow, get_active_account, normalize_account_email
from core.auth.schemas import (
    AuthState,
    ChangePasswordRequest,
    CsrfResponse,
    LoginRequest,
    SetupRequest,
    SetupResponse,
    SetupStatus,
)
from core.auth.service import hash_password, verify_password, verify_login_password
from core.config import Settings
from core.database import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = logging.getLogger("bbd.auth")
# Machine code lets the client branch without parsing English text.
_PASSWORD_INCORRECT = {"message": "Password is incorrect", "code": "password_incorrect"}


def _is_owner_conflict(exc: IntegrityError) -> bool:
    """Recognize only the singleton owner primary-key uniqueness violation in a wrapped database error."""
    original: BaseException | None = exc.orig
    while original is not None:
        if (
            getattr(original, "sqlstate", None) == "23505"
            and getattr(original, "constraint_name", None) == "owner_pkey"
        ):
            return True
        original = original.__cause__ or original.__context__
    return False


def get_auth_redis(request: Request) -> Redis:
    """Return the Redis client installed on application state for authentication throttling."""
    return cast(Redis, request.app.state.redis)


def _new_csrf(settings: Settings) -> tuple[str, str]:
    """Generate a random client token and signed, short-lived cookie value."""
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + CSRF_MAX_AGE_SECONDS
    return token, f"{token}.{expires_at}.{_csrf_signature(token, expires_at, settings)}"


def _set_csrf_cookie(request: Request, response: Response, value: str) -> None:
    """Set the signed CSRF cookie with HTTP-only, same-site, secure, path, and age settings."""
    settings: Settings = request.app.state.settings
    response.set_cookie(
        CSRF_COOKIE,
        value,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=CSRF_MAX_AGE_SECONDS,
    )


async def _allow_attempt(request: Request, redis: Redis, action: str) -> None:
    """Atomically increment per-address and global minute counters; reject Redis outages or exceeded limits."""
    minute = int(time.time() // 60)
    address = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if getattr(request.app.state.settings, "auth_trust_forwarded_for", False) is True and forwarded.strip():
        address = forwarded.rsplit(",", 1)[-1].strip()  # rightmost hop: the one our trusted proxy wrote
    keys = (f"auth:{action}:ip:{_hash(address)}:{minute}", f"auth:{action}:all:{minute}")
    try:
        pipeline = redis.pipeline(transaction=True)
        for key in keys:
            pipeline.incr(key)
            pipeline.expire(key, 120, nx=True)
        counts = await pipeline.execute()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable") from exc
    per_address, _, global_count, _ = counts
    if per_address > 5 or global_count > 20:
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts",
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )


@router.get("/setup-status", response_model=SetupStatus)
async def setup_status(session: Annotated[AsyncSession, Depends(get_session)]) -> SetupStatus:
    """Report whether the singleton owner row exists."""
    has_owner = await session.scalar(select(Owner.id).limit(1)) is not None
    return SetupStatus(setupRequired=not has_owner)


@router.post("/setup", response_model=SetupResponse, status_code=201)
async def create_owner(
    body: SetupRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    x_setup_token: Annotated[str | None, Header(alias="X-Setup-Token")] = None,
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SetupResponse:
    """Atomically create bootstrap account, default workspace and owner membership.

    Require setup token, Origin/CSRF and throttling; roll back the full identity unit if
    competing setup already created account 1. This endpoint never issues a session.
    """
    settings: Settings = request.app.state.settings
    configured_token = settings.setup_token.get_secret_value()
    if not configured_token:
        raise HTTPException(status_code=503, detail="Owner setup is not configured")
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    if not x_setup_token or not hmac.compare_digest(configured_token, x_setup_token):
        raise HTTPException(status_code=403, detail="Setup token is invalid")
    await _allow_attempt(request, redis, "setup")
    if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings):
        raise HTTPException(status_code=403, detail="CSRF token is invalid")

    try:
        await provision_bootstrap_account_in_uow(session, await asyncio.to_thread(hash_password, body.password))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _is_owner_conflict(exc):
            raise HTTPException(status_code=409, detail="Owner is already configured") from exc
        raise
    response.delete_cookie(CSRF_COOKIE, path="/")
    return SetupResponse(created=True)


@router.get("/csrf", response_model=CsrfResponse)
async def csrf(request: Request, response: Response) -> CsrfResponse:
    """Issue a client CSRF token and its signed HTTP-only cookie."""
    token, cookie = _new_csrf(request.app.state.settings)
    _set_csrf_cookie(request, response, cookie)
    return CsrfResponse(csrfToken=token)


@router.post("/login", response_model=AuthState)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthState:
    """Resolve exact normalized email (omission bootstrap1), lock account and issue active identity session.

    Origin/anonymous CSRF and bounded rate admission precede credentials. Never scan passwords;
    invalid identifier, absent account, wrong password and rollout denial share generic401.
    Recheck complete default/active identity after lock before committing hashed session state.
    """
    settings: Settings = request.app.state.settings
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    await _allow_attempt(request, redis, "login")
    if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings):
        raise HTTPException(status_code=403, detail="CSRF token is invalid")
    await admit_identity_write(request, session, "auth_password_login")
    if body.identifier is None:
        account_id = 1
    else:
        try:
            email = normalize_account_email(body.identifier)
        except ValueError:
            await asyncio.to_thread(verify_login_password, None, body.password)
            raise HTTPException(status_code=401, detail="Email or password is incorrect") from None
        account_id = await session.scalar(select(Owner.id).where(Owner.email == email))
    try:
        owner = await _lock_owner(session, account_id) if account_id is not None else None
    except HTTPException:
        await asyncio.to_thread(verify_login_password, None, body.password)
        raise HTTPException(status_code=401, detail="Email or password is incorrect") from None
    password_matches = await asyncio.to_thread(
        verify_login_password, owner.password_hash if owner else None, body.password,
    )
    if owner is None or not password_matches or await get_active_account(
        session, owner.id, multi_workspace_enabled=settings.multi_workspace_enabled,
    ) is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")

    session_token = secrets.token_urlsafe(32)
    next_csrf, csrf_cookie = _new_csrf(settings)
    auth_session = AuthSession(
        token_hash=_hash(session_token),
        owner_id=owner.id,
        csrf_hash=_hash(next_csrf),
        reauthenticated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_lifetime_hours),
    )
    session.add(auth_session)
    await session.commit()
    _issue_auth_session(settings, response, request, session_token, csrf_cookie)
    return AuthState(csrfToken=next_csrf)


def _google_configured(settings: Settings) -> bool:
    """Report whether both Google OAuth client credentials are configured."""
    return bool(settings.google_client_id and settings.google_client_secret.get_secret_value())


def _require_recent_reauthentication(auth_session: AuthSession) -> None:
    """Require a successful owner password check within the preceding five minutes."""
    timestamp = auth_session.reauthenticated_at
    age = datetime.now(UTC) - timestamp if timestamp is not None else None
    if age is None or age < timedelta(0) or age > timedelta(minutes=5):
        raise HTTPException(status_code=403, detail="Reauthentication required")


def _raise_if_owner_lock_busy(exc: DBAPIError) -> None:
    """Translate PostgreSQL nowait lock contention into a retryable authentication conflict."""
    cause: BaseException | None = exc.orig
    while cause is not None:
        if getattr(cause, "sqlstate", None) == "55P03":
            raise HTTPException(
                status_code=409, detail="Authentication state changed; retry the request"
            ) from exc
        cause = cause.__cause__ or cause.__context__


async def _lock_owner(session: AsyncSession, owner_id: int) -> Owner | None:
    """Lock the owner row with NOWAIT and map lock contention to the API conflict response."""
    try:
        return await session.scalar(
            select(Owner)
            .where(Owner.id == owner_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        _raise_if_owner_lock_busy(exc)
        raise


async def _lock_auth_session(
    session: AsyncSession, token_hash: str, owner_id: int
) -> AuthSession:
    """Lock and validate a live auth session belonging to the expected owner."""
    try:
        auth_session = await session.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        _raise_if_owner_lock_busy(exc)
        raise
    if (
        auth_session is None
        or auth_session.owner_id != owner_id
        or auth_session.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_session


async def _lock_owner_session(
    request: Request,
    session: AsyncSession,
    stale_session: AuthSession,
    csrf_token: str | None,
    *,
    check_csrf: bool = True,
) -> tuple[Owner, AuthSession]:
    """Lock account then session, rechecking complete active identity, feature gate and current CSRF.

    Reauthentication, logout and rotation share this ordered lock/reload. Inactive, incomplete
    or gate-denied accounts return401 even when an earlier dependency admitted the session.
    This grants account lifecycle authority only, never legacy operator/domain access.
    """
    owner = await _lock_owner(session, stale_session.owner_id)
    if owner is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    auth_session = await _lock_auth_session(
        session, stale_session.token_hash, stale_session.owner_id
    )
    if await get_active_account(
        session, owner.id, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ) is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if check_csrf and (
        not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, request.app.state.settings)
        or not hmac.compare_digest(auth_session.csrf_hash, _hash(csrf_token or ""))
    ):
        raise HTTPException(
            status_code=403,
            detail="CSRF token is invalid",
            headers={"X-CSRF-Error": "invalid"},
        )
    return owner, auth_session


async def _lock_owner_session_retrying(
    request: Request, session: AsyncSession, stale_session: AuthSession, attempts: int = 5
) -> tuple[Owner, AuthSession]:
    """Take the owner/session locks for CSRF rotation, briefly retrying NOWAIT contention.

    Rotation is idempotent for the caller, so concurrent tabs wait a few tens of milliseconds for
    each other instead of surfacing a 409. Still 409 if the locks stay busy after ``attempts``.
    """
    # rollback expires loaded rows; keep the two identifiers the lock helpers read on a detached copy.
    ids = AuthSession(token_hash=stale_session.token_hash, owner_id=stale_session.owner_id)
    for attempt in range(attempts):
        try:
            return await _lock_owner_session(request, session, ids, None, check_csrf=False)
        except HTTPException as exc:
            if exc.status_code != 409 or attempt == attempts - 1:
                raise
            await session.rollback()  # a failed NOWAIT aborts the transaction
            await asyncio.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")


async def _lock_identity_for_owner(
    session: AsyncSession, owner_id: int
) -> GoogleIdentity | None:
    """Lock the owner Google identity row with NOWAIT."""
    try:
        return await session.scalar(
            select(GoogleIdentity)
            .where(GoogleIdentity.owner_id == owner_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        _raise_if_owner_lock_busy(exc)
        raise


async def _lock_identity_for_subject(
    session: AsyncSession, subject: str, owner_id: int,
) -> GoogleIdentity | None:
    """Lock exact issuer/subject only beneath its already-locked account; never lock foreign identity.

    A changed association returns None rather than acquiring an undiscovered account lock.
    Linking races are rejected by issuer/subject uniqueness at the caller's atomic commit.
    """
    try:
        return await session.scalar(
            select(GoogleIdentity)
            .where(GoogleIdentity.issuer == GOOGLE_ISSUER, GoogleIdentity.subject == subject,
                   GoogleIdentity.owner_id == owner_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        _raise_if_owner_lock_busy(exc)
        raise


def _google_callback_url(settings: Settings) -> str:
    """Build the callback URL from the configured public origin and fixed callback path."""
    return f"{str(settings.public_origin).rstrip('/')}{GOOGLE_CALLBACK_PATH}"


@router.get("/google/status", response_model=GoogleStatus)
async def google_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GoogleStatus:
    """Return OAuth configuration and authenticated actor link state; anonymous reveals no identity."""
    linked = False
    if request.cookies.get(SESSION_COOKIE):
        auth = await _account_session(request, session, request.cookies.get(SESSION_COOKIE))
        linked = await session.get(GoogleIdentity, auth.owner_id) is not None
    return GoogleStatus(configured=_google_configured(request.app.state.settings), linked=linked)


@router.post("/reauthenticate", status_code=204)
async def reauthenticate(
    request: Request,
    body: ReauthenticateRequest,
    auth_session: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Verify current account password under account/session locks and active gate, then record reauth."""
    await _allow_attempt(request, redis, "reauthenticate")
    owner, auth_session = await _lock_owner_session(
        request, session, auth_session, csrf_token
    )
    if owner is None or not await asyncio.to_thread(verify_password, owner.password_hash, body.password):
        raise HTTPException(status_code=403, detail=_PASSWORD_INCORRECT)
    auth_session.reauthenticated_at = datetime.now(UTC)
    await session.commit()


@router.post("/password", response_model=AuthState)
async def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    auth_session: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthState:
    """Verify the current password, store the new hash, revoke every owner session, and issue a fresh one."""
    settings: Settings = request.app.state.settings
    # Same bucket as reauthenticate: both are password-guess oracles for a stolen session.
    await _allow_attempt(request, redis, "reauthenticate")
    owner, auth_session = await _lock_owner_session(request, session, auth_session, csrf_token)
    if not await asyncio.to_thread(verify_password, owner.password_hash, body.currentPassword):
        raise HTTPException(status_code=403, detail=_PASSWORD_INCORRECT)
    if await asyncio.to_thread(verify_password, owner.password_hash, body.newPassword):
        raise HTTPException(status_code=422, detail="New password must differ from the current password")
    owner.password_hash = await asyncio.to_thread(hash_password, body.newPassword)
    revoked = await session.execute(delete(AuthSession).where(AuthSession.owner_id == owner.id))
    session_token = secrets.token_urlsafe(32)
    next_csrf, csrf_cookie = _new_csrf(settings)
    session.add(
        AuthSession(
            token_hash=_hash(session_token),
            owner_id=owner.id,
            csrf_hash=_hash(next_csrf),
            reauthenticated_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_lifetime_hours),
        )
    )
    await session.commit()
    _issue_auth_session(settings, response, request, session_token, csrf_cookie)
    logger.info("owner password changed; sessions rotated, revoked: %s", getattr(revoked, "rowcount", 0))
    return AuthState(csrfToken=next_csrf)


@router.post("/google/start", response_model=GoogleStartResponse)
async def google_start(
    body: GoogleStartRequest,
    request: Request,
    response: Response,
    redis: Annotated[Redis, Depends(get_auth_redis)],
    session: Annotated[AsyncSession, Depends(get_session)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> GoogleStartResponse:
    """Begin linked login, recent-password account linking or valid-invitation Google enrollment.

    State contains only binding/account/session identifiers or invitation SHA256, never raw
    bearer/password. Release lifecycle SQL locks before Redis/provider I/O. Invite enrollment
    does not provision an account; callback verifies bound mailbox then issues a server proof.
    """
    settings: Settings = request.app.state.settings
    auth_session: AuthSession | None = None
    if not _google_configured(settings):
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    if body.purpose in {"login", "invitation"}:
        if not _origin_allowed(origin, settings) or not _valid_csrf(
            request.cookies.get(CSRF_COOKIE), csrf_token, settings
        ):
            raise HTTPException(status_code=403, detail="CSRF token is invalid")
        await _allow_attempt(request, redis, "google")
        if body.purpose == "invitation":
            if not settings.multi_workspace_enabled:
                raise HTTPException(status_code=403, detail="Invitation acceptance is not enabled")
            if request.cookies.get(SESSION_COOKIE) or body.invitation_token is None:
                raise HTTPException(status_code=403, detail="Invitation enrollment requires anonymous bearer proof")
            from core.workspaces.public import invitation_target

            await invitation_target(session, _hash(body.invitation_token.get_secret_value()))
    else:
        auth_session = await require_account_write(request, session, origin, csrf_token)
        if auth_session is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        _owner, auth_session = await _lock_owner_session(
            request, session, auth_session, csrf_token
        )
        _require_recent_reauthentication(auth_session)

    state = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    transaction = {
        "purpose": body.purpose,
        "binding": _hash(binding),
        "owner_id": auth_session.owner_id if auth_session else None,
        "session_hash": auth_session.token_hash if auth_session else None,
        "invitation_hash": _hash(body.invitation_token.get_secret_value())
        if body.purpose == "invitation" and body.invitation_token is not None else None,
        "csrf_cookie_hash": _hash(request.cookies.get(CSRF_COOKIE, "")),
    }
    # Release every SQL preparation transaction before Redis/provider I/O, including invitation reads.
    await session.rollback()
    try:
        stored = await redis.set(
            f"auth:google:state:{state}", json.dumps(transaction), ex=300, nx=True
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable") from exc
    if not stored:
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable")

    response.set_cookie(
        "bbd_google_binding",
        binding,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path=GOOGLE_CALLBACK_PATH,
        max_age=300,
    )
    oauth = google_client(settings)
    try:
        async with asyncio.timeout(10):
            authorization = await oauth.google.authorize_redirect(
                request, _google_callback_url(settings), state=state
            )
    except (TimeoutError, httpx.HTTPError, AuthlibBaseError, JoseError, ValueError, RuntimeError) as exc:
        try:
            await redis.getdel(f"auth:google:state:{state}")
        except RedisError:
            pass
        response.delete_cookie("bbd_google_binding", path=GOOGLE_CALLBACK_PATH)
        raise HTTPException(
            status_code=503, detail="Google sign-in provider is temporarily unavailable"
        ) from exc
    return GoogleStartResponse(authorization_url=authorization.headers["location"])


def _issue_auth_session(
    settings: Settings,
    response: Response,
    request: Request,
    session_token: str,
    csrf_cookie: str,
) -> None:
    """Set session and CSRF cookies with configured security and session lifetime attributes."""
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_lifetime_hours * 3600,
    )
    _set_csrf_cookie(request, response, csrf_cookie)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
) -> Response:
    """Consume browser-bound OAuth state and verified OIDC identity, then recheck locked auth.

    Linked issuer/subject alone resolves login; email never resolves an existing account.
    Link requires same active session and recent password reauth after locks. Invitation
    branch creates only a 5-minute, one-use, CSRF-cookie/invitation/email-bound Redis proof;
    password acceptance later atomically provisions account/default/membership. No SQL lock
    spans provider network. Callback writes receive backup admission before lifecycle locks.
    """
    settings: Settings = request.app.state.settings
    state = request.query_params.get("state", "")
    try:
        async with asyncio.timeout(2):
            raw = await redis.getdel(f"auth:google:state:{state}") if state and len(state) <= 256 else None
    except (RedisError, TimeoutError):
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable") from None
    try:
        transaction = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        transaction = None
    response = Response(status_code=303, headers={"location": "/login?google=error"})
    purpose = transaction.get("purpose") if isinstance(transaction, dict) else None
    if purpose == "link":
        response.headers["location"] = "/knowledge/documents?google=error"
    response.delete_cookie("bbd_google_binding", path=GOOGLE_CALLBACK_PATH)
    binding = request.cookies.get("bbd_google_binding", "")
    if (not isinstance(transaction, dict) or purpose not in {"login", "link", "invitation"}
            or not isinstance(transaction.get("binding"), str) or not binding
            or not hmac.compare_digest(transaction["binding"], _hash(binding)) or not _google_configured(settings)):
        return response
    # Provider exchange finishes before the first SQL lifecycle lock.
    try:
        async with asyncio.timeout(10):
            token = await google_client(settings).google.authorize_access_token(request)
    except (TimeoutError, httpx.HTTPError, AuthlibBaseError, JoseError, ValueError, RuntimeError):
        return response
    userinfo = token.get("userinfo")
    if (not userinfo or userinfo.get("iss") != GOOGLE_ISSUER
            or not isinstance(userinfo.get("sub"), str) or not userinfo["sub"] or len(userinfo["sub"]) > 255
            or userinfo.get("email_verified") is not True or not isinstance(userinfo.get("email"), str)):
        return response
    try:
        email = normalize_account_email(userinfo["email"])
    except ValueError:
        return response

    if purpose == "invitation":
        from core.workspaces.public import invitation_target

        digest = transaction.get("invitation_hash")
        if (not settings.multi_workspace_enabled or not isinstance(digest, str)
                or transaction.get("csrf_cookie_hash") != _hash(request.cookies.get(CSRF_COOKIE, ""))):
            return response
        try:
            target = await invitation_target(session, digest)
        except HTTPException:
            await session.rollback()
            return response
        if target.email != email:
            await session.rollback()
            return response
        await session.rollback()
        handle = secrets.token_urlsafe(32)
        proof = {"invitation_hash": digest, "email": email, "issuer": GOOGLE_ISSUER,
                 "subject": userinfo["sub"], "csrf_cookie_hash": transaction["csrf_cookie_hash"]}
        try:
            async with asyncio.timeout(2):
                stored = await redis.set(f"auth:google:enrollment:{_hash(handle)}", json.dumps(proof), ex=300, nx=True)
        except (RedisError, TimeoutError):
            return response
        if not stored:
            return response
        response.set_cookie("bbd_google_enrollment", handle, httponly=True, secure=settings.secure_cookies,
                            samesite="strict", path="/api/v1/workspaces/invitations/accept", max_age=300)
        # The browser continues its existing invitation/password page; no secret in redirect query.
        response.headers["location"] = "/invitations/accept?google=enrollment"
        return response

    await admit_identity_write(request, session, "auth_google_callback")
    try:
        if purpose == "login":
            # Resolve linked account IDs without locks, then account -> identity reload.
            owner_id = await session.scalar(select(GoogleIdentity.owner_id).where(
                GoogleIdentity.issuer == GOOGLE_ISSUER, GoogleIdentity.subject == userinfo["sub"],
            ))
            owner = await _lock_owner(session, owner_id) if owner_id is not None else None
            if owner is None or await get_active_account(
                session, owner.id, multi_workspace_enabled=settings.multi_workspace_enabled,
            ) is None:
                await session.rollback()
                return response
            identity = await _lock_identity_for_subject(session, userinfo["sub"], owner.id)
            if identity is None or identity.owner_id != owner.id:
                await session.rollback()
                return response
            reauthenticated_at = None
            response.headers["location"] = "/app"
        else:
            owner_id = transaction.get("owner_id")
            if not isinstance(owner_id, int) or isinstance(owner_id, bool):
                return response
            linked_user_id = await session.scalar(select(GoogleIdentity.owner_id).where(
                GoogleIdentity.issuer == GOOGLE_ISSUER, GoogleIdentity.subject == userinfo["sub"],
            ))
            if linked_user_id is not None and linked_user_id != owner_id:
                await session.rollback()
                return response
            owner = await _lock_owner(session, owner_id)
            if owner is None:
                await session.rollback()
                return response
            auth = await _lock_auth_session(session, _hash(request.cookies.get(SESSION_COOKIE, "")), owner.id)
            if (transaction.get("session_hash") != auth.token_hash or await get_active_account(
                    session, owner.id, multi_workspace_enabled=settings.multi_workspace_enabled,
                ) is None):
                await session.rollback()
                return response
            _require_recent_reauthentication(auth)
            identity = await _lock_identity_for_owner(session, owner.id)
            subject_identity = await _lock_identity_for_subject(session, userinfo["sub"], owner.id)
            if ((subject_identity is not None and subject_identity.owner_id != owner.id)
                    or (identity is not None and (identity.issuer != GOOGLE_ISSUER or identity.subject != userinfo["sub"]))):
                await session.rollback()
                return response
            if identity is None:
                session.add(GoogleIdentity(owner_id=owner.id, issuer=GOOGLE_ISSUER, subject=userinfo["sub"], email=email))
            else:
                identity.email = email
            # Verified provider evidence applies only to the exact bound account mailbox.
            if owner.email == email:
                owner.email_verified_at = datetime.now(UTC)
                owner.email_verification_source = "google_oidc"
            reauthenticated_at = auth.reauthenticated_at
            await session.delete(auth)
            response.headers["location"] = "/knowledge/documents?google=linked"
        # Owner row stays locked through issuance; recheck immediately before publishing session.
        if await get_active_account(session, owner.id, multi_workspace_enabled=settings.multi_workspace_enabled) is None:
            await session.rollback()
            response.headers["location"] = "/login?google=error"
            return response
        session_token = secrets.token_urlsafe(32)
        csrf_token, csrf_cookie = _new_csrf(settings)
        session.add(AuthSession(token_hash=_hash(session_token), owner_id=owner.id, csrf_hash=_hash(csrf_token),
                                reauthenticated_at=reauthenticated_at,
                                expires_at=datetime.now(UTC) + timedelta(hours=settings.session_lifetime_hours)))
        await session.commit()
    except (HTTPException, IntegrityError):
        await session.rollback()
        response.headers["location"] = "/knowledge/documents?google=error" if purpose == "link" else "/login?google=error"
        return response
    _issue_auth_session(settings, response, request, session_token, csrf_cookie)
    return response


@router.post("/google/unlink", status_code=204)
async def google_unlink(
    request: Request,
    auth_session: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Require active account/recent password reauthentication and unlink under account/session locks."""
    owner, auth_session = await _lock_owner_session(
        request, session, auth_session, csrf_token
    )
    _require_recent_reauthentication(auth_session)
    identity = await _lock_identity_for_owner(session, owner.id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Google account is not linked")
    if not owner.password_hash:
        raise HTTPException(status_code=409, detail="Cannot remove the last sign-in method")
    await session.delete(identity)
    await session.commit()


@router.get("/session", response_model=AuthState)
async def auth_session(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthState:
    """Return current account CSRF; rotate only after admission and locked active/gate revalidation."""
    row = await _account_session(request, session, request.cookies.get(SESSION_COOKIE))
    settings: Settings = request.app.state.settings
    existing_cookie = request.cookies.get(CSRF_COOKIE)
    existing_token = existing_cookie.split(".", 1)[0] if existing_cookie and "." in existing_cookie else None
    csrf_is_current = bool(
        existing_token
        and _valid_csrf(existing_cookie, existing_token, settings)
        and hmac.compare_digest(row.csrf_hash, _hash(existing_token))
    )
    if not csrf_is_current:
        from modules.backup.public import BackupAdmissionDenied
        from modules.settings.public import register_activity

        try:
            receipt = await register_activity(session, "auth_csrf_rotation", "auth/session")
        except BackupAdmissionDenied as exc:
            raise HTTPException(
                status_code=503, detail="Authentication writes are paused for a consistent backup",
                headers={"Retry-After": "30"},
            ) from exc
        request.state.backup_activity = receipt
        # Persist admission before locking the owner/session rows for rotation.
        await session.commit()
    if csrf_is_current:
        # Read-only: _current_session already validated existence and expiry, and nothing is written,
        # so no NOWAIT owner/session lock is needed (concurrent tabs must not 409 on a plain read).
        assert existing_token is not None
        csrf_token = existing_token
    else:
        _owner, row = await _lock_owner_session_retrying(request, session, row)
        csrf_token, csrf_cookie = _new_csrf(settings)
        row.csrf_hash = _hash(csrf_token)
        await session.commit()
        _set_csrf_cookie(request, response, csrf_cookie)
    return AuthState(csrfToken=csrf_token)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    stale_session: Annotated[AuthSession, Depends(require_account_write)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Invalidate active account session after ordered lock/CSRF recheck and clear browser cookies."""
    _owner, auth_session = await _lock_owner_session(
        request, session, stale_session, csrf_token
    )
    await session.delete(auth_session)
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
