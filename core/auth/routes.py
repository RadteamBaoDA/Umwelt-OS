import asyncio
import hmac
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

import httpx
from authlib.common.errors import AuthlibBaseError
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from joserfc.errors import JoseError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import (
    CSRF_COOKIE,
    CSRF_MAX_AGE_SECONDS,
    SESSION_COOKIE,
    _csrf_signature,
    _current_session,
    _hash,
    _origin_allowed,
    _valid_csrf,
    require_owner_write,
)
from core.auth.google import GOOGLE_CALLBACK_PATH, GOOGLE_ISSUER, google_client
from core.auth.google_schemas import (
    GoogleStartRequest,
    GoogleStartResponse,
    GoogleStatus,
    ReauthenticateRequest,
)
from core.auth.models import AuthSession, GoogleIdentity, Owner
from core.auth.schemas import (
    AuthState,
    CsrfResponse,
    LoginRequest,
    SetupRequest,
    SetupResponse,
    SetupStatus,
)
from core.auth.service import hash_password, verify_password
from core.config import Settings
from core.database import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


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
    """Create the first owner only with configured setup token, valid Origin/CSRF, and throttling; handle concurrent creation as conflict."""
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

    owner = Owner(id=1, password_hash=hash_password(body.password))
    session.add(owner)
    try:
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
    """Validate Origin, CSRF, throttling, and password, then persist hashed session credentials and set response cookies."""
    settings: Settings = request.app.state.settings
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    await _allow_attempt(request, redis, "login")
    if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings):
        raise HTTPException(status_code=403, detail="CSRF token is invalid")
    owner = await _lock_owner(session, 1)
    if owner is None or not verify_password(owner.password_hash, body.password):
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
    """Lock owner then session in a consistent order, optionally validating the current CSRF token."""
    owner = await _lock_owner(session, stale_session.owner_id)
    if owner is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    auth_session = await _lock_auth_session(
        session, stale_session.token_hash, stale_session.owner_id
    )
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
    session: AsyncSession, subject: str
) -> GoogleIdentity | None:
    """Lock the Google identity matching the configured issuer and provider subject."""
    try:
        return await session.scalar(
            select(GoogleIdentity)
            .where(GoogleIdentity.issuer == GOOGLE_ISSUER, GoogleIdentity.subject == subject)
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
    """Return OAuth configuration and whether the owner currently has a linked Google identity."""
    linked = await session.get(GoogleIdentity, 1) is not None
    return GoogleStatus(configured=_google_configured(request.app.state.settings), linked=linked)


@router.post("/reauthenticate", status_code=204)
async def reauthenticate(
    request: Request,
    body: ReauthenticateRequest,
    auth_session: Annotated[AuthSession, Depends(require_owner_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Verify the owner password under row locks and update the session recent-authentication timestamp."""
    await _allow_attempt(request, redis, "reauthenticate")
    owner, auth_session = await _lock_owner_session(
        request, session, auth_session, csrf_token
    )
    if owner is None or not verify_password(owner.password_hash, body.password):
        raise HTTPException(status_code=403, detail="Password is incorrect")
    auth_session.reauthenticated_at = datetime.now(UTC)
    await session.commit()


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
    """Validate login/link intent and security state, then begin Google authorization with a short-lived state."""
    settings: Settings = request.app.state.settings
    auth_session: AuthSession | None = None
    if not _google_configured(settings):
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    if body.purpose == "login":
        if not _origin_allowed(origin, settings) or not _valid_csrf(
            request.cookies.get(CSRF_COOKIE), csrf_token, settings
        ):
            raise HTTPException(status_code=403, detail="CSRF token is invalid")
        await _allow_attempt(request, redis, "google")
    else:
        auth_session = await require_owner_write(request, session, origin, csrf_token)
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
    }
    if body.purpose == "link":
        # Release owner/session row locks before Redis state storage and external OAuth provider work.
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
    """Validate Google OAuth state and identity, then create or link an owner session under row locks."""
    settings: Settings = request.app.state.settings
    state = request.query_params.get("state", "")
    try:
        raw_transaction = (
            await redis.getdel(f"auth:google:state:{state}")
            if state and len(state) <= 256
            else None
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable") from exc
    try:
        transaction = json.loads(raw_transaction) if raw_transaction else None
    except json.JSONDecodeError:
        transaction = None
    binding = request.cookies.get("bbd_google_binding", "")
    response = Response(status_code=303)
    response.headers["location"] = "/login?google=error"
    if isinstance(transaction, dict) and transaction.get("purpose") == "link":
        response.headers["location"] = "/knowledge/documents?google=error"
    response.delete_cookie("bbd_google_binding", path=GOOGLE_CALLBACK_PATH)
    if (
        not isinstance(transaction, dict)
        or transaction.get("purpose") not in {"login", "link"}
        or not isinstance(transaction.get("binding"), str)
        or not binding
        or not hmac.compare_digest(transaction["binding"], _hash(binding))
        or not _google_configured(settings)
    ):
        return response

    try:
        async with asyncio.timeout(10):
            token = await google_client(settings).google.authorize_access_token(request)
    except (TimeoutError, httpx.HTTPError, AuthlibBaseError, JoseError, ValueError, RuntimeError):
        return response
    userinfo = token.get("userinfo")
    if (
        not userinfo
        or userinfo.get("iss") != GOOGLE_ISSUER
        or not isinstance(userinfo.get("sub"), str)
        or userinfo.get("email_verified") is not True
        or not isinstance(userinfo.get("email"), str)
    ):
        return response

    if transaction["purpose"] == "login":
        try:
            owner = await _lock_owner(session, 1)
            if owner is None:
                return response
            identity = await _lock_identity_for_subject(session, userinfo["sub"])
            if identity is None or identity.owner_id != owner.id:
                return response
            owner_id = owner.id
            reauthenticated_at = None
            response.headers["location"] = "/app"
        except HTTPException:
            return response
    else:
        owner_id = transaction.get("owner_id")
        if not isinstance(owner_id, int) or owner_id != 1:
            return response
        old_token = request.cookies.get(SESSION_COOKIE, "")
        try:
            owner = await _lock_owner(session, owner_id)
            if owner is None:
                return response
            auth_session = await _lock_auth_session(session, _hash(old_token), owner.id)
        except HTTPException:
            return response
        transaction_session_hash = transaction.get("session_hash")
        if not isinstance(transaction_session_hash, str) or not hmac.compare_digest(
            auth_session.token_hash, transaction_session_hash
        ):
            return response
        try:
            _require_recent_reauthentication(auth_session)
        except HTTPException:
            return response
        if auth_session.owner_id != owner.id:
            return response
        try:
            identity = await _lock_identity_for_owner(session, owner.id)
            subject_identity = await _lock_identity_for_subject(session, userinfo["sub"])
        except HTTPException:
            return response
        if subject_identity and subject_identity.owner_id != owner.id:
            return response
        if identity and (identity.issuer != GOOGLE_ISSUER or identity.subject != userinfo["sub"]):
            return response
        if identity is None:
            identity = GoogleIdentity(
                owner_id=auth_session.owner_id,
                issuer=GOOGLE_ISSUER,
                subject=userinfo["sub"],
                email=userinfo["email"],
            )
            session.add(identity)
        else:
            identity.email = userinfo["email"]
        owner_id = owner.id
        reauthenticated_at = auth_session.reauthenticated_at
        await session.delete(auth_session)
        response.headers["location"] = "/knowledge/documents?google=linked"

    session_token = secrets.token_urlsafe(32)
    csrf_token, csrf_cookie = _new_csrf(settings)
    session.add(
        AuthSession(
            token_hash=_hash(session_token),
            owner_id=owner_id,
            csrf_hash=_hash(csrf_token),
            reauthenticated_at=reauthenticated_at,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_lifetime_hours),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        response.headers["location"] = (
            "/login?google=error"
            if transaction["purpose"] == "login"
            else "/knowledge/documents?google=error"
        )
        return response
    _issue_auth_session(settings, response, request, session_token, csrf_cookie)
    return response


@router.post("/google/unlink", status_code=204)
async def google_unlink(
    request: Request,
    auth_session: Annotated[AuthSession, Depends(require_owner_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Require recent reauthentication and remove the owner Google identity transactionally."""
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
    """Return the current authenticated state and CSRF token for the active owner session."""
    row = await _current_session(request, session, request.cookies.get(SESSION_COOKIE))
    settings: Settings = request.app.state.settings
    existing_cookie = request.cookies.get(CSRF_COOKIE)
    existing_token = existing_cookie.split(".", 1)[0] if existing_cookie and "." in existing_cookie else None
    csrf_is_current = bool(
        existing_token
        and _valid_csrf(existing_cookie, existing_token, settings)
        and hmac.compare_digest(row.csrf_hash, _hash(existing_token))
    )
    if not csrf_is_current:
        from modules.settings.public import register_activity
        from modules.backup.public import BackupAdmissionDenied

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
    _owner, row = await _lock_owner_session(
        request, session, row, None, check_csrf=False
    )
    if csrf_is_current:
        assert existing_token is not None
        csrf_token = existing_token
    else:
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
    stale_session: Annotated[AuthSession, Depends(require_owner_write)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Invalidate the current session and clear authentication and CSRF cookies."""
    _owner, auth_session = await _lock_owner_session(
        request, session, stale_session, csrf_token
    )
    await session.delete(auth_session)
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
