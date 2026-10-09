import asyncio
import copy
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from core.config import Settings
from core.realtime import commit_with_replay
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provisioning
from modules.connectors.credentials import N8nCredentials
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.connectors.n8n import N8nApi, workflow_matches
from modules.sources import public as sources

logger = logging.getLogger(__name__)

# Admission outcomes that mean "this subject may not act now": skip it, never rebase onto
# another actor or epoch. Anything else (database, programming, 5xx) propagates.
_DENIED = frozenset({401, 403, 404, 409})
_PASS_LIMIT = 40


_CURSOR_KEYS = frozenset({
    "connectors_deleted_revoke", "connectors_workflows", "connectors_unknown_create", "connectors_activation",
})


def _cursor_ctx(ctx: dict[str, object]) -> dict[str, object] | None:
    """Return ``ctx`` when the worker-wide fairness dict (``core.worker_cursors.STATE_KEY``) is installed.

    ARQ hands each job a shallow copy of ``ctx``, so only that shared ``dict[str, str]`` survives
    between passes. Without it every pass starts at the beginning, which is correct but not fair.
    """
    return ctx if isinstance(ctx.get(STATE_KEY), dict) else None


async def _read_cursor(ctx: dict[str, object] | None, key: str) -> UUID | None:
    """Read one Source-id fairness cursor (Redis first); missing or corrupt restarts the sweep."""
    return await read_cursor(ctx, key, _CURSOR_KEYS) if ctx is not None else None


async def _write_cursor(ctx: dict[str, object] | None, key: str, last: UUID | None) -> None:
    """Remember the last discovered Source id of a full page, or clear the key to wrap around."""
    if ctx is not None:
        await write_cursor(ctx, key, last, _CURSOR_KEYS)


def _slot_state(ctx: dict[str, object] | None) -> dict[str, str] | None:
    return cast(dict[str, str], ctx[STATE_KEY]) if ctx is not None else None


def _read_slot_cursor(ctx: dict[str, object] | None, key: str) -> tuple[UUID, str] | None:
    """Parse a composite ``source_id|slot`` cursor (encoded string, not a core UUID cursor)."""
    state = _slot_state(ctx)
    try:
        source_id, slot = (state[key] if state is not None else "").split("|", 1)
        return UUID(source_id), slot
    except (KeyError, ValueError):
        return None


def _write_slot_cursor(ctx: dict[str, object] | None, key: str, last: tuple[UUID, str] | None) -> None:
    """Remember the last (Source id, slot) of a full page, or clear the key to wrap around."""
    state = _slot_state(ctx)
    if state is None:
        return
    if last is None:
        state.pop(key, None)
    else:
        state[key] = f"{last[0]}|{last[1]}"


def _page(
    statement: Any, id_column: Any, after: UUID | None, *where: ColumnElement[bool], order: tuple[Any, ...] = (),
    slot_column: Any = None, after_slot: str | None = None,
) -> Any:
    """Order a Source-id discovery by id after the cursor; denied subjects cannot starve later ones.

    With ``slot_column`` and ``after_slot`` the keyset is the composite (id, slot), so a page that
    ended mid-Source resumes at that Source's next slot instead of skipping its remaining slots.
    """
    statement = statement.where(*where)
    if after is not None:
        if slot_column is not None and after_slot is not None:
            statement = statement.where(or_(id_column > after, and_(id_column == after, slot_column > after_slot)))
        else:
            statement = statement.where(id_column > after)
    return statement.order_by(id_column, *order).limit(_PASS_LIMIT)


def _lineage(source_id: UUID, envelope: object) -> tuple[InternalJobScope, AccessFence] | None:
    """Rebuild the original principal and access fence recorded in a durable operation JSON.

    Prepared credential/workflow/activation envelopes record the admitted workspace, actor,
    membership revision, workspace configuration revision and Source generation. Legacy rows
    without them are quarantined (None): a job is never rebased onto the current epoch.
    """
    if not isinstance(envelope, dict):
        return None
    try:
        scope = InternalJobScope(
            workspace_id=UUID(str(envelope["workspace_id"])), actor_user_id=envelope["actor_user_id"],
            membership_revision=envelope["membership_revision"], source_id=source_id,
            source_generation=envelope["source_generation"],
        )
        return scope, AccessFence(
            scope.workspace_id, scope.actor_user_id, scope.membership_revision,
            envelope["workspace_configuration_revision"],
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _admit_lineage(
    session: AsyncSession, source_id: UUID, envelope: object, *, multi_workspace_enabled: bool,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Admit a durable operation's original principal; None releases the transaction and skips it.

    Reads the access fence for the recorded lineage (non-locking) and requires it to equal the
    recorded fence exactly. Denial, a changed epoch or a legacy envelope never fall back to the
    current owner. Callers that continue into locks take them in access, Source, provisioning order.
    """
    lineage = _lineage(source_id, envelope)
    if lineage is None:
        await session.rollback()
        return None
    scope, original = lineage
    try:
        current = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
        return None
    if current != original:
        await session.rollback()
        return None
    return scope, original


async def _admit_source_job(
    session: AsyncSession, source_id: UUID, *, multi_workspace_enabled: bool,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Recipe S: resolve a bare Source's job scope, then read its fence; None means skip this Source.

    ``resolve_source_job_scope`` admits (locks) auth/workspace/membership before it rereads the
    Source row, so this is safe before any Source lock. Archived anchors resolve too, which is what
    deleted-source grant revocation needs. The caller continues into ``lock_*`` helpers passing the
    returned fence as the expected fence.
    """
    try:
        scope = await sources.resolve_source_job_scope(
            session, source_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None:
            await session.rollback()
            return None
        fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
        return None
    return scope, fence


async def expire_github_webhook_details(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 50,
) -> int:
    """Scrub expired payload-derived metadata while retaining replay digests and delivery IDs.

    Do not scrub a delivery with unfinished binding fanout. The indexed retention deadline and
    batch cap bound cleanup; digest_count and the unique delivery namespace remain unchanged.
    """
    from sqlalchemy import or_

    from modules.connectors.models import GithubWebhookDelivery, GithubWebhookOutbox

    async with factory() as session:
        rows = list((await session.scalars(
            select(GithubWebhookDelivery)
            .outerjoin(GithubWebhookOutbox, GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id)
            .where(
                GithubWebhookDelivery.detail_expires_at <= now,
                GithubWebhookDelivery.details_scrubbed_at.is_(None),
                or_(GithubWebhookOutbox.id.is_(None), GithubWebhookOutbox.state == "complete"),
            )
            .order_by(GithubWebhookDelivery.detail_expires_at, GithubWebhookDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
        )).all())
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
    factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 100,
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
        rows = list((await session.scalars(
            select(GithubWebhookDelivery)
            .outerjoin(GithubWebhookOutbox, GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id)
            .where(
                GithubWebhookDelivery.details_scrubbed_at.is_not(None),
                GithubWebhookDelivery.details_scrubbed_at <= now - DIGEST_RETENTION,
                or_(GithubWebhookOutbox.id.is_(None), GithubWebhookOutbox.state == "complete"),
            )
            .order_by(GithubWebhookDelivery.details_scrubbed_at, GithubWebhookDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
        )).all())
        for delivery in rows:
            await session.delete(delivery)
        capacity.digest_count = max(0, capacity.digest_count - len(rows))
        await session.commit()
        return len(rows)


_DELETED_SOURCE_REVOKE = "provider_revoke_pending_source_deleted"
_COORDINATOR_STALE_AFTER = timedelta(minutes=5)


async def revoke_deleted_source_github_grants(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, limit: int = 10,
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
    from modules.connectors.models import GithubOAuthCoordinator, GithubOAuthGrant

    flag = settings.multi_workspace_enabled
    key = settings.connector_credential_encryption_key.get_secret_value()
    after = await _read_cursor(cursor_ctx, "connectors_deleted_revoke")
    async with factory() as session:
        pending: list[UUID] = list((await session.execute(_page(
            select(GithubOAuthGrant.source_id), GithubOAuthGrant.source_id, after,
            GithubOAuthGrant.error_code == _DELETED_SOURCE_REVOKE,
            GithubOAuthGrant.encrypted_tokens.is_not(None),
        ).limit(limit))).scalars().all())
        await session.rollback()
    await _write_cursor(cursor_ctx, "connectors_deleted_revoke", pending[-1] if len(pending) >= limit else None)
    handled = 0
    for source_id in pending:
        token: str | None = None
        claim: UUID | None = None
        outcome = "provider_revoke_failed_source_deleted"
        async with factory() as session:
            try:
                admitted = await _admit_source_job(session, source_id, multi_workspace_enabled=flag)
                if admitted is None:
                    continue
                scope, fence = admitted
                actor = scope.actor_user_id
                anchor, _row, _slots = await provisioning.lock_connector(
                    session, source_id, (), scope=scope, multi_workspace_enabled=flag,
                    expected_access_fence=fence,
                )
                grant = await session.scalar(
                    select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
                    .with_for_update().execution_options(populate_existing=True)
                )
                if (
                    anchor is None or grant is None or grant.encrypted_tokens is None
                    or grant.error_code != _DELETED_SOURCE_REVOKE
                ):
                    await session.rollback()
                    continue
                coordinator = await session.scalar(
                    select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == actor).with_for_update()
                )
                if coordinator is None:
                    coordinator = GithubOAuthCoordinator(owner_id=actor, state="idle")
                    session.add(coordinator)
                    await session.flush()
                if coordinator.state != "idle":
                    if (
                        coordinator.state == "revoking" and coordinator.error_code == _DELETED_SOURCE_REVOKE
                        and coordinator.updated_at <= datetime.now(UTC) - _COORDINATOR_STALE_AFTER
                    ):
                        # Our own claim from a crashed pass: release it, retry on the next pass.
                        coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
                        await commit_with_replay(
                            session, scope=scope, multi_workspace_enabled=flag, access_fence=fence,
                        )
                    else:
                        await session.rollback()
                    continue
                grant_operation, token_revision = grant.operation_id, grant.token_revision
                if await provisioning.github_grant_has_active_peer(
                    session, grant, scope=scope, multi_workspace_enabled=flag,
                ):
                    outcome = "provider_revoke_skipped_source_deleted"
                else:
                    try:
                        opened = oauth._open_token_cipher(
                            key, grant.encrypted_tokens, grant.source_id, grant.operation_id,
                            grant.source_generation, grant.configuration_revision,
                        )
                        candidate = opened.get("access_token")
                        token = candidate if isinstance(candidate, str) else None
                    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                        token = None
                    if token is not None:
                        claim = uuid4()
                        coordinator.state, coordinator.operation_id = "revoking", claim
                        coordinator.error_code = _DELETED_SOURCE_REVOKE
                await commit_with_replay(session, scope=scope, multi_workspace_enabled=flag, access_fence=fence)
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
                continue
        if token is not None:
            try:
                await oauth.revoke_github_grant(settings, token)
                outcome = "provider_revoked_source_deleted"
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                outcome = "provider_revoke_failed_source_deleted"
        async with factory() as session:
            try:
                # Lock order matches the routes: access, Source, provisioning, grant, coordinator last.
                await provisioning.lock_connector(
                    session, source_id, (), scope=scope, multi_workspace_enabled=flag, expected_access_fence=fence,
                )
                await provisioning.finish_deleted_source_grant_revoke(
                    session, source_id, outcome, scope=scope, multi_workspace_enabled=flag,
                    access_fence=fence, grant_operation_id=grant_operation, token_revision=token_revision,
                )
                if claim is not None:
                    coordinator = await session.scalar(
                        select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == actor).with_for_update()
                    )
                    if coordinator is not None and coordinator.operation_id == claim:
                        coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
                await commit_with_replay(session, scope=scope, multi_workspace_enabled=flag, access_fence=fence)
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
                if claim is not None:
                    # Account-scoped row, no workspace data: undo only this job's own claim so the
                    # actor's OAuth routes in other workspaces are not wedged by lost access here.
                    coordinator = await session.scalar(
                        select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == actor).with_for_update()
                    )
                    if coordinator is not None and coordinator.operation_id == claim:
                        coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
                        await session.commit()
                    else:
                        await session.rollback()
                logger.warning("deleted-source grant revoke result not recorded: HTTP %s", exc.status_code)
                continue
        handled += 1
    return handled


async def _settle_credential_delete(
    session: AsyncSession, source_id: UUID, slot: str, *, original: dict[str, object],
    scope: InternalJobScope, fence: AccessFence, multi_workspace_enabled: bool,
    outcome: str, error_code: str | None,
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
            session, source_id, original_operation=original, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        )
    except provisioning.RetainedEffectAdmissionDenied:
        await session.rollback()
        disposition = await provisioning.record_retained_credential_result_in_uow(
            session, source_id, slot, original_operation=original, scope=scope, access_fence=fence,
            outcome=outcome, error_code=error_code,
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        return False
    if outcome == "known_success":
        changed = await provisioning.acknowledge_credential_delete(
            session, source_id, slot, operation_id, str(original["target_id"]),
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            original_operation=original, access_fence=fence,
        )
    else:
        changed = await provisioning.fail_credential_operation(
            session, source_id, slot, operation_id, error_code or "credential_delete_outcome_unknown",
            unknown=outcome == "unknown", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            original_operation=original, access_fence=fence,
        )
    if not changed:
        await session.rollback()
        return False
    await provisioning.commit_retained_connector_effect(
        session, before, source_id=source_id, original_operation=original, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return outcome == "known_success"


async def _delete_credential(
    session: AsyncSession,
    client: N8nCredentials,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *, multi_workspace_enabled: bool,
) -> bool:
    """Claim and delete a prepared credential operation, preserving unknown outcomes for reconciliation.

    The prepared envelope's recorded principal and access fence are admitted first (a changed
    or revoked epoch skips the delete; a legacy envelope is quarantined). The claim commits the
    dispatch barrier before any network call and holds no lock afterwards. Every settlement then
    reuses that ORIGINAL scope and fence and the claimed envelope as the immutable operation
    (see ``_settle_credential_delete``), so later Source or configuration changes only permit this
    exact cleanup result and never a new send.
    """
    envelope = await session.scalar(select(ConnectorManagedCredential.operation_envelope).where(
        ConnectorManagedCredential.source_id == source_id, ConnectorManagedCredential.slot == slot,
        ConnectorManagedCredential.operation_id == operation_id,
    ).execution_options(populate_existing=True))
    admitted = await _admit_lineage(
        session, source_id, copy.deepcopy(envelope), multi_workspace_enabled=multi_workspace_enabled,
    )
    if admitted is None:
        return False
    scope, fence = admitted
    claimed = await provisioning.claim_credential_operation(
        session, source_id, slot, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if claimed is None:
        await session.rollback()
        return False
    settle = partial(
        _settle_credential_delete, session, source_id, slot, original=claimed, scope=scope, fence=fence,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    target = claimed.get("target_id")
    if not isinstance(target, str):
        return await settle(outcome="known_rejection", error_code="credential_delete_target_missing")
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
            error_code="credential_delete_rejected" if rejected else "credential_delete_outcome_unknown",
        )
    return await settle(outcome="known_success", error_code=None)


async def _resume_activation(
    session: AsyncSession,
    source_id: UUID,
    api: N8nApi,
    credentials: N8nCredentials,
    settings: Settings,
) -> bool:
    """Resume a persisted activation under the principal and fence recorded in its intent.

    The intent's workspace, actor, membership revision, configuration revision and Source
    generation are admitted exactly (no current-owner fallback), the connectors module must be
    enabled for that workspace (otherwise the durable work is left untouched), and SQL is released
    before ``drive_activation`` reacquires its own ordered locks and does all provider I/O.
    """
    from modules.connectors.activation import drive_activation
    from modules.settings.public import module_is_enabled

    flag = settings.multi_workspace_enabled
    intent = await session.scalar(select(ConnectorProvisioning.activation_intent).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    admitted = await _admit_lineage(session, source_id, copy.deepcopy(intent), multi_workspace_enabled=flag)
    if admitted is None:
        return False
    scope, fence = admitted
    if not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=flag):
        await session.rollback()
        return False
    await session.rollback()
    return await drive_activation(
        session, source_id, api, credentials,
        settings.connector_credential_encryption_key.get_secret_value(),
        scope=scope, multi_workspace_enabled=flag, access_fence=fence,
    )


async def _drive_workflow(session: AsyncSession, api: N8nApi, source_id: UUID, *, flag: bool) -> int:
    """Drive one prepared workflow step under the envelope's recorded principal and fence.

    Non-delete steps additionally require the connectors module to be enabled for that
    workspace. The immutable envelope copy is the operation identity for every claim/send/result
    inside ``drive_workflow_operation``, which releases SQL before network calls.
    """
    from modules.settings.public import module_is_enabled

    envelope = await session.scalar(select(ConnectorProvisioning.workflow_operation).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    original = copy.deepcopy(envelope) if isinstance(envelope, dict) else None
    step = original.get("step") if original is not None else None
    if not isinstance(step, dict) or step.get("state") != "prepared":
        await session.rollback()
        return 0
    admitted = await _admit_lineage(session, source_id, original, multi_workspace_enabled=flag)
    if admitted is None:
        return 0
    scope, fence = admitted
    if step.get("kind") != "delete" and not await module_is_enabled(
        session, "connectors", scope=scope, multi_workspace_enabled=flag,
    ):
        await session.rollback()
        return 0
    await session.rollback()
    return int(await provisioning.drive_workflow_operation(
        session, source_id, api, scope=scope, multi_workspace_enabled=flag,
        original_operation=cast(dict[str, object], original), access_fence=fence,
    ))


async def _recover_unknown_create(
    factory: async_sessionmaker[AsyncSession], api: N8nApi, source_id: UUID, *, flag: bool,
) -> int:
    """Look up an uncertain workflow create and attach its single exact match, or defer it later.

    The unknown-create envelope is captured once under the recorded principal and fence and kept
    unchanged through the n8n lookup (which holds no session). Resolution then locks the original
    admission, retained Source anchor and provisioning rows and commits through
    ``commit_retained_connector_effect``. If the original access is gone, the discovered remote
    workflow ID is journaled (never silently dropped). The create is never repeated.
    """
    async with factory() as session:
        stored = await session.scalar(select(ConnectorProvisioning.workflow_operation).where(
            ConnectorProvisioning.source_id == source_id,
        ).execution_options(populate_existing=True))
        original = copy.deepcopy(stored) if isinstance(stored, dict) else None
        step = original.get("step") if original is not None else None
        if (
            original is None or not isinstance(step, dict)
            or step.get("state") != "unknown" or step.get("kind") != "create"
        ):
            await session.rollback()
            return 0
        admitted = await _admit_lineage(session, source_id, original, multi_workspace_enabled=flag)
        if admitted is None:
            return 0
        await session.rollback()
    scope, fence = admitted
    operation_value, step_value, name, body = original.get("id"), step.get("id"), original.get("workflow_name"), step.get("request")
    created_operation_id: UUID | None = None
    workflow_id: str | None = None
    try:
        created_operation_id = UUID(str(operation_value))
        matches = await api.find_workflows(str(name))
        if (
            len(matches) == 1
            and isinstance(matches[0].get("id"), str)
            and isinstance(body, dict)
        ):
            candidate_id = str(matches[0]["id"])
            actual = await api.get_workflow(candidate_id)
            if workflow_matches(body, actual):
                workflow_id = candidate_id
    except Exception:  # noqa: BLE001, S110  # best-effort cleanup/optional step; failure intentionally ignored
        pass
    if workflow_id is not None and created_operation_id is not None:
        async with factory() as session:
            try:
                before = await provisioning.lock_retained_connector_effect(
                    session, source_id, original_operation=original, scope=scope,
                    multi_workspace_enabled=flag, access_fence=fence,
                )
            except provisioning.RetainedEffectAdmissionDenied:
                await session.rollback()
                disposition = await provisioning.record_retained_workflow_result_in_uow(
                    session, source_id, original_operation=original, scope=scope, access_fence=fence,
                    outcome="known_success", remote_id=workflow_id,
                )
                if disposition in {"stored", "duplicate"}:
                    await session.commit()
                else:
                    await session.rollback()
                return 0
            resolved = await provisioning.resolve_unknown_workflow_create(
                session, source_id, created_operation_id, str(step_value), workflow_id,
                scope=scope, multi_workspace_enabled=flag, original_operation=original, access_fence=fence,
            )
            if resolved:
                await provisioning.commit_retained_connector_effect(
                    session, before, source_id=source_id, original_operation=original, scope=scope,
                    multi_workspace_enabled=flag, access_fence=fence,
                )
                return 1
            await session.rollback()
    async with factory() as session:
        try:
            deferred = await provisioning.defer_unknown_workflow_create(
                session, source_id, str(operation_value), str(step_value), scope=scope,
                multi_workspace_enabled=flag, original_operation=original, access_fence=fence,
            )
        except provisioning.RetainedEffectAdmissionDenied:
            deferred = False
        if deferred:
            # Bookkeeping only (a later retry time): locks were taken by the original-fence helper above.
            await session.commit()
        else:
            await session.rollback()
    return 0


async def _advance_transitions(
    factory: async_sessionmaker[AsyncSession], settings: Settings, state: dict[str, object] | None,
) -> int:
    """Resume persisted backend transitions (draining, stop-old, native activation) after a crash or retry.

    A transition carries no envelope of its own, so it runs as the workspace owner under a freshly read
    access fence, like scheduled collection. reconciliation_required and an n8n activation wait for the owner.
    Works without an n8n key: native-only deployments still finish transitions that need no n8n call.
    """
    from sqlalchemy import text

    from core.workspaces.models import WorkspaceMembership
    from modules.settings.public import module_is_enabled

    flag = settings.multi_workspace_enabled
    api_key = settings.n8n_api_key.get_secret_value()
    api = N8nApi(str(settings.n8n_service_url), api_key) if api_key else None
    async with factory() as session:
        ids = list((await session.scalars(_page(
            select(ConnectorProvisioning.source_id), ConnectorProvisioning.source_id,
            await _read_cursor(state, "connectors_transitions"),
            or_(ConnectorProvisioning.transition_phase.in_(("draining", "deactivating_old")),
                and_(ConnectorProvisioning.transition_phase == "activating_new",
                     ConnectorProvisioning.target_backend == "native",
                     ConnectorProvisioning.error_code == "backend_transition_pending")),  # a failed activation waits for the owner
        ))).all())
        await session.rollback()
    await _write_cursor(state, "connectors_transitions", ids[-1] if len(ids) >= _PASS_LIMIT else None)
    advanced = 0
    for source_id in ids:
        async with factory() as session:
            try:
                workspace_id = await session.scalar(
                    text("SELECT workspace_id FROM sources WHERE id = :id"), {"id": source_id})
                row = await session.get(ConnectorProvisioning, source_id)
                owner = await session.scalar(select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.role == "owner",
                )) if workspace_id is not None else None
                if owner is None or row is None:
                    await session.rollback()
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id, membership_revision=owner.revision,
                    source_id=source_id, source_generation=row.source_generation)
                await session.rollback()
                if not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=flag):
                    await session.rollback()
                    continue
                fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=flag)
                await session.rollback()
                await provisioning.advance_backend_transition(
                    session, source_id, api, scope=scope, multi_workspace_enabled=flag, access_fence=fence)
                advanced += 1
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
    return advanced


async def reconcile_connectors(ctx: dict[str, object]) -> int:
    """Progress connector provisioning and one bounded GitHub hint dispatch pass.

    Module enablement is per workspace: the cron gate checks build availability only, so each
    subject's workspace is checked after its admission, and disabled work is left untouched.
    Discovery reads only Source ids (ordered, at most 40 per kind, after a shared fairness
    cursor) and every durable operation is then driven under the principal and access fence
    recorded in its own envelope. A denied or legacy subject is skipped, never rebased, and
    cannot starve later subjects because the cursor advances past every discovered page.
    """
    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = settings.multi_workspace_enabled
    state = _cursor_ctx(ctx)
    completed = await dispatch_github_webhooks(ctx)
    completed += await _advance_transitions(factory, settings, state)
    api_key = settings.n8n_api_key.get_secret_value()
    if not api_key:
        return completed
    credentials = N8nCredentials(str(settings.n8n_service_url), api_key)
    api = N8nApi(str(settings.n8n_service_url), api_key)
    credential_after = _read_slot_cursor(state, "connectors_credentials")
    async with factory() as session:
        credential_rows: list[Any] = list((await session.execute(_page(
            select(ConnectorManagedCredential.source_id, ConnectorManagedCredential.slot,
                   ConnectorManagedCredential.operation_id, ConnectorManagedCredential.operation_envelope["kind"].astext),
            ConnectorManagedCredential.source_id, (credential_after or (None, None))[0],
            ConnectorManagedCredential.operation_envelope["state"].astext == "prepared",
            ConnectorManagedCredential.operation_envelope["kind"].astext == "delete",
            ConnectorManagedCredential.operation_id.is_not(None),
            order=(ConnectorManagedCredential.slot,),
            slot_column=ConnectorManagedCredential.slot, after_slot=(credential_after or (None, None))[1],
        ))).all())
        workflow_ids = list((await session.scalars(_page(
            select(ConnectorProvisioning.source_id), ConnectorProvisioning.source_id,
            await _read_cursor(state, "connectors_workflows"),
            ConnectorProvisioning.workflow_operation["step"]["state"].astext == "prepared",
        ))).all())
        unknown_create_ids = list((await session.scalars(_page(
            select(ConnectorProvisioning.source_id), ConnectorProvisioning.source_id,
            await _read_cursor(state, "connectors_unknown_create"),
            ConnectorProvisioning.workflow_operation["step"]["state"].astext == "unknown",
            ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
        ))).all())
        activation_ids = list((await session.scalars(_page(
            select(ConnectorProvisioning.source_id), ConnectorProvisioning.source_id,
            await _read_cursor(state, "connectors_activation"),
            ConnectorProvisioning.desired_enabled.is_(True),
            ConnectorProvisioning.state == "provisioning",
            ConnectorProvisioning.workflow_operation.is_(None),
            ConnectorProvisioning.activation_intent.is_not(None),
        ))).all())
        await session.rollback()
    _write_slot_cursor(
        state, "connectors_credentials",
        (credential_rows[-1][0], credential_rows[-1][1]) if len(credential_rows) >= _PASS_LIMIT else None,
    )
    for name, found in (("connectors_workflows", workflow_ids), ("connectors_unknown_create", unknown_create_ids),
                        ("connectors_activation", activation_ids)):
        await _write_cursor(state, name, found[-1] if len(found) >= _PASS_LIMIT else None)

    for source_id, slot, operation_id, kind in credential_rows:
        if operation_id is None or kind != "delete":
            continue
        async with factory() as session:
            try:
                completed += await _delete_credential(
                    session, credentials, source_id, slot, operation_id, multi_workspace_enabled=flag,
                )
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise

    for source_id in workflow_ids:
        async with factory() as session:
            try:
                completed += await _drive_workflow(session, api, source_id, flag=flag)
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise

    for source_id in unknown_create_ids:
        try:
            completed += await _recover_unknown_create(factory, api, source_id, flag=flag)
        except HTTPException as exc:
            if exc.status_code not in _DENIED:
                raise

    for source_id in activation_ids:
        async with factory() as session:
            try:
                completed += await _resume_activation(session, source_id, api, credentials, settings)
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
    return completed


def _webhook_retry_delay(attempt: int) -> int:
    """Bound durable webhook dispatch retry spacing between 30 seconds and one hour."""
    return int(min(30 * (2 ** max(0, attempt - 1)), 3600))


async def dispatch_github_webhooks(ctx: dict[str, object]) -> int:
    """Fan out bounded frozen binding pages with durable target admissions and fair hint recovery.

    Binding snapshots are fetched without Source/outbox locks, then frozen under an exact outbox
    cursor check. Each public binding DTO is explicitly dumped and validated into the stricter
    private persisted DTO, preserving its field, identity, and bounds checks. Per-target admission
    and hint/capacity effects commit together; page advancement clears that ledger only after the
    same cursor/page still owns the result. Deferred hints that cannot revalidate current authority
    get a durable retry deadline and never reserve or wake.

    The receiver ledger (deliveries, outboxes, capacity) is a deliberate instance global, so binding
    discovery runs under explicit operator admission (``instance_operator=True``). Each binding is then
    a separate subject: its real workspace, actor, membership revision, Source and generation come
    from the frozen page entry, ``enqueue_github_hint`` admits that original identity itself, and a
    denied, disabled or vanished binding is skipped (webhook content is only a hint; polling and
    reauthorization remain authoritative) instead of failing the whole delivery for every workspace.
    Deferred-hint recovery and the wake use Recipe S per Source, with the connectors module checked
    per workspace after admission.
    """
    from datetime import UTC, datetime, timedelta

    from modules.connectors import public as connectors
    from modules.connectors.github.webhooks import GitHubTargetHint
    from modules.connectors.public import (
        _GitHubFanoutBinding,
        _GitHubFanoutPage,
        _validated_github_fanout_page,
    )
    from modules.settings.public import module_is_enabled

    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = settings.multi_workspace_enabled
    now = datetime.now(UTC)
    progressed = await expire_github_webhook_details(factory, now=now)
    progressed += await reclaim_github_webhook_digests(factory, now=now)
    progressed += await revoke_deleted_source_github_grants(factory, settings, cursor_ctx=_cursor_ctx(ctx))
    async with factory() as session:
        outbox_ids = list((await session.scalars(select(GithubWebhookOutbox.id).where(
            GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")),
            GithubWebhookOutbox.next_attempt_at <= now,
        ).order_by(GithubWebhookOutbox.next_attempt_at, GithubWebhookOutbox.id).limit(50))).all())

    for outbox_id in outbox_ids:
        async with factory() as session:
            outbox = await session.get(GithubWebhookOutbox, outbox_id)
            delivery = await session.get(GithubWebhookDelivery, outbox.delivery_id) if outbox else None
            if outbox is None or delivery is None:
                await session.rollback()
                continue
            cursor = outbox.binding_cursor
            stored_page = outbox.fanout_page
            delivery_snapshot: dict[str, Any] = {
                "id": delivery.id, "app_id": delivery.app_id,
                "installation_id": delivery.installation_id,
                "repository_id": delivery.repository_id,
                "targets": delivery.targets,
            }
            await session.rollback()

        if not delivery_snapshot["installation_id"]:
            async with factory() as session:
                current = await session.scalar(select(GithubWebhookOutbox).where(
                    GithubWebhookOutbox.id == outbox_id,
                ).with_for_update())
                if (
                    current is None or current.state not in {"pending", "dispatched", "needs_attention"}
                    or current.binding_cursor != cursor or current.fanout_page != stored_page
                ):
                    await session.rollback()
                    continue
                capacity = await session.scalar(select(GithubWebhookCapacity).where(
                    GithubWebhookCapacity.id == 1,
                ).with_for_update())
                current.state = "complete"
                current.binding_cursor = None
                current.fanout_page = None
                if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
                    capacity.pending_count -= 1
                if current.capacity_reserved:
                    current.capacity_reserved = False
                await session.commit()
            progressed += 1
            continue

        fanout_page: _GitHubFanoutPage | None = None
        failed = False
        if stored_page is not None:
            try:
                candidate = _validated_github_fanout_page(stored_page)
                if candidate.cursor_before != cursor:
                    raise ValueError("github_fanout_page_cursor_stale")
                fanout_page = candidate
            except ValueError:
                failed = True
        else:
            detached_bindings = None
            try:
                async with factory() as session:
                    detached_bindings = await connectors.list_github_event_bindings(
                        session, app_id=str(delivery_snapshot["app_id"]),
                        installation_id=str(delivery_snapshot["installation_id"]),
                        repository_id=str(delivery_snapshot["repository_id"]) if delivery_snapshot["repository_id"] else None,
                        limit=50, cursor=cursor,
                        multi_workspace_enabled=flag, instance_operator=True,
                    )
                    await session.rollback()
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                failed = True
            if detached_bindings is not None:
                try:
                    # Convert the public DTO to a mapping explicitly; strict private DTO validation
                    # does not coerce a distinct Pydantic model instance by attribute name.
                    frozen_bindings = tuple(
                        _GitHubFanoutBinding.model_validate(item.model_dump(mode="python"))
                        for item in detached_bindings.items
                    )
                    candidate = _GitHubFanoutPage(
                        cursor_before=cursor,
                        cursor_after=detached_bindings.next_cursor,
                        bindings=frozen_bindings,
                        admissions=(),
                    )
                except (TypeError, ValueError):
                    candidate = None
                    failed = True
                if candidate is None:
                    detached_bindings = None
            if detached_bindings is not None and candidate is not None:
                async with factory() as session:
                    current = await session.scalar(select(GithubWebhookOutbox).where(
                        GithubWebhookOutbox.id == outbox_id,
                    ).with_for_update())
                    if (
                        current is None or current.state not in {"pending", "dispatched", "needs_attention"}
                        or current.binding_cursor != cursor
                    ):
                        await session.rollback()
                        continue
                    if current.fanout_page is None:
                        current.fanout_page = candidate.model_dump(mode="json")
                        await session.commit()
                        fanout_page = candidate
                    else:
                        try:
                            persisted = _validated_github_fanout_page(current.fanout_page)
                            if persisted.cursor_before != cursor:
                                raise ValueError("github_fanout_page_cursor_stale")
                            fanout_page = persisted
                            await session.rollback()
                        except ValueError:
                            await session.rollback()
                            failed = True
        if fanout_page is not None and not failed:
            for binding in fanout_page.bindings:
                if binding.app_id != delivery_snapshot["app_id"] or binding.installation_id != delivery_snapshot["installation_id"]:
                    continue
                if delivery_snapshot["repository_id"] and binding.repository_id != delivery_snapshot["repository_id"]:
                    continue
                for raw_target in delivery_snapshot["targets"]:
                    try:
                        target = GitHubTargetHint.model_validate(raw_target)
                    except (TypeError, ValueError):
                        failed = True
                        break
                    if target.locator_kind == "installation" and target.locator != binding.installation_id:
                        continue
                    if target.locator_kind == "repository" and target.locator != binding.repository_id:
                        continue
                    if delivery_snapshot["repository_id"] and target.locator_kind not in {"installation"} and target.locator_kind != "repository" and binding.repository_id != delivery_snapshot["repository_id"]:
                        continue
                    try:
                        binding_scope = InternalJobScope(
                            workspace_id=binding.workspace_id, actor_user_id=binding.actor_user_id,
                            membership_revision=binding.membership_revision, source_id=binding.source_id,
                            source_generation=binding.source_generation,
                        )
                        async with factory() as session:
                            if not await module_is_enabled(
                                session, "connectors", scope=binding_scope, multi_workspace_enabled=flag,
                            ):
                                await session.rollback()
                                continue
                            result = await connectors.enqueue_github_hint(
                                session, binding=binding,
                                delivery_receipt_id=delivery_snapshot["id"],
                                target=target,
                                expected_binding_cursor=fanout_page.cursor_before,
                                scope=binding_scope, multi_workspace_enabled=flag,
                            )
                        if result.disposition in {"capacity_exhausted", "stale_progress"}:
                            failed = True
                            break
                    except HTTPException as exc:
                        if exc.status_code in _DENIED:
                            continue  # this binding's owner is not admissible now; other bindings still proceed
                        failed = True
                        break
                    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                        failed = True
                        break
                if failed:
                    break

        async with factory() as session:
            current = await session.scalar(select(GithubWebhookOutbox).where(
                GithubWebhookOutbox.id == outbox_id,
            ).with_for_update())
            if current is None:
                await session.rollback()
                continue
            if fanout_page is not None:
                try:
                    current_page = _validated_github_fanout_page(current.fanout_page)
                except ValueError:
                    current_page = None
                if (
                    current.state not in {"pending", "dispatched", "needs_attention"}
                    or current.binding_cursor != fanout_page.cursor_before
                    or current_page is None
                    or current_page.cursor_before != fanout_page.cursor_before
                    or current_page.cursor_after != fanout_page.cursor_after
                    or current_page.bindings != fanout_page.bindings
                ):
                    await session.rollback()
                    continue
            elif (
                current.state not in {"pending", "dispatched", "needs_attention"}
                or current.binding_cursor != cursor or current.fanout_page != stored_page
            ):
                await session.rollback()
                continue
            capacity = await session.scalar(select(GithubWebhookCapacity).where(
                GithubWebhookCapacity.id == 1,
            ).with_for_update())
            if failed:
                current.attempts = min(5, current.attempts + 1)
                current.state = "needs_attention" if current.attempts >= 5 else "pending"
                current.next_attempt_at = now + timedelta(
                    seconds=3600 if current.state == "needs_attention" else _webhook_retry_delay(current.attempts)
                )
            elif fanout_page is not None and fanout_page.cursor_after is not None:
                current.binding_cursor = fanout_page.cursor_after
                current.fanout_page = None
                current.state = "pending"
                current.next_attempt_at = now
            else:
                current.state = "complete"
                current.binding_cursor = None
                current.fanout_page = None
                if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
                    capacity.pending_count -= 1
                if current.capacity_reserved:
                    current.capacity_reserved = False
            await session.commit()
            progressed += 1

    async with factory() as session:
        deferred_source_id = await session.scalar(select(GithubSourceHint.source_id).where(
            GithubSourceHint.state == "capacity_deferred",
            GithubSourceHint.capacity_reserved.is_(False),
            GithubSourceHint.next_attempt_at <= now,
        ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at).limit(1))
        if deferred_source_id is not None:
            admitted = await _admit_source_job(session, deferred_source_id, multi_workspace_enabled=flag)
            if admitted is not None:
                deferred_scope, deferred_fence = admitted
                try:
                    locked = await sources.lock_source(
                        session, deferred_source_id, scope=deferred_scope, multi_workspace_enabled=flag,
                        expected_access_fence=deferred_fence,
                    )
                    source = await sources.get_connector_source(
                        session, deferred_source_id, scope=deferred_scope, multi_workspace_enabled=flag,
                    ) if locked is not None else None
                    if source is not None and source.status == "active" and await module_is_enabled(
                        session, "connectors", scope=deferred_scope, multi_workspace_enabled=flag,
                    ):
                        await connectors.reconcile_github_source_hints_lifecycle(
                            session, source_id=deferred_source_id,
                            source_generation=source.generation, active=True,
                            scope=deferred_scope, multi_workspace_enabled=flag,
                        )
                except HTTPException as exc:
                    await session.rollback()
                    if exc.status_code not in _DENIED:
                        raise
            # Pushing the Source's deferred hints later also rotates a denied/disabled Source out of the head.
            retry_base = datetime.now(UTC)
            retry_at = retry_base + timedelta(seconds=60)
            remaining = list((await session.scalars(select(GithubSourceHint).where(
                GithubSourceHint.source_id == deferred_source_id,
                GithubSourceHint.state == "capacity_deferred",
                GithubSourceHint.capacity_reserved.is_(False),
                GithubSourceHint.next_attempt_at <= retry_base,
            ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.id).limit(100).with_for_update(skip_locked=True))).all())
            for deferred_hint in remaining:
                deferred_hint.next_attempt_at = max(deferred_hint.next_attempt_at, retry_at)
            await session.commit()

        hint = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
            GithubSourceHint.capacity_reserved.is_(True),
            GithubSourceHint.reconcile_page <= 100,
            GithubSourceHint.next_attempt_at <= now,
        ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at).limit(1).with_for_update(skip_locked=True))
        if hint is None:
            await session.rollback()
            return progressed
        snapshot = (hint.id, hint.source_id, hint.source_generation, hint.connector_revision, hint.dirty_revision)
        needs_attention = hint.state == "needs_attention" or hint.attempts >= 4
        hint.attempts = min(5, hint.attempts + 1)
        attempt = hint.attempts
        needs_attention = needs_attention or attempt >= 5
        hint.state = "needs_attention" if needs_attention else "dispatched"
        hint.next_attempt_at = now + timedelta(
            seconds=3600 if needs_attention else _webhook_retry_delay(attempt)
        )
        await session.commit()
    async with factory() as session:
        wake: Any = None
        admitted = await _admit_source_job(session, snapshot[1], multi_workspace_enabled=flag)
        if admitted is not None:
            wake_scope, _wake_fence = admitted
            try:
                if await module_is_enabled(session, "connectors", scope=wake_scope, multi_workspace_enabled=flag):
                    wake = await connectors.wake_packaged_collection(
                        session, source_id=snapshot[1], source_generation=snapshot[2],
                        connector_revision=snapshot[3], settings=settings, timeout_seconds=15,
                        scope=wake_scope, multi_workspace_enabled=flag,
                    )
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
    async with factory() as session:
        hint = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.id == snapshot[0],
        ).with_for_update())
        if hint is not None and hint.dirty_revision == snapshot[4] and hint.state in {"dispatched", "needs_attention"}:
            if wake is None:
                # No admissible subject (or module disabled): retry later, bounded by the attempts cap.
                hint.state = "needs_attention" if hint.attempts >= 5 else "pending"
                hint.next_attempt_at = now + timedelta(
                    seconds=3600 if hint.state == "needs_attention" else _webhook_retry_delay(max(1, hint.attempts))
                )
            elif wake.outcome == "acknowledged":
                hint.state = "needs_attention" if hint.attempts >= 5 else "dispatched"
                hint.next_attempt_at = now + timedelta(
                    seconds=3600 if hint.state == "needs_attention" else _webhook_retry_delay(max(1, hint.attempts))
                )
            elif wake.outcome == "deferred" and wake.next_eligible_at is not None:
                hint.state = "needs_attention" if hint.attempts >= 5 else "pending"
                hint.next_attempt_at = max(
                    wake.next_eligible_at,
                    now + timedelta(seconds=3600 if hint.state == "needs_attention" else 0),
                )
            elif hint.attempts >= 5:
                hint.state = "needs_attention"
                hint.next_attempt_at = now + timedelta(seconds=3600)
            else:
                hint.state = "pending"
                hint.next_attempt_at = now + timedelta(seconds=_webhook_retry_delay(hint.attempts))
            await session.commit()
        else:
            await session.rollback()
    return progressed + 1


async def process_collection_request(ctx: dict[str, object], request_id: str) -> str:
    """Admit one queued native request and hand it to the registered collection executor.

    The executor defaults to the shared collection service (``collection.execute_collection``);
    ``ctx["collection_executor"]`` overrides it (tests, alternative composition). An executor
    error is a retryable failure, and an executor that already settled the request makes the
    fallback settlement a stale no-op.
    """
    from modules.connectors import scheduler
    from modules.connectors.collection import execute_collection

    executor = ctx.get("collection_executor") or execute_collection
    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        admission = await scheduler.admit_collection_request(
            session, UUID(request_id), multi_workspace_enabled=settings.multi_workspace_enabled)
    if admission is None:
        return "deferred"
    try:
        await cast(Any, executor)(ctx, admission)
    except Exception:
        async with factory() as session:
            await scheduler.settle_admission(
                session, admission.request_id, admission.admission_token,
                outcome="failed", error_code="executor_error", retryable=True)
        return "failed"
    return "admitted"
