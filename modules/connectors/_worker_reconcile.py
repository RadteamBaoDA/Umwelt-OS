"""Connector workflow activation, unknown-create recovery and the ``reconcile_connectors`` job."""

import copy
import logging
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.worker_context import worker_session_factory
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import InternalJobScope
from modules.connectors import provisioning
from modules.connectors._worker_base import (
    _DENIED,
    _PASS_LIMIT,
    _admit_lineage,
    _cursor_ctx,
    _page,
    _read_cursor,
    _read_slot_cursor,
    _write_cursor,
    _write_slot_cursor,
)
from modules.connectors._worker_grants import (
    _delete_credential,
)
from modules.connectors._worker_outbox import (
    dispatch_github_webhooks,
)
from modules.connectors.credentials import N8nCredentials
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.n8n import N8nApi, find_workflows_by_name, workflow_matches

logger = logging.getLogger("modules.connectors.worker")  # same logger object as the facade


async def _resume_activation(
    session: AsyncSession,
    source_id: UUID,
    settings: Settings,
) -> bool:
    """Resume a persisted activation under the principal and fence recorded in its intent.

    The intent's workspace, actor, membership revision, configuration revision and Source
    generation are admitted exactly (no current-owner fallback), the connectors module must be
    enabled for that workspace (otherwise the durable work is left untouched), and SQL is released
    before ``drive_activation`` reacquires its own ordered locks and does all provider I/O.
    Clients and kill switch are resolved only from this original workspace's settings.
    """
    from modules.connectors.activation import drive_activation
    from modules.settings.public import module_is_enabled, resolve_n8n

    flag = settings.multi_workspace_enabled
    intent = await session.scalar(
        select(ConnectorProvisioning.activation_intent)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .execution_options(populate_existing=True)
    )
    admitted = await _admit_lineage(
        session, source_id, copy.deepcopy(intent), multi_workspace_enabled=flag
    )
    if admitted is None:
        return False
    scope, fence = admitted
    if not await module_is_enabled(
        session, "connectors", scope=scope, multi_workspace_enabled=flag
    ):
        await session.rollback()
        return False
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    await session.rollback()
    if not n8n.enabled or not n8n.api_key:
        return False
    api = N8nApi(n8n.service_url, n8n.api_key, settings)
    credentials = N8nCredentials(n8n.service_url, n8n.api_key)
    return await drive_activation(
        session,
        source_id,
        api,
        credentials,
        settings.connector_credential_encryption_key.get_secret_value(),
        scope=scope,
        multi_workspace_enabled=flag,
        access_fence=fence,
    )


async def _drive_workflow(
    session: AsyncSession, settings: Settings, source_id: UUID, *, flag: bool
) -> int:
    """Drive one prepared workflow step under the envelope's recorded principal and fence.

    Non-delete steps additionally require the connectors module to be enabled for that
    workspace. The immutable envelope copy is the operation identity for every claim/send/result
    inside ``drive_workflow_operation``, which releases SQL before network calls.
    Resolve clients and the kill switch from the original admitted workspace only.
    """
    from modules.settings.public import module_is_enabled, resolve_n8n

    envelope = await session.scalar(
        select(ConnectorProvisioning.workflow_operation)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .execution_options(populate_existing=True)
    )
    original = copy.deepcopy(envelope) if isinstance(envelope, dict) else None
    step = original.get("step") if original is not None else None
    if original is None or not isinstance(step, dict) or step.get("state") != "prepared":
        await session.rollback()
        return 0
    admitted = await _admit_lineage(session, source_id, original, multi_workspace_enabled=flag)
    if admitted is None:
        return 0
    scope, fence = admitted
    if step.get("kind") != "delete" and not await module_is_enabled(
        session,
        "connectors",
        scope=scope,
        multi_workspace_enabled=flag,
    ):
        await session.rollback()
        return 0
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    await session.rollback()
    if not n8n.api_key or (not n8n.enabled and original.get("kind") != "deactivate"):
        return 0
    api = N8nApi(n8n.service_url, n8n.api_key, settings)
    return int(
        await provisioning.drive_workflow_operation(
            session,
            source_id,
            api,
            scope=scope,
            multi_workspace_enabled=flag,
            original_operation=cast(dict[str, object], original),
            access_fence=fence,
        )
    )


async def _discover_created_workflow(
    api: N8nApi,
    original: dict[str, Any],
    step: dict[str, Any],
    source_id: UUID,
) -> tuple[UUID | None, str | None]:
    """Look up the remote workflow an uncertain create may have produced, by exact name and body.

    Holds no session. Returns ``(operation_id, workflow_id)``; ``workflow_id`` is set only when
    exactly one workflow carries the recorded name and its body matches the recorded request.
    Transport or shape failures leave it ``None`` (logged at debug) so the create stays "unknown"
    and is retried by a later pass; discovery never counts as success on its own.
    """
    name, body = original.get("workflow_name"), step.get("request")
    created_operation_id: UUID | None = None
    workflow_id: str | None = None
    try:
        created_operation_id = UUID(str(original.get("id")))
        matches = await find_workflows_by_name(api, str(name))
        if len(matches) == 1 and isinstance(matches[0].get("id"), str) and isinstance(body, dict):
            candidate_id = str(matches[0]["id"])
            actual = await api.get_workflow(candidate_id)
            if workflow_matches(body, actual):
                workflow_id = candidate_id
    except Exception as exc:
        # Deliberately broad: lookup is discovery only. Any failure (transport, shape, or the egress
        # policy's ``EndpointNetworkPolicyError``, a RuntimeError) leaves ``workflow_id`` unset so the
        # create stays "unknown" and is retried by a later pass instead of aborting the whole
        # reconcile pass; it is never treated as a success.
        logger.debug(
            "connector_unknown_create_lookup_failed source_id=%s error=%s",
            source_id,
            type(exc).__name__,
            exc_info=True,
        )
    return created_operation_id, workflow_id


async def _recover_unknown_create(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    source_id: UUID,
    *,
    flag: bool,
) -> int:
    """Look up an uncertain workflow create and attach its single exact match, or defer it later.

    The unknown-create envelope is captured once under the recorded principal and fence and kept
    unchanged through the n8n lookup (which holds no session). Resolution then locks the original
    admission, retained Source anchor and provisioning rows and commits through
    ``commit_retained_connector_effect``. If the original access is gone, the discovered remote
    workflow ID is journaled (never silently dropped). The create is never repeated.
    Unknown legacy origins or a different client endpoint cannot be reconciled by lookup.
    Resolve URL/key and the kill switch only for the admitted original workspace.
    """
    from modules.settings.public import resolve_n8n

    async with factory() as session:
        stored = await session.scalar(
            select(ConnectorProvisioning.workflow_operation)
            .where(
                ConnectorProvisioning.source_id == source_id,
            )
            .execution_options(populate_existing=True)
        )
        original = copy.deepcopy(stored) if isinstance(stored, dict) else None
        step = original.get("step") if original is not None else None
        if (
            original is None
            or not isinstance(step, dict)
            or step.get("state") != "unknown"
            or step.get("kind") != "create"
        ):
            await session.rollback()
            return 0
        admitted = await _admit_lineage(session, source_id, original, multi_workspace_enabled=flag)
        if admitted is None:
            return 0
        n8n = await resolve_n8n(session, settings, workspace_id=admitted[0].workspace_id)
        if not n8n.enabled or not n8n.api_key:
            await session.rollback()
            return 0
        api = N8nApi(n8n.service_url, n8n.api_key, settings)
        from modules.connectors.n8n import n8n_service_identity

        endpoint = await session.scalar(
            select(ConnectorProvisioning.n8n_service_url).where(
                ConnectorProvisioning.source_id == source_id,
            )
        )
        if not isinstance(endpoint, str) or n8n_service_identity(endpoint) != api.service_url:
            await session.rollback()
            return 0
        await session.rollback()
    scope, fence = admitted
    operation_value, step_value = original.get("id"), step.get("id")
    created_operation_id, workflow_id = await _discover_created_workflow(
        api, original, step, source_id
    )
    if workflow_id is not None and created_operation_id is not None:
        async with factory() as session:
            try:
                before = await provisioning.lock_retained_connector_effect(
                    session,
                    source_id,
                    original_operation=original,
                    scope=scope,
                    multi_workspace_enabled=flag,
                    access_fence=fence,
                )
            except provisioning.RetainedEffectAdmissionDenied:
                await session.rollback()
                disposition = await provisioning.record_retained_workflow_result_in_uow(
                    session,
                    source_id,
                    original_operation=original,
                    scope=scope,
                    access_fence=fence,
                    outcome="known_success",
                    remote_id=workflow_id,
                )
                if disposition in {"stored", "duplicate"}:
                    await session.commit()
                else:
                    await session.rollback()
                return 0
            resolved = await provisioning.resolve_unknown_workflow_create(
                session,
                source_id,
                created_operation_id,
                str(step_value),
                workflow_id,
                scope=scope,
                multi_workspace_enabled=flag,
                original_operation=original,
                access_fence=fence,
            )
            if resolved:
                await provisioning.commit_retained_connector_effect(
                    session,
                    before,
                    source_id=source_id,
                    original_operation=original,
                    scope=scope,
                    multi_workspace_enabled=flag,
                    access_fence=fence,
                )
                return 1
            await session.rollback()
    async with factory() as session:
        try:
            deferred = await provisioning.defer_unknown_workflow_create(
                session,
                source_id,
                str(operation_value),
                str(step_value),
                scope=scope,
                multi_workspace_enabled=flag,
                original_operation=original,
                access_fence=fence,
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
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: dict[str, object] | None,
) -> int:
    """Resume persisted backend transitions (draining, stop-old, native activation) after a crash or retry.

    A transition carries no envelope of its own, so it runs as the workspace owner under a freshly read
    access fence, like scheduled collection. reconciliation_required and an n8n activation wait for the owner.
    Works without an n8n key: native-only deployments still finish transitions that need no n8n call.
    """
    from sqlalchemy import text

    from core.workspaces.models import WorkspaceMembership
    from modules.settings.public import module_is_enabled, resolve_n8n

    flag = settings.multi_workspace_enabled
    async with factory() as session:
        ids = list(
            (
                await session.scalars(
                    _page(
                        select(ConnectorProvisioning.source_id),
                        ConnectorProvisioning.source_id,
                        await _read_cursor(state, "connectors_transitions"),
                        or_(
                            ConnectorProvisioning.transition_phase.in_(
                                ("draining", "deactivating_old")
                            ),
                            and_(
                                ConnectorProvisioning.transition_phase == "activating_new",
                                ConnectorProvisioning.target_backend == "native",
                                ConnectorProvisioning.error_code == "backend_transition_pending",
                            ),
                        ),  # a failed activation waits for the owner
                    )
                )
            ).all()
        )
        await session.rollback()
    await _write_cursor(
        state, "connectors_transitions", ids[-1] if len(ids) >= _PASS_LIMIT else None
    )
    advanced = 0
    for source_id in ids:
        async with factory() as session:
            try:
                workspace_id = await session.scalar(
                    text("SELECT workspace_id FROM sources WHERE id = :id"), {"id": source_id}
                )
                row = await session.get(ConnectorProvisioning, source_id)
                owner = (
                    await session.scalar(
                        select(WorkspaceMembership).where(
                            WorkspaceMembership.workspace_id == workspace_id,
                            WorkspaceMembership.role == "owner",
                        )
                    )
                    if workspace_id is not None
                    else None
                )
                if owner is None or row is None:
                    await session.rollback()
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id,
                    actor_user_id=owner.user_id,
                    membership_revision=owner.revision,
                    source_id=source_id,
                    source_generation=row.source_generation,
                )
                await session.rollback()
                if not await module_is_enabled(
                    session, "connectors", scope=scope, multi_workspace_enabled=flag
                ):
                    await session.rollback()
                    continue
                fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=flag)
                n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
                await session.rollback()
                api = N8nApi(n8n.service_url, n8n.api_key, settings) if n8n.api_key else None
                await provisioning.advance_backend_transition(
                    session,
                    source_id,
                    api,
                    scope=scope,
                    multi_workspace_enabled=flag,
                    access_fence=fence,
                )
                advanced += 1
            except (DomainError, HTTPException) as exc:
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
    When the owner disabled n8n, create/update/activate/lookup work is skipped (left durable for
    re-enable) while credential deletes and workflow deactivations still run so cleanup finishes.
    Discovery is deployment-wide; each admitted operation resolves its own URL/key/kill switch.
    """
    settings = cast(Settings, ctx["settings"])
    factory = worker_session_factory(ctx)
    flag = settings.multi_workspace_enabled
    state = _cursor_ctx(ctx)
    completed = await dispatch_github_webhooks(ctx)
    completed += await _advance_transitions(factory, settings, state)
    credential_after = _read_slot_cursor(state, "connectors_credentials")
    async with factory() as session:
        credential_rows: list[Any] = list(
            (
                await session.execute(
                    _page(
                        select(
                            ConnectorManagedCredential.source_id,
                            ConnectorManagedCredential.slot,
                            ConnectorManagedCredential.operation_id,
                            ConnectorManagedCredential.operation_envelope["kind"].astext,
                        ),
                        ConnectorManagedCredential.source_id,
                        (credential_after or (None, None))[0],
                        ConnectorManagedCredential.operation_envelope["state"].astext == "prepared",
                        ConnectorManagedCredential.operation_envelope["kind"].astext == "delete",
                        ConnectorManagedCredential.operation_id.is_not(None),
                        order=(ConnectorManagedCredential.slot,),
                        slot_column=ConnectorManagedCredential.slot,
                        after_slot=(credential_after or (None, None))[1],
                    )
                )
            ).all()
        )
        workflow_ids = list(
            (
                await session.scalars(
                    _page(
                        select(ConnectorProvisioning.source_id),
                        ConnectorProvisioning.source_id,
                        await _read_cursor(state, "connectors_workflows"),
                        ConnectorProvisioning.workflow_operation["step"]["state"].astext
                        == "prepared",
                    )
                )
            ).all()
        )
        unknown_create_ids = list(
            (
                await session.scalars(
                    _page(
                        select(ConnectorProvisioning.source_id),
                        ConnectorProvisioning.source_id,
                        await _read_cursor(state, "connectors_unknown_create"),
                        ConnectorProvisioning.workflow_operation["step"]["state"].astext
                        == "unknown",
                        ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
                    )
                )
            ).all()
        )
        activation_ids = list(
            (
                await session.scalars(
                    _page(
                        select(ConnectorProvisioning.source_id),
                        ConnectorProvisioning.source_id,
                        await _read_cursor(state, "connectors_activation"),
                        ConnectorProvisioning.desired_enabled.is_(True),
                        ConnectorProvisioning.state == "provisioning",
                        ConnectorProvisioning.workflow_operation.is_(None),
                        ConnectorProvisioning.activation_intent.is_not(None),
                    )
                )
            ).all()
        )
        await session.rollback()
    _write_slot_cursor(
        state,
        "connectors_credentials",
        (credential_rows[-1][0], credential_rows[-1][1])
        if len(credential_rows) >= _PASS_LIMIT
        else None,
    )
    for name, found in (
        ("connectors_workflows", workflow_ids),
        ("connectors_unknown_create", unknown_create_ids),
        ("connectors_activation", activation_ids),
    ):
        await _write_cursor(state, name, found[-1] if len(found) >= _PASS_LIMIT else None)

    for source_id, slot, operation_id, kind in credential_rows:
        if operation_id is None or kind != "delete":
            continue
        async with factory() as session:
            try:
                completed += await _delete_credential(
                    session,
                    settings,
                    source_id,
                    slot,
                    operation_id,
                    multi_workspace_enabled=flag,
                )
            except (DomainError, HTTPException) as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise

    for source_id in workflow_ids:
        async with factory() as session:
            try:
                completed += await _drive_workflow(session, settings, source_id, flag=flag)
            except (DomainError, HTTPException) as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise

    for source_id in unknown_create_ids:
        try:
            completed += await _recover_unknown_create(factory, settings, source_id, flag=flag)
        except (DomainError, HTTPException) as exc:
            if exc.status_code not in _DENIED:
                raise

    for source_id in activation_ids:
        async with factory() as session:
            try:
                completed += await _resume_activation(session, source_id, settings)
            except (DomainError, HTTPException) as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
    return completed
