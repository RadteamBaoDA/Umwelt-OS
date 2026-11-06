"""GitHub webhook detail/digest retention, deleted-source grant revoke and credential delete for the connector worker."""

import asyncio
import copy
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.ids import uuid7
from core.realtime import commit_with_replay
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provisioning
from modules.connectors._worker_base import (
    _DENIED,
    _admit_lineage,
    _admit_source_job,
    _page,
    _read_cursor,
    _write_cursor,
)
from modules.connectors.credentials import N8nCredentials
from modules.connectors.models import (
    ConnectorManagedCredential,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)

logger = logging.getLogger("modules.connectors.worker")  # same logger object as the facade


async def expire_github_webhook_details(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 50,
) -> int:
    """Scrub expired payload-derived metadata while retaining replay digests and delivery IDs.

    Do not scrub a delivery with unfinished binding fanout. The indexed retention deadline and
    batch cap bound cleanup; digest_count and the unique delivery namespace remain unchanged.
    """
    from sqlalchemy import or_

    from modules.connectors.models import GithubWebhookDelivery, GithubWebhookOutbox

    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(GithubWebhookDelivery)
                    .outerjoin(
                        GithubWebhookOutbox,
                        GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id,
                    )
                    .where(
                        GithubWebhookDelivery.detail_expires_at <= now,
                        GithubWebhookDelivery.details_scrubbed_at.is_(None),
                        or_(
                            GithubWebhookOutbox.id.is_(None),
                            GithubWebhookOutbox.state == "complete",
                        ),
                    )
                    .order_by(GithubWebhookDelivery.detail_expires_at, GithubWebhookDelivery.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
                )
            ).all()
        )
        for delivery in rows:
            delivery.event = "expired"
            delivery.action = None
            delivery.app_id = None
            delivery.installation_id = None
            delivery.repository_id = None
            delivery.targets = []
            delivery.details_scrubbed_at = now
        if rows:
            await session.commit()
        else:
            await session.rollback()
        return len(rows)


# Replay window after detail scrubbing during which a digest still rejects a re-sent delivery ID.
# Past it, a replay is harmless: deliveries only enqueue refresh/reconcile hints that are
# revalidated against the GitHub API, and HMAC verification still blocks forgery.
DIGEST_RETENTION = timedelta(days=30)


async def reclaim_github_webhook_digests(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    """Delete fully fanned-out, long-scrubbed digests and give their capacity back.

    Invariant: ``GithubWebhookCapacity.digest_count`` equals the number of delivery rows. The
    receiver mutates both under the capacity row lock, so this takes the same lock first and
    deletes and decrements in one transaction. Only deliveries whose details were scrubbed more
    than ``DIGEST_RETENTION`` ago and whose outbox is absent or complete are removed (outbox rows
    cascade). The batch cap bounds lock time; leftovers are reclaimed on the next worker pass.
    """
    from sqlalchemy import or_

    async with factory() as session:
        capacity = await session.scalar(
            select(GithubWebhookCapacity).where(GithubWebhookCapacity.id == 1).with_for_update()
        )
        if capacity is None:
            await session.rollback()
            return 0
        rows = list(
            (
                await session.scalars(
                    select(GithubWebhookDelivery)
                    .outerjoin(
                        GithubWebhookOutbox,
                        GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id,
                    )
                    .where(
                        GithubWebhookDelivery.details_scrubbed_at.is_not(None),
                        GithubWebhookDelivery.details_scrubbed_at <= now - DIGEST_RETENTION,
                        or_(
                            GithubWebhookOutbox.id.is_(None),
                            GithubWebhookOutbox.state == "complete",
                        ),
                    )
                    .order_by(GithubWebhookDelivery.details_scrubbed_at, GithubWebhookDelivery.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
                )
            ).all()
        )
        for delivery in rows:
            await session.delete(delivery)
        capacity.digest_count = max(0, capacity.digest_count - len(rows))
        await session.commit()
        return len(rows)


_DELETED_SOURCE_REVOKE = "provider_revoke_pending_source_deleted"
_COORDINATOR_STALE_AFTER = timedelta(minutes=5)


@dataclass
class _DeletedGrantRevoke:
    """State carried between the three phases of one deleted-Source grant revoke.

    Captures the admitted worker subject and the exact grant operation/token revision seen in
    phase 1 so phase 3 can re-lock under the ORIGINAL access fence and write only for that
    operation. ``claim`` is set only when this pass took the actor-wide coordinator; ``token`` is
    the decrypted access token and is never logged.
    """

    scope: InternalJobScope
    fence: AccessFence
    actor: int
    grant_operation: UUID
    token_revision: int
    token: str | None = None
    claim: UUID | None = None
    outcome: str = "provider_revoke_failed_source_deleted"


def _idle_coordinator(coordinator: Any) -> None:
    """Return an OAuth coordinator row to ``idle`` and clear its operation and error code."""
    coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None


def _open_grant_access_token(key: str, grant: Any) -> str | None:
    """Decrypt a grant's access token; any cipher failure yields ``None`` (revoke is then skipped).

    The token is returned to the caller only and is never logged.
    """
    from modules.connectors.github import oauth

    try:
        opened = oauth._open_token_cipher(
            key,
            grant.encrypted_tokens,
            grant.source_id,
            grant.operation_id,
            grant.source_generation,
            grant.configuration_revision,
        )
        candidate = opened.get("access_token")
        return candidate if isinstance(candidate, str) else None
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return None


async def _claim_deleted_grant_revoke(
    session: AsyncSession,
    source_id: UUID,
    *,
    flag: bool,
    key: str,
) -> _DeletedGrantRevoke | None:
    """Phase 1: admit the archived Source, lock it, claim the coordinator and open the token.

    Lock order: access fence, Source/provisioning, grant, then the actor-wide OAuth coordinator.
    Returns ``None`` (after rollback or releasing a stale claim of this module) when the Source
    is not admissible, the grant no longer needs revoking, or the coordinator is busy. Otherwise
    commits (with replay) and returns the state for phases 2 and 3; a live peer Source on the
    same grant turns the revoke into a recorded skip. Denied-admission errors propagate to the
    caller, which rolls back.
    """
    from modules.connectors.models import GithubOAuthCoordinator, GithubOAuthGrant

    admitted = await _admit_source_job(session, source_id, multi_workspace_enabled=flag)
    if admitted is None:
        return None
    scope, fence = admitted
    actor = scope.actor_user_id
    anchor, _row, _slots = await provisioning.lock_connector(
        session,
        source_id,
        (),
        scope=scope,
        multi_workspace_enabled=flag,
        expected_access_fence=fence,
    )
    grant = await session.scalar(
        select(GithubOAuthGrant)
        .where(GithubOAuthGrant.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        anchor is None
        or grant is None
        or grant.encrypted_tokens is None
        or grant.error_code != _DELETED_SOURCE_REVOKE
    ):
        await session.rollback()
        return None
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    if coordinator is None:
        coordinator = GithubOAuthCoordinator(owner_id=actor, state="idle")
        session.add(coordinator)
        await session.flush()
    if coordinator.state != "idle":
        if (
            coordinator.state == "revoking"
            and coordinator.error_code == _DELETED_SOURCE_REVOKE
            and coordinator.updated_at <= datetime.now(UTC) - _COORDINATOR_STALE_AFTER
        ):
            # Our own claim from a crashed pass: release it, retry on the next pass.
            _idle_coordinator(coordinator)
            await commit_with_replay(
                session,
                scope=scope,
                multi_workspace_enabled=flag,
                access_fence=fence,
            )
        else:
            await session.rollback()
        return None
    state = _DeletedGrantRevoke(
        scope=scope,
        fence=fence,
        actor=actor,
        grant_operation=grant.operation_id,
        token_revision=grant.token_revision,
    )
    if await provisioning.github_grant_has_active_peer(
        session,
        grant,
        scope=scope,
        multi_workspace_enabled=flag,
    ):
        state.outcome = "provider_revoke_skipped_source_deleted"
    else:
        state.token = _open_grant_access_token(key, grant)
        if state.token is not None:
            state.claim = uuid7()
            coordinator.state, coordinator.operation_id = "revoking", state.claim
            coordinator.error_code = _DELETED_SOURCE_REVOKE
    await commit_with_replay(session, scope=scope, multi_workspace_enabled=flag, access_fence=fence)
    return state


async def _record_deleted_grant_revoke(
    factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
    state: _DeletedGrantRevoke,
    *,
    flag: bool,
) -> bool:
    """Phase 3: re-lock under the original fence, record the outcome and release our claim.

    Returns True when the outcome was committed. On a denied admission (401/403/404/409) nothing
    is recorded; if this pass holds the coordinator claim it is undone in its own transaction so
    the actor's OAuth routes in other workspaces are not wedged, and False is returned. Other
    HTTP-shaped errors propagate.
    """
    from modules.connectors.models import GithubOAuthCoordinator

    async with factory() as session:
        try:
            # Lock order matches the routes: access, Source, provisioning, grant, coordinator last.
            await provisioning.lock_connector(
                session,
                source_id,
                (),
                scope=state.scope,
                multi_workspace_enabled=flag,
                expected_access_fence=state.fence,
            )
            await provisioning.finish_deleted_source_grant_revoke(
                session,
                source_id,
                state.outcome,
                scope=state.scope,
                multi_workspace_enabled=flag,
                access_fence=state.fence,
                grant_operation_id=state.grant_operation,
                token_revision=state.token_revision,
            )
            if state.claim is not None:
                coordinator = await session.scalar(
                    select(GithubOAuthCoordinator)
                    .where(GithubOAuthCoordinator.owner_id == state.actor)
                    .with_for_update()
                )
                if coordinator is not None and coordinator.operation_id == state.claim:
                    _idle_coordinator(coordinator)
            await commit_with_replay(
                session,
                scope=state.scope,
                multi_workspace_enabled=flag,
                access_fence=state.fence,
            )
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            if exc.status_code not in _DENIED:
                raise
            if state.claim is not None:
                # Account-scoped row, no workspace data: undo only this job's own claim so the
                # actor's OAuth routes in other workspaces are not wedged by lost access here.
                coordinator = await session.scalar(
                    select(GithubOAuthCoordinator)
                    .where(GithubOAuthCoordinator.owner_id == state.actor)
                    .with_for_update()
                )
                if coordinator is not None and coordinator.operation_id == state.claim:
                    _idle_coordinator(coordinator)
                    await session.commit()
                else:
                    await session.rollback()
            logger.warning(
                "deleted-source grant revoke result not recorded: HTTP %s", exc.status_code
            )
            return False
    return True


async def revoke_deleted_source_github_grants(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    limit: int = 10,
    cursor_ctx: dict[str, object] | None = None,
) -> int:
    """Best-effort remote revoke for grants whose last GitHub source was archived or purged.

    GitHub's grant revoke is app/user-wide, so it is serialized with OAuth authorize, refresh and
    disconnect through the actor-wide ``GithubOAuthCoordinator`` (keyed by the Source owner's real
    actor id, never a constant):
    phase 1 admits the archived Source anchor (Recipe S) and locks access fence, Source,
    provisioning, grant, then the coordinator; it claims ``revoking`` (skipping this grant for the
    pass if the coordinator is busy), rechecks live peers and decrypts. Phase 2 calls GitHub with
    no session or lock held. Phase 3 re-locks in the same order under the ORIGINAL access fence,
    clears the ciphertext only for the exact captured grant operation and token revision, records
    an opaque outcome code and releases the coordinator only if it still holds this operation.
    A crash after the claim leaves ``revoking`` with this module's error code; any later pass
    releases such a claim once it is older than five minutes, so the coordinator cannot stay
    wedged. The token is never logged. A Source whose owner lost access is skipped, not rebased;
    if access is lost between phases the remote revoke has still happened and the ciphertext stays
    until a later pass is admitted. Discovery pages by Source id with a shared fairness cursor so
    denied grants cannot starve later ones.
    """
    from modules.connectors.github import oauth
    from modules.connectors.models import GithubOAuthGrant

    flag = settings.multi_workspace_enabled
    key = settings.connector_credential_encryption_key.get_secret_value()
    after = await _read_cursor(cursor_ctx, "connectors_deleted_revoke")
    async with factory() as session:
        pending: list[UUID] = list(
            (
                await session.execute(
                    _page(
                        select(GithubOAuthGrant.source_id),
                        GithubOAuthGrant.source_id,
                        after,
                        GithubOAuthGrant.error_code == _DELETED_SOURCE_REVOKE,
                        GithubOAuthGrant.encrypted_tokens.is_not(None),
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()
    await _write_cursor(
        cursor_ctx, "connectors_deleted_revoke", pending[-1] if len(pending) >= limit else None
    )
    handled = 0
    for source_id in pending:
        async with factory() as session:
            try:
                state = await _claim_deleted_grant_revoke(session, source_id, flag=flag, key=key)
            except (DomainError, HTTPException) as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
                continue
        if state is None:
            continue
        if state.token is not None:
            try:
                await oauth.revoke_github_grant(settings, state.token)
                state.outcome = "provider_revoked_source_deleted"
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                state.outcome = "provider_revoke_failed_source_deleted"
        if await _record_deleted_grant_revoke(factory, source_id, state, flag=flag):
            handled += 1
    return handled


async def _settle_credential_delete(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    *,
    original: dict[str, object],
    scope: InternalJobScope,
    fence: AccessFence,
    multi_workspace_enabled: bool,
    outcome: str,
    error_code: str | None,
) -> bool:
    """Settle one dispatched credential delete under the ORIGINAL scope and access fence.

    The claim already committed the dispatch barrier, so this is a fresh transaction after the
    provider call: admission, retained Source anchor, provisioning and every slot are locked via
    ``lock_retained_connector_effect``, the exact captured envelope is compared, and publication
    goes through ``commit_retained_connector_effect``. If the original access is definitively
    gone, the transport result is journaled in its own fresh transaction (no binding, no secret)
    and nothing else changes. Returns True only for a confirmed delete.
    """
    operation_id = UUID(str(original["id"]))
    try:
        before = await provisioning.lock_retained_connector_effect(
            session,
            source_id,
            original_operation=original,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
    except provisioning.RetainedEffectAdmissionDenied:
        await session.rollback()
        disposition = await provisioning.record_retained_credential_result_in_uow(
            session,
            source_id,
            slot,
            original_operation=original,
            scope=scope,
            access_fence=fence,
            outcome=outcome,
            error_code=error_code,
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        return False
    if outcome == "known_success":
        changed = await provisioning.acknowledge_credential_delete(
            session,
            source_id,
            slot,
            operation_id,
            str(original["target_id"]),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            original_operation=original,
            access_fence=fence,
        )
    else:
        changed = await provisioning.fail_credential_operation(
            session,
            source_id,
            slot,
            operation_id,
            error_code or "credential_delete_outcome_unknown",
            unknown=outcome == "unknown",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            original_operation=original,
            access_fence=fence,
        )
    if not changed:
        await session.rollback()
        return False
    await provisioning.commit_retained_connector_effect(
        session,
        before,
        source_id=source_id,
        original_operation=original,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return outcome == "known_success"


async def _delete_credential(
    session: AsyncSession,
    settings: Settings,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> bool:
    """Claim and delete a prepared credential operation, preserving unknown outcomes for reconciliation.

    The prepared envelope's recorded principal and access fence are admitted first (a changed
    or revoked epoch skips the delete; a legacy envelope is quarantined). The claim commits the
    dispatch barrier before any network call and holds no lock afterwards. Every settlement then
    reuses that ORIGINAL scope and fence and the claimed envelope as the immutable operation
    (see ``_settle_credential_delete``), so later Source or configuration changes only permit this
    exact cleanup result and never a new send. Resolve URL/key only from that admitted
    workspace; an instance-wide client must never receive another workspace's credentials.
    """
    from modules.settings.public import resolve_n8n

    envelope = await session.scalar(
        select(ConnectorManagedCredential.operation_envelope)
        .where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.slot == slot,
            ConnectorManagedCredential.operation_id == operation_id,
        )
        .execution_options(populate_existing=True)
    )
    admitted = await _admit_lineage(
        session,
        source_id,
        copy.deepcopy(envelope),
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if admitted is None:
        return False
    scope, fence = admitted
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    await session.rollback()
    if not n8n.api_key:
        return False
    client = N8nCredentials(n8n.service_url, n8n.api_key)
    claimed = await provisioning.claim_credential_operation(
        session,
        source_id,
        slot,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        service_url=client.service_url,
    )
    if claimed is None:
        await session.rollback()
        return False
    settle = partial(
        _settle_credential_delete,
        session,
        source_id,
        slot,
        original=claimed,
        scope=scope,
        fence=fence,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    target = claimed.get("target_id")
    if not isinstance(target, str):
        return await settle(
            outcome="known_rejection", error_code="credential_delete_target_missing"
        )
    try:
        await client.delete(target)
    except asyncio.CancelledError:
        await settle(outcome="unknown", error_code="credential_delete_outcome_unknown")
        raise
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        rejected = (
            isinstance(exc, httpx.HTTPStatusError)
            and 400 <= exc.response.status_code < 500
            and exc.response.status_code != 408
        )
        return await settle(
            outcome="known_rejection" if rejected else "unknown",
            error_code="credential_delete_rejected"
            if rejected
            else "credential_delete_outcome_unknown",
        )
    return await settle(outcome="known_success", error_code=None)
