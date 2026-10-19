"""Password-confirmed account deletion and a preissued browser-bound status receipt."""
import asyncio
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_models import AccountDeletionOperation
from core.auth.deletion_schemas import DeletionPreview, DeletionRequest, DeletionStatus
from core.auth.deletion_service import deletion_status, receipt_operation
from core.auth.dependencies import CSRF_COOKIE, SESSION_COOKIE, _hash, require_account, require_account_write
from core.auth.models import AuthSession
from core.auth.routes import _allow_attempt, _lock_owner_session, get_auth_redis
from core.auth.service import verify_password
from core.database import get_session
from core.workspaces.public import account_deletion_blockers

router = APIRouter(prefix="/api/v1/auth/account", tags=["auth"])
RECEIPT_COOKIE = "bbd_account_deletion"
RECEIPT_PATH = "/api/v1/auth/account"
RECEIPT_SECONDS = 7 * 24 * 3600


def receipt_token(request: Request) -> str | None:
    """Accept only the fixed-length cryptographically random receipt encoding."""
    value = request.cookies.get(RECEIPT_COOKIE, "")
    return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else None


def set_receipt_cookie(request: Request, response: Response, token: str, seconds: int = RECEIPT_SECONDS) -> None:
    """Keep status authority out of JavaScript/URLs and renew pending polling lifetime."""
    response.set_cookie(RECEIPT_COOKIE, token, httponly=True, secure=request.app.state.settings.secure_cookies,
                        samesite="strict", path=RECEIPT_PATH, max_age=seconds)
    response.headers["Cache-Control"] = "no-store"


@router.get("/deletion-preview", response_model=DeletionPreview)
async def preview_account_deletion(
    request: Request, response: Response,
    auth: Annotated[AuthSession, Depends(require_account)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeletionPreview:
    """Issue receipt before acceptance so a lost POST response remains recoverable."""
    token = receipt_token(request)
    if token is None:
        token = secrets.token_urlsafe(32)
    else:
        existing = await receipt_operation(session, _hash(token))
        # A browser may later sign in to another account; never reuse another operation's authority.
        if existing is not None and existing.account_id != auth.owner_id:
            token = secrets.token_urlsafe(32)
    set_receipt_cookie(request, response, token)
    _, blockers = await account_deletion_blockers(session, auth.owner_id)
    return DeletionPreview(can_delete=not blockers, blockers=blockers, workspace_count=1, instance_operator=auth.owner_id == 1)


@router.post("/deletion", response_model=DeletionStatus, status_code=202)
async def accept_account_deletion(
    body: DeletionRequest, request: Request, response: Response,
    auth: Annotated[AuthSession, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_auth_redis)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> DeletionStatus:
    """Atomically bind receipt, disable identity and revoke sessions after locked password recheck.

    Existing receipt recovery uses the status GET after logout. Account then session/workspace
    locks serialize competing auth/lifecycle writes; no provider or Redis I/O occurs under locks.
    """
    token = receipt_token(request)
    if token is None:
        raise HTTPException(status_code=409, detail={"code": "deletion_preview_required"})
    await _allow_attempt(request, redis, "account-deletion")
    owner, _ = await _lock_owner_session(request, session, auth, csrf_token)
    if not await asyncio.to_thread(verify_password, owner.password_hash, body.current_password.get_secret_value()):
        raise HTTPException(status_code=400, detail={"code": "password_incorrect", "message": "Password is incorrect"})
    workspace_id, blockers = await account_deletion_blockers(session, owner.id, lock=True)
    if blockers:
        raise HTTPException(status_code=409, detail={"code": blockers[0], "blockers": blockers})
    existing = await receipt_operation(session, _hash(token))
    if existing is not None:
        raise HTTPException(status_code=409, detail={"code": "deletion_preview_required"})
    operation = AccountDeletionOperation(receipt_hash=_hash(token), account_id=owner.id,
                                         workspace_id=workspace_id, status="queued", phase="external", cursor={},
                                         next_attempt_at=datetime.now(UTC))
    session.add(operation)
    owner.account_state = "disabled"
    await session.execute(delete(AuthSession).where(AuthSession.owner_id == owner.id))
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    set_receipt_cookie(request, response, token)
    return deletion_status(operation)


@router.get("/deletion-status", response_model=DeletionStatus)
async def account_deletion_status(
    request: Request, response: Response, session: Annotated[AsyncSession, Depends(get_session)],
) -> DeletionStatus:
    """Read safe progress using only receipt authority, including after session revocation."""
    response.headers["Cache-Control"] = "no-store"
    token = receipt_token(request)
    operation = await receipt_operation(session, _hash(token)) if token else None
    if operation is None:
        raise HTTPException(status_code=404, detail={"code": "deletion_receipt_unavailable"})
    assert token is not None
    seconds = RECEIPT_SECONDS if operation.expires_at is None else max(1, int((operation.expires_at - datetime.now(UTC)).total_seconds()))
    set_receipt_cookie(request, response, token, seconds)
    return deletion_status(operation)
