"""Account/workspace management only; no implicit domain/source grants or public signup."""

import asyncio
import json
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import (
    CSRF_COOKIE, SESSION_COOKIE, _hash, _origin_allowed, _valid_csrf,
    admit_identity_write, require_account, require_account_write,
)
from core.auth.models import AuthSession
from core.auth.public import (
    account_id_for_email, get_active_account, lock_account_lifecycle, provision_invited_account_in_uow,
)
from core.auth.routes import get_auth_redis
from core.database import get_session
from core.workspaces.public import (
    accept_invitation_in_uow, create_invitation_in_uow, edit_workspace_in_uow, invitation_target,
    list_invitations, list_members, list_workspaces, remove_member_in_uow, revoke_invitation_in_uow,
)
from core.workspaces.schemas import (
    InvitationAccept, InvitationAccepted, InvitationCreate, InvitationCreated, InvitationList,
    MemberList, WorkspaceEdit, WorkspaceList, WorkspaceRead,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])
ENROLLMENT_COOKIE = "bbd_google_enrollment"
ACCEPT_PATH = "/api/v1/workspaces/invitations/accept"


def _if_match(value: str | None) -> int | None:
    """Parse one strong decimal revision; missing handled as 428 after owner visibility check."""
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    if not cleaned.isascii() or not cleaned.isdecimal() or int(cleaned) < 1:
        raise HTTPException(status_code=422, detail="Invalid workspace revision")
    return int(cleaned)


async def _lock_actor(request: Request, session: AsyncSession, auth: AuthSession, target: int | None = None) -> None:
    """After CSRF/admission, lock sorted account IDs then exact session and recheck account/CSRF."""
    ids = (auth.owner_id,) if target is None else (auth.owner_id, target)
    submitted = request.headers.get("X-CSRF-Token", "")
    await lock_account_lifecycle(
        session, ids, actor_user_id=auth.owner_id, token_hash=auth.token_hash,
        csrf_hash=_hash(submitted), multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )


async def _allow_invitation_attempt(request: Request, redis: Redis, digest: str) -> None:
    """Bound admission with SHA256 IP/token component and global counters; outages fail closed.

    First check IP/global caps before creating token-component keys, bounding random-token
    cardinality. Fixed minute buckets expire after 120 seconds; max5/IP, max20/global and
    max5/IP-token/minute. No raw address/bearer/email/password enters Redis keys or errors.
    """
    minute = int(time.time() // 60)
    address = request.client.host if request.client else "unknown"
    key = _hash(address)
    script = """
    local ip = redis.call('INCR', KEYS[1]); if ip == 1 then redis.call('EXPIRE', KEYS[1], 120) end
    local total = redis.call('INCR', KEYS[2]); if total == 1 then redis.call('EXPIRE', KEYS[2], 120) end
    if ip > 5 or total > 20 then return 0 end
    local pair = redis.call('INCR', KEYS[3]); if pair == 1 then redis.call('EXPIRE', KEYS[3], 120) end
    if pair > 5 then return 0 end
    return 1
    """
    try:
        async with asyncio.timeout(2):
            allowed = await redis.eval(script, 3, f"invite:ip:{key}:{minute}", f"invite:all:{minute}",
                                       f"invite:pair:{_hash(key + digest[:16])}:{minute}")
    except (RedisError, TimeoutError):
        raise HTTPException(status_code=503, detail="Invitation acceptance is temporarily unavailable") from None
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many invitation attempts", headers={"Retry-After": "60"})


async def _consume_google_enrollment(
    request: Request, redis: Redis, digest: str, email: str, *, required: bool,
) -> dict[str, str] | None:
    """Consume 5-minute one-time proof, bound to invitation digest/email and signed CSRF cookie.

    Proof handle travels only in HttpOnly cookie; issuer/subject/email are Redis-only. GETDEL
    consumes once before transaction, including failures/races. Missing proof permits ordinary
    invitation enrollment, but a supplied expired/replayed/mismatched proof uniformly fails410.
    """
    handle = request.cookies.get(ENROLLMENT_COOKIE)
    if handle is None and not required:
        return None
    if handle is None:
        raise HTTPException(status_code=410, detail="Enrollment is no longer available")
    try:
        async with asyncio.timeout(2):
            raw = await redis.getdel(f"auth:google:enrollment:{_hash(handle)}") if len(handle) <= 256 else None
        proof = json.loads(raw) if raw else None
    except (RedisError, TimeoutError):
        raise HTTPException(status_code=503, detail="Authentication is temporarily unavailable") from None
    except (ValueError, TypeError):
        proof = None
    if (not isinstance(proof, dict) or proof.get("invitation_hash") != digest or proof.get("email") != email
            or proof.get("csrf_cookie_hash") != _hash(request.cookies.get(CSRF_COOKIE, ""))
            or not isinstance(proof.get("subject"), str) or not isinstance(proof.get("issuer"), str)):
        raise HTTPException(status_code=410, detail="Enrollment is no longer available")
    return proof


@router.get("", response_model=WorkspaceList)
async def workspace_list(
    auth: Annotated[AuthSession, Depends(require_account)], session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceList:
    """List actor memberships only; selected workspace header never broadens enumeration."""
    return WorkspaceList(items=await list_workspaces(session, auth.owner_id))


@router.patch("/{workspace_id}", response_model=WorkspaceRead)
async def workspace_edit(
    workspace_id: UUID, body: WorkspaceEdit, request: Request,
    auth: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkspaceRead:
    """Rename actual owner workspace after current auth locks and body revision CAS."""
    await _lock_actor(request, session, auth)
    result = await edit_workspace_in_uow(session, workspace_id, auth.owner_id, body.name, body.expected_revision)
    await session.commit()
    return result


@router.get("/{workspace_id}/invitations", response_model=InvitationList)
async def invitation_list(
    workspace_id: UUID, auth: Annotated[AuthSession, Depends(require_account)],
    session: Annotated[AsyncSession, Depends(get_session)], cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> InvitationList:
    """Owner-only paginated invitation metadata; secrets/digests are never serialized."""
    return await list_invitations(session, workspace_id, auth.owner_id, cursor=cursor, limit=limit)


@router.get("/{workspace_id}/members", response_model=MemberList)
async def member_list(
    workspace_id: UUID, auth: Annotated[AuthSession, Depends(require_account)],
    session: Annotated[AsyncSession, Depends(get_session)], cursor: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> MemberList:
    """Owner-only paginated member identities and membership revisions."""
    return await list_members(session, workspace_id, auth.owner_id, cursor=cursor, limit=limit)


@router.post("/{workspace_id}/invitations", response_model=InvitationCreated, status_code=201)
async def invitation_create(
    workspace_id: UUID, body: InvitationCreate, request: Request,
    auth: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InvitationCreated:
    """Commit owner CAS invitation and return the only bearer URL once, with 7-day expiry."""
    await _lock_actor(request, session, auth)
    invitation_id, secret, expiry = await create_invitation_in_uow(
        session, workspace_id, auth.owner_id, body.email, body.expected_revision,
    )
    await session.commit()
    origin = str(request.app.state.settings.public_origin).rstrip("/")
    return InvitationCreated(invitation_id=invitation_id, invitation_url=f"{origin}/invitations/accept?token={secret}",
                             expires_at=expiry)


@router.delete("/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def invitation_revoke(
    workspace_id: UUID, invitation_id: UUID, request: Request,
    auth: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    revision: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    """Commit current-owner token revocation under If-Match; stale409/missing428."""
    await _lock_actor(request, session, auth)
    await revoke_invitation_in_uow(session, workspace_id, auth.owner_id, invitation_id, _if_match(revision))
    await session.commit()


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def member_remove(
    workspace_id: UUID, user_id: int, request: Request,
    auth: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    revision: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    """Commit owner-only nonowner removal with sorted actor/target auth locks and If-Match."""
    await _lock_actor(request, session, auth, user_id)
    await remove_member_in_uow(session, workspace_id, auth.owner_id, user_id, _if_match(revision))
    await session.commit()


@router.post("/invitations/accept", response_model=InvitationAccepted)
async def invitation_accept(
    body: InvitationAccept, request: Request, response: Response,
    session: Annotated[AsyncSession, Depends(get_session)], redis: Annotated[Redis, Depends(get_auth_redis)],
    origin: Annotated[str | None, Header()] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> InvitationAccepted:
    """Atomic invited-account/default/membership/token consume without automatic login.

    Flag/origin and bounded hashed admission guard anonymous acceptance; signed-in accounts
    retain session CSRF. Resolve IDs first, auth rows sorted -> session -> workspace/member
    -> token; real-password provisioning remains uncommitted until token recheck. Email races
    roll back all rows and require explicit winning-account login, never email-only joining.
    """
    settings = request.app.state.settings
    if not settings.multi_workspace_enabled:
        raise HTTPException(status_code=403, detail="Invitation acceptance is not enabled")
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="Origin is not allowed")
    digest = _hash(body.token.get_secret_value())
    await _allow_invitation_attempt(request, redis, digest)
    signed_in = SESSION_COOKIE in request.cookies
    auth = None
    if signed_in:
        auth = await require_account_write(request, session, origin, csrf_token)
    else:
        # Anonymous CSRF bootstrap already exists for login; bind Google proof to the same cookie.
        if not _valid_csrf(request.cookies.get(CSRF_COOKIE), csrf_token, settings):
            raise HTTPException(status_code=403, detail="CSRF token is invalid")
        await admit_identity_write(request, session, "invitation_accept")
    target = await invitation_target(session, digest)
    existing = await account_id_for_email(session, target.email)
    if signed_in and body.google_enrollment:
        raise HTTPException(status_code=403, detail="Google enrollment requires anonymous invitation acceptance")
    proof = await _consume_google_enrollment(
        request, redis, digest, target.email, required=body.google_enrollment,
    ) if not signed_in else None
    if proof is not None:
        response.delete_cookie(ENROLLMENT_COOKIE, path=ACCEPT_PATH)
    try:
        if auth is not None:
            actor = await lock_account_lifecycle(
                session, (auth.owner_id, target.owner_user_id), actor_user_id=auth.owner_id,
                token_hash=auth.token_hash, csrf_hash=_hash(csrf_token or ""),
                multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            assert actor is not None
            if actor.email != target.email:
                raise HTTPException(status_code=403, detail="Invitation belongs to another account")
        else:
            if existing is not None or body.password is None:
                raise HTTPException(status_code=401, detail="Authentication or password enrollment required")
            await lock_account_lifecycle(session, (target.owner_user_id,))
            actor = await provision_invited_account_in_uow(
                session, target.email, body.password.get_secret_value(),
                google_subject=proof["subject"] if proof else None, google_issuer=proof["issuer"] if proof else None,
            )
        if not settings.multi_workspace_enabled or await get_active_account(
            session, target.owner_user_id, multi_workspace_enabled=settings.multi_workspace_enabled,
        ) is None:
            raise HTTPException(status_code=410, detail="Invitation is no longer available")
        result = await accept_invitation_in_uow(
            session, target, digest, actor.id, target.email, new_account=auth is None,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=401, detail="Authentication or password enrollment required") from None
    return result
