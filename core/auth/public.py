"""Detached account identity, ordered lifecycle locks and provisioning for other modules.

This module keeps AuthSession persistence private while accepting only detached token and owner
identifiers from callers. Callers own the short session lifetime and must not retain it over I/O.
"""

from datetime import UTC, datetime
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, Request
from hmac import compare_digest

from core.auth.models import AuthSession, Owner
from core.auth.schemas import AccountRead, AccountSessionRef
from core.auth.service import hash_password
from core.workspaces.public import provision_default_workspace_in_uow, resolve_workspace_context


def normalize_account_email(value: str) -> str:
    """Normalize account/invitation identifiers without provider-specific alias rewriting.

    Reject empty, oversized or whitespace-containing identifiers and malformed single-@
    addresses. This is syntax normalization only, never mailbox ownership verification.
    """
    normalized = value.strip().lower()
    if len(normalized) > 320 or normalized.count("@") != 1 or any(char.isspace() for char in normalized):
        raise ValueError("Invalid email identifier")
    local, domain = normalized.split("@")
    if not local or not domain or len(local) > 64:
        raise ValueError("Invalid email identifier")
    return normalized


async def provision_bootstrap_account_in_uow(session: AsyncSession, password_hash: str) -> None:
    """Flush account 1, its private workspace and owner membership in one caller-owned unit.

    Setup must supply the hash of its validated real password. This helper issues no session,
    commits nothing, and lets the caller roll back the entire unit on competing setup. Flush
    the account first, workspace/membership second, and owned-default pointer last.
    """
    if not password_hash:
        raise ValueError("A real password hash is required")
    owner = Owner(id=1, password_hash=password_hash, account_state="active")
    session.add(owner)
    await session.flush()
    owner.default_workspace_id = await provision_default_workspace_in_uow(session, owner.id)
    await session.flush()


async def get_active_account(
    session: AsyncSession, account_id: int, *, multi_workspace_enabled: bool = False,
) -> AccountRead | None:
    """Return a complete active identity or fail closed on disabled/incomplete accounts.

    Account 1 retains legacy login. Every other account requires the explicit rollout flag,
    normalized email, a password hash, and its own default workspace with owner membership.
    This query grants no workspace resource or operator authority, and does not commit.
    """
    if account_id != 1 and not multi_workspace_enabled:
        return None
    owner = await session.scalar(
        select(Owner).where(Owner.id == account_id).execution_options(populate_existing=True)
    )
    if owner is None or owner.account_state != "active" or not owner.password_hash or owner.default_workspace_id is None:
        return None
    if owner.id != 1 and owner.email is None:
        return None
    if owner.email is not None:
        try:
            if normalize_account_email(owner.email) != owner.email:
                return None
        except ValueError:
            return None
    if (owner.email_verified_at is None) != (owner.email_verification_source is None):
        return None
    if owner.email_verification_source not in {None, "google_oidc"}:
        return None
    if owner.email_verified_at is not None and owner.email is None:
        return None
    workspace = await resolve_workspace_context(session, owner.id, owner.default_workspace_id)
    if workspace is None or workspace.role != "owner" or workspace.user_id != owner.id or workspace.workspace_id != owner.default_workspace_id:
        return None
    return AccountRead(
        id=owner.id, email=owner.email, default_workspace_id=owner.default_workspace_id,
        email_verified_at=owner.email_verified_at, email_verification_source=owner.email_verification_source,
    )


async def revalidate_owner_session(
    session: AsyncSession,
    token_hash: str,
    owner_id: int,
) -> bool:
    """Return whether the exact bootstrap session and its account remain valid.

    The caller supplies only its authenticated session's stored token digest and owner ID. This
    query grants no authority by itself, performs no write, and returns false for revoked,
    expired, disabled, incomplete or nonbootstrap identities even when the rollout flag is on.
    Legacy worker/tool/Chat callers must not gain workspace-owner or instance authority.
    """
    if owner_id != 1:
        return False
    return await revalidate_account_session(session, token_hash, owner_id)


async def revalidate_account_session(
    session: AsyncSession, token_hash: str, account_id: int, *, multi_workspace_enabled: bool = False,
) -> bool:
    """Revalidate a detached account token, expiry, rollout gate and complete active identity.

    New scoped callers opt in explicitly; this does not grant legacy domain/operator access.
    Use a fresh short transaction for each later admission/publication check, never over I/O.
    """
    current = await session.scalar(
        select(AuthSession.token_hash).where(
            AuthSession.token_hash == token_hash,
            AuthSession.owner_id == account_id,
            AuthSession.expires_at > datetime.now(UTC),
        )
    )
    return current is not None and await get_active_account(
        session, account_id, multi_workspace_enabled=multi_workspace_enabled,
    ) is not None


async def get_demo_owner_id(session: AsyncSession) -> int:
    """Return the configured singleton owner ID, failing when setup has not created it."""
    owner_id = await session.scalar(select(Owner.id).where(Owner.id == 1))
    if owner_id is None:
        raise RuntimeError("Demo seeding requires the configured owner account")
    return owner_id


__all__ = [
    "account_emails", "account_id_for_email", "lock_account_lifecycle", "provision_invited_account_in_uow",
    "get_active_account", "get_demo_owner_id", "normalize_account_email",
    "provision_bootstrap_account_in_uow", "revalidate_account_session", "revalidate_owner_session",
    "authenticated_session_ref", "lock_account_admission",
]


def authenticated_session_ref(request: Request) -> AccountSessionRef:
    """Detach the exact actor/session locator established by require_account on this request.

    Auth alone reads its ORM session. This carries no bearer and proves no later validity;
    W4 passes it to lock_access_fence(auth_sessions=(ref,)) before workspace/resource locks
    for every protected publication. Missing/mismatched authentication returns 401.
    """
    current = getattr(request.state, "auth_session", None)
    account = getattr(request.state, "account", None)
    if not isinstance(current, AuthSession) or not isinstance(account, AccountRead) or current.owner_id != account.id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return AccountSessionRef(account_id=account.id, token_hash=current.token_hash)


async def lock_account_admission(
    session: AsyncSession, account_ids: tuple[int, ...], *, actor_user_id: int,
    multi_workspace_enabled: bool = False, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> dict[int, AccountRead | None]:
    """Lock a complete runtime account set, then exact sessions, and return detached identities.

    Call before workspace/membership/resource locks in a fresh short transaction. Discover
    every ID first; at most actor plus workspace owner are admitted here. Accounts lock in
    ascending ID using FOR SHARE, sessions in digest order using FOR SHARE, conflicting with
    account-state updates and logout deletes. No KEY SHARE, commit, network I/O or browser
    session fabrication for workers. Caller releases/rolls back the transaction before I/O.

    Invalid actor, expired/revoked/wrong-account session or disabled actor returns 401.
    Other absent/inactive accounts return None for workspace-owned invisible-404 handling.
    Supplied session locators must belong to the discovered set and include the actor's
    exact session; omitted locators provide account admission only, never logout proof.
    The actual configured gate must be supplied explicitly and defaults to false.
    """
    ids = tuple(sorted(set(account_ids)))
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("Explicit boolean multi-workspace gate is required")
    if (type(actor_user_id) is not int or actor_user_id <= 0
            or not ids or len(ids) > 2 or actor_user_id not in ids
            or any(type(account_id) is not int or account_id <= 0 for account_id in ids)):
        raise ValueError("Discover actor and workspace owner before account admission")
    if (len(auth_sessions) > 2 or any(
        not isinstance(ref, AccountSessionRef) or ref.account_id not in ids for ref in auth_sessions
    ) or (auth_sessions and not any(ref.account_id == actor_user_id for ref in auth_sessions))):
        raise ValueError("Exact actor session must belong to the initial account set")
    await session.execute(
        select(Owner).where(Owner.id.in_(ids)).order_by(Owner.id).with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    for ref in sorted(auth_sessions, key=lambda item: item.token_hash):
        current = await session.scalar(
            select(AuthSession).where(AuthSession.token_hash == ref.token_hash)
            .with_for_update(read=True).execution_options(populate_existing=True)
        )
        if current is None or current.owner_id != ref.account_id or current.expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Authentication required")
    accounts = {account_id: await get_active_account(
        session, account_id, multi_workspace_enabled=multi_workspace_enabled,
    ) for account_id in ids}
    if accounts[actor_user_id] is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return accounts


async def account_id_for_email(session: AsyncSession, email: str) -> int | None:
    """Resolve one normalized identifier without locks or credential/account-state disclosure."""
    return await session.scalar(select(Owner.id).where(Owner.email == normalize_account_email(email)))


async def account_emails(session: AsyncSession, account_ids: Iterable[int]) -> dict[int, str | None]:
    """Return detached emails for already owner-authorized membership management projections."""
    rows = await session.execute(select(Owner.id, Owner.email).where(Owner.id.in_(tuple(account_ids))))
    return {row.id: row.email for row in rows}


async def lock_account_lifecycle(
    session: AsyncSession, account_ids: Iterable[int], *, actor_user_id: int | None = None,
    token_hash: str | None = None, csrf_hash: str | None = None, multi_workspace_enabled: bool = False,
) -> AccountRead | None:
    """Lock existing accounts ascending, then actor session, before any workspace lifecycle lock.

    IDs must first be discovered without locks; callers restart rather than add an earlier lock
    later. Recheck actor active/default/rollout state and the exact live session/CSRF hash after
    locking. Nonactor disabled rows may be locked for removal; no authority follows from them.
    Commits nothing; 401/403 abort the caller's transaction without identifying another email.
    """
    ids = sorted(set(account_ids))
    if actor_user_id is not None and actor_user_id not in ids:
        raise ValueError("Actor must be included in the initial account lock set")
    await session.execute(
        select(Owner).where(Owner.id.in_(ids)).order_by(Owner.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor_user_id is None:
        return None
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
        .execution_options(populate_existing=True)
    )
    if auth_session is None or auth_session.owner_id != actor_user_id or auth_session.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Authentication required")
    if csrf_hash is not None and not compare_digest(auth_session.csrf_hash, csrf_hash):
        raise HTTPException(status_code=403, detail="CSRF token is invalid", headers={"X-CSRF-Error": "invalid"})
    actor = await get_active_account(session, actor_user_id, multi_workspace_enabled=multi_workspace_enabled)
    if actor is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return actor


async def provision_invited_account_in_uow(
    session: AsyncSession, email: str, password: str, *, google_subject: str | None = None,
    google_issuer: str | None = None,
) -> AccountRead:
    """Stage a real-password invited identity/default in the caller's uncommitted transaction.

    Workspace owner must validate/consume the bound invitation in this same unit before commit.
    Unique-email or issuer/subject races propagate IntegrityError so the caller rolls back every
    row and requires winning-account authentication. A copied token never verifies a mailbox;
    optional Google subject/issuer must originate from a consumed verified enrollment proof.
    """
    from core.auth.models import GoogleIdentity

    if not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12 to 128 characters")
    if (google_subject is None) != (google_issuer is None):
        raise ValueError("Incomplete Google identity")
    normalized = normalize_account_email(email)
    owner = Owner(email=normalized, password_hash=hash_password(password), account_state="active")
    if google_subject is not None:
        from core.auth.google import GOOGLE_ISSUER

        if google_issuer != GOOGLE_ISSUER or not google_subject or len(google_subject) > 255:
            raise ValueError("Invalid verified Google identity")
        owner.email_verified_at = datetime.now(UTC)
        owner.email_verification_source = "google_oidc"
    session.add(owner)
    await session.flush()
    owner.default_workspace_id = await provision_default_workspace_in_uow(session, owner.id)
    if google_subject is not None:
        session.add(GoogleIdentity(
            owner_id=owner.id, issuer=google_issuer, subject=google_subject, email=normalized,
        ))
    await session.flush()
    return AccountRead(
        id=owner.id, email=owner.email, default_workspace_id=owner.default_workspace_id,
        email_verified_at=owner.email_verified_at, email_verification_source=owner.email_verification_source,
    )
