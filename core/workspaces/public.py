"""Detached workspace lifecycle seams; caller owns admission, auth locks and final commit."""

from hashlib import sha256
from hmac import compare_digest
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from core.workspaces.models import Workspace, WorkspaceMembership, WorkspaceInvitation
from core.workspaces.schemas import (
    WorkspaceContext, WorkspaceRead, InvitationTarget, InvitationRead, MemberRead,
    InvitationList, MemberList, InvitationAccepted,
    GrantRef, ShareKind, ShareList, ShareRead, ShareUpsert,
)
from core.workspaces.access import (
    authorize_internal_job as authorize_internal_job,
    lock_access_fence as lock_access_fence,
    lock_owner_management, lock_workspace_memberships,
    read_access_fence as read_access_fence,
)


def new_invitation_secret() -> tuple[str, str]:
    """Return a one-time browser token and its storage-only SHA-256 digest."""
    token = token_urlsafe(32)
    return token, sha256(token.encode("utf-8")).hexdigest()


async def provision_default_workspace_in_uow(session: AsyncSession, user_id: int) -> UUID:
    """Create an owned workspace and owner membership for an already-flushed account.

    Auth owns credentials and the default pointer. This performs no commit or admission;
    the caller must set that pointer and commit the entire identity unit atomically. Unique
    owner and composite-FK constraints reject duplicate or mismatched provisioning.
    """
    workspace_id = uuid4()
    session.add(Workspace(id=workspace_id, name="Private workspace", owner_user_id=user_id))
    await session.flush()
    session.add(WorkspaceMembership(
        workspace_id=workspace_id, user_id=user_id, role="owner", owner_user_id=user_id,
    ))
    await session.flush()
    return workspace_id


async def resolve_workspace_context(
    session: AsyncSession, actor_user_id: int, workspace_id: UUID,
) -> WorkspaceContext | None:
    """Resolve an actor's actual membership without leaking a nonmember workspace.

    Reads no domain data, takes no locks and grants no resource access. Callers performing
    mutations or deferred publication must recheck the relevant identity/revision fence.
    """
    row = (await session.execute(
        select(Workspace, WorkspaceMembership).join(
            WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id,
        ).where(Workspace.id == workspace_id, WorkspaceMembership.user_id == actor_user_id)
        .execution_options(populate_existing=True)
    )).one_or_none()
    if row is None:
        return None
    workspace, membership = row
    if membership.role not in {"owner", "member"}:
        return None
    if membership.role == "owner" and (
        workspace.owner_user_id != actor_user_id or membership.owner_user_id != actor_user_id
    ):
        return None
    return WorkspaceContext(
        user_id=actor_user_id, workspace_id=workspace.id,
        role=cast(Literal["owner", "member"], membership.role), membership_revision=membership.revision,
    )


async def resolve_workspace_owner_context(
    session: AsyncSession, workspace_id: UUID, *, multi_workspace_enabled: bool,
) -> WorkspaceContext | None:
    """Resolve identity-only owned-default scope for a durably bound internal scheduler subject.

    Source/job owner must first load the durable record and prove its workspace binding;
    never use a request header/guessed workspace to borrow the target owner's authority.
    This returns no content, credentials, session or authorization. Caller constructs the
    durable InternalJobScope, then authorize_internal_job before Source/resource rereads.

    Uses auth public active/default admission and current owner membership. No locks, commit
    or external I/O. Missing/inactive/invalid owner lineage returns None; concurrent visible
    revision disagreement returns 409 for fresh preparation. Explicit configured gate is
    required, never inferred from actor ID and never a global current workspace.
    """
    from core.auth.public import get_active_account

    if not isinstance(workspace_id, UUID) or type(multi_workspace_enabled) is not bool:
        raise ValueError("A workspace ID and explicit boolean feature gate are required")
    owner_user_id = await session.scalar(
        select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
    )
    if owner_user_id is None:
        return None
    account = await get_active_account(
        session, owner_user_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if account is None or account.default_workspace_id != workspace_id:
        return None
    context = await resolve_workspace_context(session, account.id, workspace_id)
    if context is None or context.role != "owner":
        return None
    try:
        await read_access_fence(session, scope=context, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException as exc:
        if exc.status_code in {401, 404}:
            return None
        raise
    return context


def _workspace_read(workspace: Workspace, membership: WorkspaceMembership) -> WorkspaceRead:
    """Detach metadata; actor default is true only for the matching owner membership."""
    return WorkspaceRead(
        id=workspace.id, name=workspace.name, owner_user_id=workspace.owner_user_id,
        is_default=workspace.is_default and membership.user_id == workspace.owner_user_id,
        role=cast(Literal["owner", "member"], membership.role),
        configuration_revision=workspace.configuration_revision,
    )


async def list_workspaces(session: AsyncSession, user_id: int) -> list[WorkspaceRead]:
    """List only actual actor memberships; exposes metadata without granting domain content."""
    rows = await session.execute(
        select(Workspace, WorkspaceMembership).join(
            WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id,
        ).where(WorkspaceMembership.user_id == user_id).order_by(Workspace.created_at, Workspace.id)
    )
    return [_workspace_read(workspace, membership) for workspace, membership in rows]


async def list_workspace_job_candidate_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return one ordered identity page, with positive limits capped at 100 and UUID keyset cursor.

    This identity-only projection reads no account, membership, settings, credentials or
    domain content and performs no admission, locking, mutation or commit. Candidate IDs
    convey no scheduling permission; the worker must resolve and admit each workspace owner
    through the existing Workspace APIs before any effect.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("Workspace candidate page limit must be a positive integer")
    if after is not None and not isinstance(after, UUID):
        raise ValueError("Workspace candidate cursor must be a UUID")
    query = select(Workspace.id)
    if after is not None:
        query = query.where(Workspace.id > after)
    rows = await session.scalars(query.order_by(Workspace.id).limit(min(limit, 100)))
    return tuple(rows)


async def _owner_management_read(session: AsyncSession, workspace_id: UUID, user_id: int) -> None:
    """Require actual owner membership; missing/invisible 404 and member-only 403."""
    scope = await resolve_workspace_context(session, user_id, workspace_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


async def list_invitations(
    session: AsyncSession, workspace_id: UUID, user_id: int, *, cursor: UUID | None = None, limit: int = 100,
) -> InvitationList:
    """Return an owner-only keyset page with no token hashes/secrets; limit bounded to 100."""
    await _owner_management_read(session, workspace_id, user_id)
    query = select(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id)
    if cursor is not None:
        query = query.where(WorkspaceInvitation.id > cursor)
    size = min(max(limit, 1), 100)
    rows = list(await session.scalars(query.order_by(WorkspaceInvitation.id).limit(size + 1)))
    return InvitationList(items=[InvitationRead(
        id=row.id, email=row.email, created_at=row.created_at, expires_at=row.expires_at,
        accepted_at=row.accepted_at, accepted_by_user_id=row.accepted_by_user_id, revoked_at=row.revoked_at,
    ) for row in rows[:size]], next_cursor=rows[size - 1].id if len(rows) > size else None)


async def list_members(
    session: AsyncSession, workspace_id: UUID, user_id: int, *, cursor: int | None = None, limit: int = 100,
) -> MemberList:
    """Return an owner-only keyset identity page via auth-owned detached email lookup."""
    from core.auth.public import account_emails

    await _owner_management_read(session, workspace_id, user_id)
    query = select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id)
    if cursor is not None:
        query = query.where(WorkspaceMembership.user_id > cursor)
    size = min(max(limit, 1), 100)
    rows = list(await session.scalars(query.order_by(WorkspaceMembership.user_id).limit(size + 1)))
    emails = await account_emails(session, (row.user_id for row in rows[:size]))
    return MemberList(items=[MemberRead(
        user_id=row.user_id, email=emails.get(row.user_id),
        role=cast(Literal["owner", "member"], row.role), membership_revision=row.revision,
    ) for row in rows[:size]], next_cursor=rows[size - 1].user_id if len(rows) > size else None)


async def edit_workspace_in_uow(
    session: AsyncSession, workspace_id: UUID, user_id: int, name: str, expected_revision: int | None,
) -> WorkspaceRead:
    """Owner rename under auth->workspace->membership locks and CAS; commit owned by route."""
    workspace, memberships = await lock_owner_management(
        session, workspace_id, user_id, expected_revision=expected_revision,
    )
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 160:
        raise HTTPException(status_code=422, detail="Invalid workspace name")
    if workspace.name != cleaned:
        workspace.name = cleaned
        workspace.configuration_revision += 1
        memberships[user_id].revision += 1
    return _workspace_read(workspace, memberships[user_id])


async def create_invitation_in_uow(
    session: AsyncSession, workspace_id: UUID, user_id: int, email: str, expected_revision: int | None,
) -> tuple[UUID, str, datetime]:
    """Owner CAS creates a 7-day email-bound invitation; only SHA256 is persisted.

    Secret is returned once to the committing route. Auth account/session locks precede this
    helper. Bump configuration/owner membership revision atomically with the new invitation.
    """
    from core.auth.public import normalize_account_email

    workspace, memberships = await lock_owner_management(
        session, workspace_id, user_id, expected_revision=expected_revision,
    )
    try:
        normalized = normalize_account_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid invitation email") from None
    secret, digest = new_invitation_secret()
    invitation_id, now = uuid4(), datetime.now(UTC)
    expiry = now + timedelta(days=7)
    session.add(WorkspaceInvitation(
        id=invitation_id, workspace_id=workspace_id, invited_by_user_id=user_id,
        email=normalized, token_hash=digest, created_at=now, expires_at=expiry,
    ))
    workspace.configuration_revision += 1
    memberships[user_id].revision += 1
    await session.flush()
    return invitation_id, secret, expiry


def _invitation_live(invitation: WorkspaceInvitation | None, digest: str) -> bool:
    """Check exact hash, expiry and single-use terminal state without exposing bearer data."""
    return bool(invitation is not None and compare_digest(invitation.token_hash, digest)
                and invitation.expires_at > datetime.now(UTC)
                and invitation.accepted_at is None and invitation.revoked_at is None)


async def invitation_target(session: AsyncSession, digest: str) -> InvitationTarget:
    """Resolve live invitation IDs/email without locks for initial account discovery/OIDC only.

    Caller must repeat the exact hash/email/state checks after auth/workspace/membership/token
    locks before consumption. Invalid, expired, revoked and replayed tokens uniformly return 410.
    """
    row = await session.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.token_hash == digest)
                               .execution_options(populate_existing=True))
    if not _invitation_live(row, digest):
        raise HTTPException(status_code=410, detail="Invitation is no longer available")
    assert row is not None
    return InvitationTarget(row.id, row.workspace_id, row.invited_by_user_id, row.email)


async def revoke_invitation_in_uow(
    session: AsyncSession, workspace_id: UUID, user_id: int, invitation_id: UUID, expected_revision: int | None,
) -> None:
    """Owner CAS revoke locks token after workspace/member; terminal tokens return 410."""
    workspace, memberships = await lock_owner_management(
        session, workspace_id, user_id, expected_revision=expected_revision,
    )
    row = await session.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.id == invitation_id, WorkspaceInvitation.workspace_id == workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if not _invitation_live(row, row.token_hash):
        raise HTTPException(status_code=410, detail="Invitation is no longer available")
    row.revoked_at = datetime.now(UTC)
    workspace.configuration_revision += 1
    memberships[user_id].revision += 1


async def remove_member_in_uow(
    session: AsyncSession, workspace_id: UUID, user_id: int, target_user_id: int, expected_revision: int | None,
) -> None:
    """Owner CAS removal excludes owner; revision increments serialize later content revocation.

    Caller has locked actor/target auth rows sorted. Delete membership in this transaction;
    regrant uses a newer configuration revision so old membership snapshots never resurrect.
    W3 grants must bind membership revision and cannot authorize missing/replaced membership.
    """
    workspace, memberships = await lock_owner_management(
        session, workspace_id, user_id, expected_revision=expected_revision, target_user_id=target_user_id,
    )
    target = memberships.get(target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner" or target_user_id == workspace.owner_user_id:
        raise HTTPException(status_code=403, detail="Workspace owner cannot be removed")
    target.revision += 1
    await session.delete(target)
    workspace.configuration_revision += 1
    memberships[user_id].revision += 1


async def accept_invitation_in_uow(
    session: AsyncSession, target: InvitationTarget, digest: str, user_id: int, email: str,
    *, new_account: bool,
) -> InvitationAccepted:
    """Consume exact live email-bound token after caller auth locks/provisioning, atomically.

    Lock invited existing workspace -> owner/recipient memberships sorted -> invitation;
    recheck issuer/email/hash/state after locks. Missing owner state/invitation 410, wrong
    signed-in email 403. New account/default rows remain uncommitted until route commits.
    Existing membership consumes token without granting extra authority or minting a session.
    """
    workspace, memberships = await lock_workspace_memberships(
        session, target.workspace_id, (target.owner_user_id, user_id),
    )
    invitation = await session.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.id == target.invitation_id,
        WorkspaceInvitation.workspace_id == target.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    issuer = memberships.get(target.owner_user_id)
    if (not _invitation_live(invitation, digest) or issuer is None or issuer.role != "owner"
            or issuer.owner_user_id != target.owner_user_id or workspace.owner_user_id != target.owner_user_id):
        raise HTTPException(status_code=410, detail="Invitation is no longer available")
    assert invitation is not None
    if invitation.email != target.email or invitation.invited_by_user_id != target.owner_user_id:
        raise HTTPException(status_code=410, detail="Invitation is no longer available")
    if email != invitation.email:
        raise HTTPException(status_code=403, detail="Invitation belongs to another account")
    workspace.configuration_revision += 1
    issuer.revision += 1
    membership = memberships.get(user_id)
    if membership is None:
        membership = WorkspaceMembership(
            workspace_id=workspace.id, user_id=user_id, role="member",
            revision=workspace.configuration_revision,
        )
        session.add(membership)
    invitation.accepted_at = datetime.now(UTC)
    invitation.accepted_by_user_id = user_id
    await session.flush()
    default = None
    if new_account:
        defaults = await list_workspaces(session, user_id)
        default = next(row for row in defaults if row.is_default)
    return InvitationAccepted(membership=MemberRead(
        user_id=user_id, email=email, role=cast(Literal["owner", "member"], membership.role),
        membership_revision=membership.revision,
    ), default_workspace=default)


# --- Post-Port-B frozen share signatures (M0 stubs; W3-core implements) ---
# Migration chain: ... r15_highlight_rule_delivery (H) -> p14_workspace_shares (W3-core)
#   -> p14_translation_runtime (T3).


def granted_resource_ids(*, scope: WorkspaceContext, kind: ShareKind) -> Select[tuple[UUID]]:
    raise NotImplementedError("W3-core")


async def read_resource_grants(
    session: AsyncSession, *, scope: WorkspaceContext, kind: ShareKind,
    resource_ids: tuple[UUID, ...],
) -> tuple[GrantRef, ...]:
    raise NotImplementedError("W3-core")


async def active_grant_ids(
    session: AsyncSession, *, workspace_id: UUID, member_user_id: int, kind: ShareKind,
    resource_ids: tuple[UUID, ...],
) -> frozenset[UUID]:
    raise NotImplementedError("W3-core")


async def lock_resource_grants(
    session: AsyncSession, *, scope: WorkspaceContext, grants: tuple[GrantRef, ...],
) -> None:
    raise NotImplementedError("W3-core")


async def list_resource_shares(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int, *, resource_type: ShareKind,
    resource_id: UUID, after: int | None = None, limit: int = 100,
) -> ShareList:
    raise NotImplementedError("W3-core")


async def grant_share_in_uow(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int, resource_type: ShareKind,
    resource_id: UUID, member_user_id: int, payload: ShareUpsert, *, multi_workspace_enabled: bool,
) -> ShareRead:
    raise NotImplementedError("W3-core")


async def revoke_share_in_uow(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int, resource_type: ShareKind,
    resource_id: UUID, member_user_id: int, expected_revision: int | None,
) -> None:
    raise NotImplementedError("W3-core")


async def revoke_resource_shares_in_uow(
    session: AsyncSession, *, workspace_id: UUID, resource_type: ShareKind,
    resource_ids: tuple[UUID, ...],
) -> int:
    raise NotImplementedError("W3-core")
