"""Collection, validation and n8n-backend fences plus retired-credential and grant cleanup."""

import copy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.backends import backend_admits
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    GithubOAuthGrant,
)
from modules.connectors.provisioning._activation import _new_deactivation
from modules.connectors.provisioning._common import _lock_connector_rows
from modules.connectors.public import (
    _connector_access,
    _read_scoped_source,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence


async def clear_retired_source_credentials(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Acquire native then GitHub grant and retire archived Source credentials, flush only.

    Legacy acquiring continuation requires caller-held admission/Source/provisioning and
    no already-acquired later rows. Ordinary lifecycle apply instead passes its prepared
    native and later-held grant to the private held mutation below. Peer retirement retains
    the exact archived anchor and pending last-source provider liability; no network I/O.
    """
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError("Source not found")
    native = await session.scalar(
        select(ConnectorNativeCredential).where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    grant = await session.scalar(
        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    await _clear_retired_source_credentials_held(
        session, source_id, native=native, grant=grant, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _clear_retired_source_credentials_held(
    session: AsyncSession, source_id: UUID, *, native: ConnectorNativeCredential | None,
    grant: GithubOAuthGrant | None, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Retire already-held native/grant rows without acquiring earlier lifecycle locks.

    Caller proves the exact archived Source/access and holds optional native before tokens,
    then the later GitHub grant before hints/capacity. Missing rows are independent empty
    sets. Native loses ciphertext/fingerprint/validation/bot reservation under a new exact
    operation ID. GitHub's unchanged peer guard rechecks owned operation/token/config/binding
    and joins Source's archived-anchor sibling IDs before LIMIT101: a proven peer clears local
    ciphertext, an empty set retains pending last-source revoke. No provider I/O/commit occurs.
    """
    if any(item is not None and item.source_id != source_id for item in (native, grant)):
        raise ConflictError("Retired Source credential identity changed")
    if native is not None and (native.state != "revoked" or native.verified_bot_id is not None):
        native.operation_id = uuid7()
        native.encrypted_token = None
        native.token_fingerprint = None
        native.validated_at = None
        native.state = "revoked"
        native.error_code = None
        native.verified_bot_id = None
    if grant is not None and grant.encrypted_tokens is not None:
        has_peer = await github_grant_has_active_peer(
            session, grant, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        grant.state = "revoked"
        grant.refresh_operation_id = None
        if has_peer:
            # GitHub's revoke is app/user-wide; another live source still needs the account grant.
            grant.encrypted_tokens = None
            grant.error_code = "provider_revoke_skipped_source_deleted"
        else:
            # Last source of this GitHub account: keep the ciphertext only until the worker's
            # best-effort remote revoke (outside this transaction) clears it, whatever the outcome.
            grant.error_code = "provider_revoke_pending_source_deleted"
    await session.flush()


async def github_grant_has_active_peer(session: AsyncSession, grant: Any, *, scope: Scope, multi_workspace_enabled: bool) -> bool:
    """Check token-bearing nonarchived peers of an exactly proved archived GitHub anchor.

    Caller holds ordered admission/anchor Source/provisioning/grant locks and preserves
    its original scope. Source's narrow IDs-only retirement projection proves actual
    owner/default workspace and anchor; no scope strip or peer Source locks. Namespace
    and token/user predicates precede LIMIT 101; empty is False, any peer is True.
    Fresh current fences here cannot replace caller's original send/publication CAS.
    """
    from modules.connectors.models import GithubOAuthGrant

    identity_fields = ("source_id", "operation_id", "token_revision", "github_user_id",
                       "source_generation", "configuration_revision", "binding_revision")
    observed = tuple(getattr(grant, field, None) for field in identity_fields)
    source_id = observed[0]
    if not isinstance(source_id, UUID) or not isinstance(observed[1], UUID):
        raise ConflictError("GitHub retirement grant changed")
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source_fence = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source_fence is None:
        raise ConflictError("GitHub retirement Source changed")
    current = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).execution_options(populate_existing=True))
    if current is None or tuple(getattr(current, field) for field in identity_fields) != observed:
        raise ConflictError("GitHub retirement grant changed")
    projection = (await sources.github_retirement_peer_projection_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )).subquery()
    peer_ids = list(await session.scalars(select(GithubOAuthGrant.source_id).join(
        projection, projection.c.id == GithubOAuthGrant.source_id,
    ).where(
        GithubOAuthGrant.github_user_id == current.github_user_id,
        GithubOAuthGrant.source_id != source_id,
        GithubOAuthGrant.encrypted_tokens.is_not(None),
    ).order_by(GithubOAuthGrant.source_id).limit(101)))
    return bool(peer_ids)


async def finish_deleted_source_grant_revoke(
    session: AsyncSession, source_id: UUID, outcome_code: str,
    *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, grant_operation_id: UUID, token_revision: int,
) -> None:
    """Clear the retained ciphertext of a deleted source's grant and record the revoke outcome.

    Clear only the exact captured grant operation/token revision under original access CAS;
    a successor grant is never cleared by an old remote result. Caller owns final commit.
    """
    from modules.connectors.models import GithubOAuthGrant

    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    if source is None:
        raise NotFoundError("Source not found")
    grant = await session.scalar(
        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if grant is not None and grant.operation_id == grant_operation_id and grant.token_revision == token_revision:
        grant.encrypted_tokens = None
        grant.error_code = outcome_code
        await session.flush()


async def _apply_collection_fence(
    session: AsyncSession, source: SourceFence, row: ConnectorProvisioning,
    slots: dict[str, ConnectorManagedCredential], *, scope: Scope, access_fence: AccessFence,
) -> bool:
    """Apply complete provisioning/activation/workflow cleanup to already-prepared rows.

    No read, lock, commit or I/O is reachable. Source owner validated the current lifecycle
    fence; callers hold provisioning and every managed slot referenced by activation.
    """
    row.source_generation = source.generation
    row.desired_enabled = False
    row.state = "disabled"
    activation = row.activation_intent
    if isinstance(activation, dict):
        unresolved = False
        required = activation.get("required_credentials")
        if isinstance(required, dict):
            for slot_name in required:
                credential = slots.get(str(slot_name))
                envelope = credential.operation_envelope if credential is not None else None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("activation_id") == activation.get("id")
                ):
                    if envelope.get("state") == "prepared":
                        if credential is None:
                            raise RuntimeError("invariant violated: credential must not be None")
                        credential.operation_id = None
                        credential.operation_envelope = None
                        credential.state = "ready" if credential.credential_id else "queued"
                    elif envelope.get("state") in {"dispatched", "unknown"}:
                        unresolved = True
        if unresolved:
            activation["state"] = "stale_unresolved"
            row.activation_intent = activation
            row.error_code = "activation_outcome_pending"
        else:
            row.activation_intent = None
    if row.workflow_operation is not None:
        operation = copy.deepcopy(row.workflow_operation)
        step = operation.get("step")
        if isinstance(step, dict) and step.get("state") in {"prepared", "blocked"}:
            row.workflow_operation = None
        else:
            operation["cleanup_required"] = True
            operation["error"] = "source_fenced_during_dispatch"
            row.workflow_operation = operation
            row.error_code = "workflow_operation_pending"
            await session.flush()
            return True
    new_operation = _new_deactivation(row, source.generation, scope=scope, access_fence=access_fence)
    if new_operation is not None:
        row.workflow_operation = new_operation
        row.error_code = "deactivation_pending"
    else:
        row.error_code = None
    await session.flush()
    return True


async def fence_source_collection(
    session: AsyncSession, source: SourceFence,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Disable connector collection at the source generation and schedule cleanup."""
    current_source, row, slots = await _lock_connector_rows(
        session, source.id, ("collector", "manual_trigger", "provider"),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_source != source:
        raise ConflictError("Source lifecycle fence changed")
    if row is None:
        return False
    if source.status == "archived":
        await clear_retired_source_credentials(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await _apply_collection_fence(session, source, row, slots, scope=scope, access_fence=access_fence)


async def require_collection_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    revision: int,
    *,
    lock: bool = False,
    backend_revision: int | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Continue caller-held admission/Source into exact provisioning collection proof.

    Source reread is nonlocking; lock=True acquires only the later provisioning row.
    Active Source DTO/workspace/generation and fully applied revision must agree, and the row's
    own backend must be ready (idle transition, applied backend revision, n8n workflow/template).
    A supplied ``backend_revision`` (every current n8n envelope carries it) must equal the row's.
    """
    current_source = await sources.get_source_fence(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        current_source is None
        or current_source.workspace_id != source.workspace_id
        or current_source.status != source.status
        or current_source.generation != source.generation
    ):
        return False
    statement = select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source.id
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    else:
        statement = statement.execution_options(populate_existing=True)
    row = await session.scalar(statement)
    return bool(
        source.status == "active"
        and source.generation == source_generation
        and row is not None
        and row.source_generation == source_generation
        and row.desired_revision == revision
        and row.applied_revision == revision
        and row.desired_enabled
        and row.state == "active"
        and backend_admits(row)
        and (backend_revision is None or backend_revision == row.backend_revision)
    )


async def require_n8n_backend(session: AsyncSession, source_id: UUID, *, n8n_enabled: bool = True) -> None:
    """Reject packaged-workflow (bearer) calls unless the row is an idle n8n source.

    ``n8n_enabled`` False (owner kill switch, resolved once by the caller) rejects every call with
    409 ``n8n_disabled`` before any row check, so late or in-flight workflow callbacks stop at ingress.

    A native source, or any source mid-transition, never accepts a late or stale n8n webhook.
    Nonlocking read under the caller-held Source lock; a missing row is left to later fences.
    """
    if not n8n_enabled:
        raise ConflictError("n8n_disabled")
    row = await session.scalar(
        select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == source_id)
        .execution_options(populate_existing=True))
    if row is not None and (row.execution_backend != "n8n" or row.transition_phase != "idle"):
        raise ConflictError("backend_inactive")


async def require_validation_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    revision: int,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Lock only provisioning under caller-held access/Source, including initial revision zero.

    This allows an unsaved draft at revision zero while ensuring callers can
    recheck the same authority after a network validation without holding locks
    across that request.
    """
    current_source = await sources.get_source_fence(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        current_source is None or current_source.status != "active"
        or current_source.workspace_id != source.workspace_id
        or current_source.status != source.status
        or current_source.generation != source_generation
        or source.generation != source_generation
    ):
        return False
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return revision == 0
    return row.source_generation == source_generation and row.desired_revision == revision
