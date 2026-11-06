"""Owner-session GitHub project summary, grant peers and explicit grant revocation routes."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select

from core.errors import DomainError
from core.ids import uuid7
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import registry
from modules.connectors.github import oauth
from modules.connectors.github._routes_common import (
    _DENIED,
    RECOVERY_GRACE,
    DisconnectRequest,
    OwnerRead,
    OwnerWrite,
    Session,
    WorkspaceRead,
    WorkspaceWrite,
    _admit,
    _configured,
    _flag,
    _FlowCtx,
    _publish,
    _require_current_owner_session,
    router,
)
from modules.connectors.github.schemas import project_github_source_config
from modules.connectors.models import (
    ConnectorProvisioning,
    GithubOAuthCoordinator,
    GithubOAuthGrant,
    GithubOAuthOperation,
)
from modules.sources import public as sources


@router.get("/{source_id}/github/summary")
async def github_project_summary(
    source_id: str,
    session: Session,
    request: Request,
    _owner: OwnerRead,
    scope: WorkspaceRead,
) -> dict[str, object]:
    """Return canonical mapped GitHub counts and the latest observation time for one github source.

    Counts come only from the Timeline public API, i.e. from records that completed collection and
    mapping. ``live_verified`` is always false: no live provider check backs these numbers.
    """
    flag = _flag(request)
    await _admit(session, request, scope, lock=False)
    source_uuid = UUID(source_id)
    source = await sources.get_connector_source(
        session, source_uuid, scope=scope, multi_workspace_enabled=flag
    )
    if source is None or source.provider != "github":
        raise HTTPException(status_code=404, detail="GitHub source not found")
    from modules.timeline import public as timeline

    counts, latest = await timeline.summarize_source_events(
        session,
        source_uuid,
        "github_",
        scope=scope,
        multi_workspace_enabled=flag,
    )
    return {
        "resource_counts": {
            "repositories": 1 if counts else 0,
            "issues": counts.get("github_issue", 0),
            "pull_requests": counts.get("github_pull", 0),
            "commits": counts.get("github_commit", 0),
            "releases": counts.get("github_release", 0),
        },
        "live_verified": False,
        "last_event_at": latest.isoformat() if latest else None,
    }


def _peer_inventory(github_user_id: str, scope: WorkspaceContext) -> Any:
    """Select token-bearing same-GitHub-user grants of Sources in the caller's workspace only.

    The workspace predicate comes from the Sources lifecycle projection and is joined before ordering
    and LIMIT 101, so another workspace's grants can neither appear nor crowd out this inventory.
    ponytail: peers of the same GitHub user in other workspaces are outside this inventory; GitHub's
    app/user-wide revoke still reaches them remotely, and their Source reports it on its next use.
    """
    lifecycle = sources.ingestion_lifecycle_projection(scope=scope).subquery()
    return (
        select(
            GithubOAuthGrant.source_id,
            GithubOAuthGrant.source_generation,
            GithubOAuthGrant.configuration_revision,
            GithubOAuthGrant.token_revision,
            GithubOAuthGrant.state,
        )
        .join(lifecycle, lifecycle.c.id == GithubOAuthGrant.source_id)
        .where(
            GithubOAuthGrant.github_user_id == github_user_id,
            GithubOAuthGrant.encrypted_tokens.is_not(None),
        )
        .order_by(GithubOAuthGrant.source_id)
        .limit(101)
    )


@router.get("/{source_id}/github/peers")
async def list_github_grant_peers(
    source_id: str,
    session: Session,
    request: Request,
    _owner: OwnerRead,
    scope: WorkspaceRead,
) -> dict[str, object]:
    """Return a bounded, secret-free inventory that must be reviewed before app-wide revocation."""
    flag = _flag(request)
    await _admit(session, request, scope, lock=False)
    parsed_id = UUID(source_id)
    if (
        await sources.get_connector_source(
            session, parsed_id, scope=scope, multi_workspace_enabled=flag
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    current_user_id = await session.scalar(
        select(GithubOAuthGrant.github_user_id).where(GithubOAuthGrant.source_id == parsed_id)
    )
    if current_user_id is None:
        return {"complete": True, "peers": []}
    rows = list((await session.execute(_peer_inventory(current_user_id, scope))).all())
    if len(rows) > 100:
        return {"complete": False, "peers": []}
    return {
        "complete": True,
        "github_user_id": current_user_id,
        "peers": [
            {
                "source_id": str(item.source_id),
                "source_generation": item.source_generation,
                "configuration_revision": item.configuration_revision,
                "token_revision": item.token_revision,
                "state": item.state,
            }
            for item in rows
        ],
    }


async def _review_revoke_peers(
    ctx: _FlowCtx,
    payload: DisconnectRequest,
    target_id: UUID,
    target_user_id: str,
) -> tuple[Any, list[Any]]:
    """Check the owner-reviewed peer list against the live workspace inventory and source lifecycles.

    Read-and-lock only (source rows are locked in sorted order under the original fence); nothing
    is written. Returns the inventory statement and its rows for the later locked recheck.

    Raises:
        HTTPException: 409 for an oversized inventory, changed peers/revisions or changed lifecycles.
    """
    session, scope = ctx.session, ctx.scope
    peer_inventory = _peer_inventory(target_user_id, scope).with_only_columns(
        GithubOAuthGrant.source_id,
        GithubOAuthGrant.source_generation,
        GithubOAuthGrant.configuration_revision,
        GithubOAuthGrant.token_revision,
    )
    peer_rows = list((await session.execute(peer_inventory)).all())
    if len(peer_rows) > 100:
        raise HTTPException(
            status_code=409,
            detail="GitHub grant peer inventory is too large; revocation is unavailable",
        )
    expected = {item.source_id: item for item in payload.reviewed_peers}
    actual_ids = {item.source_id for item in peer_rows}
    if set(map(str, actual_ids)) != {item.source_id for item in payload.reviewed_peers}:
        raise HTTPException(
            status_code=409,
            detail="GitHub grant peers changed; review the current affected sources",
        )
    for item in sorted(peer_rows, key=lambda row: str(row.source_id)):
        reviewed = expected.get(str(item.source_id))
        if reviewed is None or (
            item.source_generation,
            item.configuration_revision,
            item.token_revision,
        ) != (reviewed.source_generation, reviewed.configuration_revision, reviewed.token_revision):
            raise HTTPException(
                status_code=409,
                detail="A GitHub source revision changed; review the current affected sources",
            )
        source_lock = await sources.lock_source(
            session,
            item.source_id,
            scope=scope,
            multi_workspace_enabled=ctx.flag,
            expected_access_fence=ctx.fence,
        )
        if (
            source_lock is None
            or source_lock.status == "archived"
            or source_lock.status == "active"
            and source_lock.generation != item.source_generation
            or source_lock.status == "paused"
            and source_lock.generation not in {item.source_generation, item.source_generation + 1}
        ):
            raise HTTPException(
                status_code=409,
                detail="A GitHub source lifecycle changed; review current affected sources",
            )
    return peer_inventory, peer_rows


async def _begin_revocation(
    ctx: _FlowCtx,
    payload: DisconnectRequest,
    target_id: UUID,
    peer_inventory: Any,
    peer_rows: list[Any],
) -> tuple[GithubOAuthCoordinator, GithubOAuthOperation, UUID | None]:
    """Lock the owner's coordinator, recheck the peer inventory under the lock and open the revoke operation.

    The coordinator must be idle unless this is an explicit retry of the same unresolved revoke
    after the recovery grace. The operation row is created (or reused on retry) as
    ``in_progress`` / ``provider_revoke_pending``; nothing is committed here.

    Raises:
        HTTPException: 409 when another operation needs reconciliation or peers changed while waiting.
    """
    session, scope, actor = ctx.session, ctx.scope, ctx.actor
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == actor)
        .with_for_update()
    )
    explicit_revoke_retry = bool(
        coordinator is not None
        and coordinator.state in {"reconciliation_required", "revoking"}
        and coordinator.error_code in {"provider_revoke_outcome_unknown", "provider_revoke_pending"}
        and payload.operation_id == coordinator.operation_id
        and (
            coordinator.state == "reconciliation_required"
            or coordinator.updated_at <= datetime.now(UTC) - RECOVERY_GRACE
        )
    )
    if coordinator is not None and coordinator.state != "idle" and not explicit_revoke_retry:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A GitHub connection operation needs reconciliation before disconnect",
        )
    if coordinator is None:
        coordinator = GithubOAuthCoordinator(owner_id=actor, state="idle")
        session.add(coordinator)
        await session.flush()
    # A newly authorized peer may have appeared while this request waited for the
    # app/user operation lock; recheck the complete inventory before fencing anything.
    locked_inventory = list(
        (await session.execute(peer_inventory.execution_options(populate_existing=True))).all()
    )
    if len(locked_inventory) > 100 or {
        (row.source_id, row.source_generation, row.configuration_revision, row.token_revision)
        for row in locked_inventory
    } != {
        (row.source_id, row.source_generation, row.configuration_revision, row.token_revision)
        for row in peer_rows
    }:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="GitHub grant peers changed; review the current affected sources",
        )
    coordinator.state = "revoking"
    coordinator.error_code = "provider_revoke_pending"
    revocation_operation = coordinator.operation_id if explicit_revoke_retry else uuid7()
    coordinator.operation_id = revocation_operation
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == revocation_operation,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    origin = next((row for row in peer_rows if row.source_id == target_id), peer_rows[0])
    peer_snapshot = [
        {
            "source_id": str(item.source_id),
            "source_generation": item.source_generation,
            "configuration_revision": item.configuration_revision,
            "token_revision": item.token_revision,
        }
        for item in peer_rows
    ]
    if operation is None:
        operation = GithubOAuthOperation(
            operation_id=revocation_operation,
            workspace_id=scope.workspace_id,
            owner_id=actor,
            operation_kind="revoke",
            source_id=origin.source_id,
            source_generation=origin.source_generation,
            configuration_revision=origin.configuration_revision,
            token_revision=origin.token_revision,
            peer_inventory=peer_snapshot,
        )
        session.add(operation)
    operation.state, operation.error_code, operation.resolved_at = (
        "in_progress",
        "provider_revoke_pending",
        None,
    )
    return coordinator, operation, revocation_operation


async def _fence_revoke_peers(ctx: _FlowCtx, peer_rows: list[Any], key: str) -> Any:
    """Pause every reviewed peer (sorted order) and mark its grant revoked/pending; return one access token.

    Each peer's source, provisioning and grant rows are locked and revalidated against the reviewed
    revisions; the first openable ciphertext yields the provider access token (``None`` if none).

    Raises:
        HTTPException: 409 when a source, configuration, grant or lifecycle changed (after rollback).
    """
    session, scope, flag, fence = ctx.session, ctx.scope, ctx.flag, ctx.fence
    token = None
    for item in sorted(peer_rows, key=lambda row: str(row.source_id)):
        source_fence = await sources.lock_source(
            session,
            item.source_id,
            scope=scope,
            multi_workspace_enabled=flag,
            expected_access_fence=fence,
        )
        current_source = (
            await sources.get_connector_source(
                session, item.source_id, scope=scope, multi_workspace_enabled=flag
            )
            if source_fence is not None
            else None
        )
        row = await session.scalar(
            select(ConnectorProvisioning)
            .where(ConnectorProvisioning.source_id == item.source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.desired_revision != item.configuration_revision:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A GitHub source configuration changed")
        locked = await session.scalar(
            select(GithubOAuthGrant)
            .where(GithubOAuthGrant.source_id == item.source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            if current_source is not None:
                registry.configuration(current_source)
                project_github_source_config(current_source.configuration)
        except (ValueError, TypeError):
            current_source = None
        lifecycle_matches = bool(
            current_source is not None
            and locked is not None
            and (
                current_source.generation == locked.source_generation
                if current_source.status == "active"
                else current_source.status == "paused"
                and current_source.generation
                in {locked.source_generation, locked.source_generation + 1}
            )
        )
        if (
            current_source is None
            or current_source.id != item.source_id
            or current_source.provider != "github"
            or current_source.status == "archived"
            or not lifecycle_matches
            or locked is None
            or locked.source_generation != item.source_generation
            or locked.configuration_revision != row.desired_revision
            or locked.token_revision != item.token_revision
            or locked.state not in {"ready", "reconciliation_required", "revoked"}
            or locked.encrypted_tokens is None
        ):
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="A GitHub grant changed; review current affected sources"
            )
        if token is None:
            opened = oauth._open_token_cipher(
                key,
                locked.encrypted_tokens,
                locked.source_id,
                locked.operation_id,
                locked.source_generation,
                locked.configuration_revision,
            )
            token = opened.get("access_token")
        source = await sources.pause_source_for_connector(
            session,
            item.source_id,
            scope=scope,
            multi_workspace_enabled=flag,
            expected_access_fence=fence,
        )
        if source is None:
            await session.rollback()
            raise HTTPException(status_code=409, detail="A GitHub source lifecycle changed")
        locked.state = "revoked"
        locked.error_code = "provider_revoke_pending"
    return token


async def _record_revoke_outcome_unknown(ctx: _FlowCtx, revocation_operation: UUID | None) -> None:
    """After a failed provider revoke, flag the coordinator/operation for reconciliation when still ours.

    Local grants stay fenced. If re-admission is denied the session is rolled back and a
    non-denial HTTPException is re-raised; otherwise the unresolved state is published.
    """
    session, request, scope, fence = ctx.session, ctx.request, ctx.scope, ctx.fence
    try:
        await _admit(session, request, scope, expected=fence)
    except (DomainError, HTTPException) as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
    else:
        coordinator = await session.scalar(
            select(GithubOAuthCoordinator)
            .where(GithubOAuthCoordinator.owner_id == ctx.actor)
            .with_for_update()
        )
        if coordinator is not None and coordinator.operation_id == revocation_operation:
            coordinator.state = "reconciliation_required"
            coordinator.error_code = "provider_revoke_outcome_unknown"
            operation = await session.scalar(
                select(GithubOAuthOperation)
                .where(
                    GithubOAuthOperation.operation_id == revocation_operation,
                    GithubOAuthOperation.workspace_id == scope.workspace_id,
                )
                .with_for_update()
            )
            if operation is not None:
                operation.state = "review_required"
                operation.error_code = "provider_revoke_outcome_unknown"
            await _publish(session, request, scope, fence)


async def _finish_revocation(
    ctx: _FlowCtx, peer_rows: list[Any], revocation_operation: UUID | None
) -> None:
    """After the provider revoke succeeded, wipe revoked ciphertexts and resolve coordinator and operation."""
    session, request, scope, fence = ctx.session, ctx.request, ctx.scope, ctx.fence
    await _admit(session, request, scope, expected=fence)
    for item in peer_rows:
        locked = await session.scalar(
            select(GithubOAuthGrant)
            .where(GithubOAuthGrant.source_id == item.source_id)
            .with_for_update()
        )
        if locked is not None and locked.state == "revoked":
            locked.encrypted_tokens = None
            locked.error_code = None
    coordinator = await session.scalar(
        select(GithubOAuthCoordinator)
        .where(GithubOAuthCoordinator.owner_id == ctx.actor)
        .with_for_update()
    )
    if coordinator is not None and coordinator.operation_id == revocation_operation:
        coordinator.state = "idle"
        coordinator.operation_id = None
        coordinator.error_code = None
    operation = await session.scalar(
        select(GithubOAuthOperation)
        .where(
            GithubOAuthOperation.operation_id == revocation_operation,
            GithubOAuthOperation.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if operation is not None:
        operation.state, operation.error_code, operation.resolved_at = (
            "completed",
            None,
            datetime.now(UTC),
        )
    await _publish(session, request, scope, fence)


@router.post("/{source_id}/github/disconnect")
async def revoke_github_grant(
    source_id: str,
    payload: DisconnectRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
    scope: WorkspaceWrite,
) -> dict[str, str]:
    """Fence every reviewed local peer atomically before GitHub's app/user-wide grant revoke.

    Peer inventory is workspace-scoped, bounded and rechecked under row locks; the access fence is
    locked first, then source rows in sorted order before provisioning and grant rows. The same
    ORIGINAL fence is re-admitted after the provider call. Remote uncertainty leaves local access fenced.
    """
    _configured(request.app.state.settings)
    flag = _flag(request)
    target_id = UUID(source_id)
    fence = await _admit(session, request, scope)
    ctx = _FlowCtx(
        request, session, _owner, scope, flag, scope.user_id, fence, request.app.state.settings
    )
    if (
        await sources.get_connector_source(
            session, target_id, scope=scope, multi_workspace_enabled=flag
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    target_user_id = await session.scalar(
        select(GithubOAuthGrant.github_user_id).where(GithubOAuthGrant.source_id == target_id)
    )
    if target_user_id is None:
        return {"state": "revoked"}
    peer_inventory, peer_rows = await _review_revoke_peers(ctx, payload, target_id, target_user_id)
    key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
    coordinator, operation, revocation_operation = await _begin_revocation(
        ctx, payload, target_id, peer_inventory, peer_rows
    )
    token = await _fence_revoke_peers(ctx, peer_rows, key)
    await _publish(session, request, scope, fence)
    if not isinstance(token, str):
        coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
        operation.state, operation.error_code, operation.resolved_at = (
            "completed",
            None,
            datetime.now(UTC),
        )
        await _publish(session, request, scope, fence)
        return {"state": "revoked"}
    await _require_current_owner_session(request, session, _owner)
    try:
        await oauth.revoke_github_grant(request.app.state.settings, token)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # Local grants remain fenced; the owner can see cleanup is unresolved without exposing credentials.
        await _record_revoke_outcome_unknown(ctx, revocation_operation)
        raise HTTPException(
            status_code=503,
            detail="Local GitHub sources are disconnected; provider revocation outcome is unknown",
        ) from None
    await _finish_revocation(ctx, peer_rows, revocation_operation)
    return {"state": "revoked"}
