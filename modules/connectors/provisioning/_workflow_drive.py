"""Workflow operation driver: remote lookup, send and retained-effect settlement."""

import asyncio
import copy
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorProvisioning,
)
from modules.connectors.provisioning._activation import prepare_workflow_step
from modules.connectors.provisioning._common import (
    RetainedEffectAdmissionDenied,
    _read_retained_connector_rows,
    _retained_operation_matches,
)
from modules.connectors.provisioning._retained import (
    commit_retained_connector_effect,
    lock_retained_connector_effect,
    record_retained_workflow_result_in_uow,
)
from modules.connectors.provisioning._workflow_steps import (
    acknowledge_workflow_step,
    claim_workflow_step,
    fail_workflow_step,
)


async def _settle_retained_workflow_after_io(
    session: AsyncSession, source_id: UUID, *,
    original_operation: dict[str, object], scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, outcome: str, remote_id: str | None = None,
    error_code: str | None = None, next_kind: str | None = None,
    request: dict[str, object] | None = None,
) -> tuple[bool, dict[str, object] | None]:
    """Settle one exact trusted workflow response and return its owned next envelope.

    Only original admission denial triggers rollback then a separate journal transaction.
    That path stores no provider body/request, commits stored/duplicate only, and raises the
    original permission response with internal journal_disposition; no network/publication
    follows it. Current/advanced-Source admitted callbacks preserve exact lineage and cleanup.
    """
    try:
        before = await lock_retained_connector_effect(
            session, source_id, original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_workflow_result_in_uow(
            session, source_id, original_operation=original_operation, scope=scope,
            access_fence=access_fence, outcome=outcome, remote_id=remote_id, error_code=error_code,
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    operation_id = UUID(str(original_operation["id"]))
    step = cast(dict[str, Any], original_operation["step"])
    step_id = str(step["id"])
    if outcome == "known_success" and step.get("kind") == "lookup":
        changed = await prepare_workflow_step(
            session, source_id, operation_id, step_id, str(next_kind), remote_id, request,
            original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    elif outcome == "known_success":
        changed = await acknowledge_workflow_step(
            session, source_id, operation_id, step_id, workflow_id=remote_id,
            original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    else:
        changed = await fail_workflow_step(
            session, source_id, operation_id, step_id, error_code or "n8n_outcome_unknown",
            unknown=outcome == "unknown", original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    if not changed:
        await session.rollback()
        return False, None
    next_operation = copy.deepcopy(await session.scalar(select(
        ConnectorProvisioning.workflow_operation,
    ).where(ConnectorProvisioning.source_id == source_id)))
    await commit_retained_connector_effect(
        session, before, source_id=source_id, original_operation=original_operation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    return True, next_operation if isinstance(next_operation, dict) else None


async def _workflow_send_allowed(
    session: AsyncSession, source_id: UUID, operation: dict[str, object], *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
    transport_entered: bool,
) -> bool:
    """Recheck exact original dispatched target; cleanup may use a later Source generation.

    Activation/update/create require current Source/config and no cleanup_required marker.
    Deactivation uses only the retained exact remote target under unchanged original access.
    Finally release SQL before provider I/O; uncertainty is never automatically replayed.
    Caller declares whether any call for this exact step already entered transport; denied
    initial sends journal not_sent, later denied calls conservatively journal unknown.
    """
    from modules.settings.public import module_is_enabled

    try:
        await lock_retained_connector_effect(
            session, source_id, original_operation=operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
        source, row, _slots = await _read_retained_connector_rows(
            session, source_id, original_operation=operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
        current = row.workflow_operation if row is not None else None
        step = operation.get("step")
        cleanup = isinstance(step, dict) and step.get("kind") == "deactivate"
        return bool(
            source is not None and row is not None and isinstance(step, dict) and current is not None
            and step.get("state") == "dispatched"
            and _retained_operation_matches(current, operation, source_id=source_id, scope=scope, access_fence=access_fence)
            and not current.get("retained_effect_result")
            and (cleanup and isinstance(step.get("target"), str)
                 and step.get("target") == operation.get("workflow_id") == row.workflow_id
                 or not cleanup and not current.get("cleanup_required")
                 and source.generation == row.source_generation == operation.get("source_generation")
                 and row.desired_revision == operation.get("revision")
                 and source.status == "active" and not source.local_only
                 and await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled))
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_workflow_result_in_uow(
            session, source_id, original_operation=operation, scope=scope,
            access_fence=access_fence, outcome="unknown" if transport_entered else "not_sent",
            error_code="original_access_revoked",
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    finally:
        await session.rollback()


async def _lookup_workflow_step(
    session: AsyncSession, source_id: UUID, api: Any, operation: dict[str, Any], body: object,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> tuple[str, str | None] | None:
    """Run a ``lookup`` step: find the workflow by name and choose the follow-up step.

    Returns ``("update", workflow_id)`` for exactly one verified match or ``("create", None)`` for
    none. Returns ``None`` after journaling ``original_effect_send_fenced`` when the second send
    fence (taken once transport is entered) denies the verification read. Several matches, a
    missing ID, an invalid request body or a mismatched identity raise ``ValueError``; the caller
    classifies it. Network calls hold no SQL lock beyond the caller's session state.
    """
    from modules.connectors.n8n import find_workflows_by_name, workflow_matches

    matches = await find_workflows_by_name(api, str(operation["workflow_name"]))
    if len(matches) > 1:
        raise ValueError("n8n has multiple workflows for this connector operation")
    if not matches:
        return "create", None
    workflow_id = matches[0].get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError("n8n workflow lookup response omitted its ID")
    if not isinstance(body, dict):
        raise ValueError("Prepared workflow request body is invalid")
    if not await _workflow_send_allowed(
        session, source_id, operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        transport_entered=True,
    ):
        await _settle_retained_workflow_after_io(
            session, source_id, original_operation=operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="not_sent", error_code="original_effect_send_fenced",
        )
        return None
    candidate = await api.get_workflow(workflow_id)
    if not workflow_matches(body, candidate):
        raise ValueError("n8n workflow lookup returned a mismatched identity")
    return "update", workflow_id


async def _send_workflow_step(api: Any, kind: str, target: object, body: object) -> Any:
    """Send one create/update/activate/deactivate step to n8n and return the resulting workflow ID.

    Only ``create`` yields a new ID; ``update`` echoes its target; activation toggles return
    ``None``. Raises ``ValueError`` for an invalid body or an unsupported step; transport errors
    propagate for the caller to classify.
    """
    if not isinstance(body, dict) and kind in {"create", "update"}:
        raise ValueError("Prepared workflow request body is invalid")
    if kind == "create":
        return await api.create_workflow(body)
    if kind == "update" and isinstance(target, str):
        await api.update_workflow(target, body)
        return target
    if kind == "activate" and isinstance(target, str):
        await api.set_active(target, True)
        return None
    if kind == "deactivate" and isinstance(target, str):
        await api.set_active(target, False)
        return None
    raise ValueError("Unsupported prepared workflow step")


async def _settle_failed_workflow_step(
    session: AsyncSession, source_id: UUID, operation: dict[str, Any], exc: Exception, kind: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Journal a finite transport failure of a workflow step as a rejection or an unknown outcome.

    A 4xx response other than 408, and any failed ``lookup``, is a known rejection (nothing was
    changed remotely); everything else is an unknown outcome that recovery must reconcile.
    """
    from httpx import HTTPStatusError

    response = exc.response if isinstance(exc, HTTPStatusError) else None
    known_rejection = (
        response is not None and 400 <= response.status_code < 500
        and response.status_code != 408
    )
    await _settle_retained_workflow_after_io(
        session, source_id, original_operation=operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        outcome="known_rejection" if known_rejection or kind == "lookup" else "unknown",
        error_code="n8n_request_rejected" if known_rejection else "n8n_outcome_unknown",
    )


async def drive_workflow_operation(
    session: AsyncSession, source_id: UUID, api: Any,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Drive at most four exact steps under immutable original envelope/access lineage.

    Caller captures a prepared envelope and AccessFence. Each claim/send/result keeps that
    identity and releases SQL before network. Advanced Source/config permits exact cleanup
    only; revoked access journals the transport and raises original permission loss.
    No dispatched/unknown replay, renewed epoch, upgraded Scope or publication after journal.
    """
    for _ in range(4):
        operation = await claim_workflow_step(
            session, source_id, original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, service_url=api.service_url,
        )
        if operation is None:
            await session.rollback()
            return False
        if not await _workflow_send_allowed(
            session, source_id, operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            transport_entered=False,
        ):
            await _settle_retained_workflow_after_io(
                session, source_id, original_operation=operation, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                outcome="not_sent", error_code="original_effect_send_fenced",
            )
            return False
        step = operation.get("step")
        if not isinstance(step, dict):
            return False
        kind = str(step["kind"])
        body = step.get("request")
        try:
            if kind == "lookup":
                lookup = await _lookup_workflow_step(
                    session, source_id, api, operation, body, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                )
                if lookup is None:
                    return False
                next_kind, next_target = lookup
                changed, next_operation = await _settle_retained_workflow_after_io(
                    session, source_id, original_operation=operation, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                    outcome="known_success", remote_id=next_target, next_kind=next_kind,
                    request=body if isinstance(body, dict) else {},
                )
                if not changed:
                    return False
                if next_operation is None:
                    return True
                original_operation = next_operation
                continue
            workflow_id = await _send_workflow_step(api, kind, step.get("target"), body)
        except asyncio.CancelledError:
            await _settle_retained_workflow_after_io(
                session, source_id, original_operation=operation, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                outcome="unknown", error_code="n8n_outcome_unknown",
            )
            raise
        except RetainedEffectAdmissionDenied:
            # A journaled permission-loss response is never a provider rejection.
            raise
        except Exception as exc:  # noqa: BLE001  # finite transport failure is retained for recovery
            await _settle_failed_workflow_step(
                session, source_id, operation, exc, kind, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            )
            return False
        changed, next_operation = await _settle_retained_workflow_after_io(
            session, source_id, original_operation=operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="known_success", remote_id=workflow_id,
        )
        if not changed:
            return False
        if next_operation is None:
            return True
        original_operation = next_operation
    return False
