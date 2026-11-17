"""Shared GitHub connector route plumbing: the ``router``, auth aliases, admission helpers, request models and the OAuth flow context."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import (
    SESSION_COOKIE,
    require_account,
    require_account_write,
    require_owner,
)
from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.config import Settings
from core.database import get_session
from core.errors import DomainError
from core.realtime import commit_with_replay
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.connectors import provisioning
from modules.connectors.github import oauth
from modules.connectors.models import (
    GithubOAuthCoordinator,
    GithubOAuthGrant,
    GithubOAuthOperation,
)

router = APIRouter(prefix="/api/v1/connectors", tags=["github-oauth"])
RECOVERY_GRACE = timedelta(minutes=2)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_account)]
OperatorRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_account_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
_DENIED = frozenset({401, 403, 404, 409})


def _flag(request: Request) -> bool:
    """Return the instance's actual configured multi-workspace flag; never defaulted or inferred."""
    return bool(request.app.state.settings.multi_workspace_enabled)


async def _admit(
    session: AsyncSession,
    request: Request,
    scope: WorkspaceContext,
    *,
    expected: AccessFence | None = None,
    lock: bool = True,
) -> AccessFence:
    """Reject members, then lock (or read) the account/session/workspace access fence first.

    Every route calls this before any Source, provisioning, grant or coordinator lock. Writes lock
    with the authenticated session reference so logout conflicts; ``expected`` re-admits the ORIGINAL
    fence after network I/O. The GitHub OAuth coordinator, operations and Source are then
    keyed by this actor and workspace, never by a constant owner.
    """
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if not lock:
        return await read_access_fence(session, scope=scope, multi_workspace_enabled=_flag(request))
    return await lock_access_fence(
        session,
        scope=scope,
        expected=expected,
        multi_workspace_enabled=_flag(request),
        auth_sessions=(authenticated_session_ref(request),),
    )


async def _publish(
    session: AsyncSession, request: Request, scope: WorkspaceContext, fence: AccessFence
) -> None:
    """Commit through the replay gate so a stale original fence never publishes this transaction."""
    await commit_with_replay(
        session, scope=scope, multi_workspace_enabled=_flag(request), access_fence=fence
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


async def _require_current_owner_session(
    request: Request, session: AsyncSession, owner: AuthSession
) -> None:
    """Re-read the exact token_hash-bound browser session so revoked sessions cannot publish OAuth state."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or hashlib.sha256(token.encode()).hexdigest() != owner.token_hash:
        raise HTTPException(status_code=401, detail="Owner session changed during GitHub operation")
    current = await session.scalar(
        select(AuthSession)
        .where(AuthSession.token_hash == owner.token_hash)
        .execution_options(populate_existing=True)
    )
    if (
        current is None
        or current.owner_id != owner.owner_id
        or current.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="Owner session expired during GitHub operation")


async def _mark_github_reconciliation(
    session: AsyncSession,
    request: Request,
    scope: WorkspaceContext,
    fence: AccessFence,
    source_id: UUID,
    operation_id: UUID,
    error_code: str,
) -> None:
    """Fence one uncertain OAuth operation under access, source, provisioning, grant, then coordinator locks.

    The ORIGINAL access fence is re-admitted. If that admission is gone, nothing is written: the
    operation stays ``in_progress`` and the owner recovers it through the normal stale-operation
    path once access is restored, rather than a revoked actor publishing state.
    """
    flag = _flag(request)
    try:
        await _admit(session, request, scope, expected=fence)
        source_fence, _provisioning, _slots = await provisioning.lock_connector(
            session,
            source_id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=scope,
            multi_workspace_enabled=flag,
            expected_access_fence=fence,
        )
    except (DomainError, HTTPException) as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
        return
    grant = await session.scalar(
        select(GithubOAuthGrant)
        .where(GithubOAuthGrant.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == scope.user_id)
        .with_for_update()
    )
    if coordinator is None or coordinator.operation_id != operation_id:
        await session.rollback()
        return
    coordinator.state, coordinator.error_code = "reconciliation_required", error_code
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == operation_id,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if operation is not None:
        operation.state = "reconciliation_required"
        operation.error_code = error_code
    should_fence_grant = error_code != "stale_authorization_outcome_unknown" and (
        not error_code.startswith("stale_refresh_")
        or grant is not None
        and grant.refresh_operation_id == operation_id
    )
    if (
        grant is not None
        and source_fence is not None
        and grant.state in {"ready", "refreshing"}
        and should_fence_grant
    ):
        grant.state, grant.error_code = "reconciliation_required", error_code
    await _publish(session, request, scope, fence)


def _configured(settings: Settings) -> None:
    """Fail closed unless App credentials and one exact registered callback are configured."""
    callback = urlsplit(settings.github_app_callback_url)
    if (
        not settings.github_app_client_id
        or not settings.github_app_client_secret.get_secret_value()
        or callback.scheme not in {"http", "https"}
        or callback.path != oauth.CALLBACK_PATH
        or callback.query
        or callback.fragment
        or callback.username
    ):
        raise HTTPException(status_code=503, detail="GitHub App connection is unavailable")


@dataclass(frozen=True)
class _FlowCtx:
    """Per-request inputs shared by the OAuth callback and disconnect phases (immutable; ``fence`` is the ORIGINAL fence)."""

    request: Request
    session: AsyncSession
    owner: Any
    scope: WorkspaceContext
    flag: bool
    actor: int
    fence: AccessFence
    settings: Settings
