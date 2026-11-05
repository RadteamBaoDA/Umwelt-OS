"""Retained-effect locking, commit and journaled result recording for connector operations."""

import copy
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, DomainError
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.provisioning._common import (
    _ALL_CREDENTIAL_SLOTS,
    RetainedEffectAdmissionDenied,
    _retained_operation_matches,
)
from modules.connectors.provisioning._observation import (
    ConnectorObservation,
    _connector_observation,
    commit_connector_observation,
)
from modules.sources import public as sources


async def lock_retained_connector_effect(
    session: AsyncSession,
    source_id: UUID,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> "ConnectorObservation | None":
    """Capture effect observation by original admission->Source->provisioning->all slots.

    Enter before domain locks using the immutable pre-I/O operation/access snapshot.
    Later Source generation grants cleanup/reconciliation only; callbacks separately
    CAS exact owned effect. Caller retains locks through final commit; no network/commit.
    Original revoked access raises and is never replaced with a current principal.
    """
    if not _retained_operation_matches(
        original_operation,
        original_operation,
        source_id=source_id,
        scope=scope,
        access_fence=access_fence,
    ) or original_operation.get("retained_effect_result"):
        return None
    try:
        source = await sources.lock_connector_retained_effect_anchor(
            session,
            source_id,
            source_generation=cast(int, original_operation["source_generation"]),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    except (DomainError, HTTPException) as exc:
        if exc.status_code not in {401, 403, 404, 409}:
            raise
        raise RetainedEffectAdmissionDenied(status_code=exc.status_code, detail=exc.detail) from exc
    if source is None:
        return None
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    slots = list(
        await session.scalars(
            select(ConnectorManagedCredential)
            .where(
                ConnectorManagedCredential.source_id == source_id,
                ConnectorManagedCredential.slot.in_(_ALL_CREDENTIAL_SLOTS),
            )
            .order_by(ConnectorManagedCredential.slot)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return _connector_observation(
        source, row, {credential.slot: credential for credential in slots}, access_fence
    )


async def commit_retained_connector_effect(
    session: AsyncSession,
    before: "ConnectorObservation | None",
    *,
    source_id: UUID,
    original_operation: dict[str, object],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Commit exact effect settlement under held original locks, never publish stale success.

    Recheck original access and the metadata-only anchor nonlockingly. Current-generation
    observations retain normal replay; an advanced Source commits only retained effect
    state without inventing a new generation Scope or replay authority. Caller callbacks
    already proved operation/step/remote target CAS. No acquiring locks or network.
    """
    if before is None or before.access_fence != access_fence:
        raise ConflictError("Retained Connector observation unavailable")
    source = await sources.get_connector_retained_effect_anchor_in_uow(
        session,
        source_id,
        source_generation=cast(int, original_operation["source_generation"]),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    if source != before.fence:
        raise ConflictError("Retained Connector Source changed")
    if source.generation == original_operation["source_generation"]:
        await commit_connector_observation(
            session,
            before,
            operation_id=UUID(cast(str, original_operation["id"])),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    else:
        await session.flush()
        await session.commit()


def _retained_result_annotation(
    original_operation: dict[str, object],
    *,
    outcome: str,
    remote_id: str | None,
    error_code: str | None,
    workflow: bool,
) -> dict[str, object]:
    """Validate bounded secret-free transport disposition for one committed original effect.

    Finite outcome/error categories accept no provider bodies, bindings, secrets or raw
    exceptions. Exact-target outcomes retain that target; only a create may add a new ID.
    Confirmed cleanup means only this already-dispatched delete/deactivate was confirmed.
    """
    if outcome not in {"known_success", "known_rejection", "unknown", "not_sent"}:
        raise ValueError("Unsupported retained effect outcome")
    if error_code not in {
        None,
        "original_access_revoked",
        "n8n_credential_rejected",
        "credential_operation_outcome_unknown",
        "n8n_request_rejected",
        "n8n_outcome_unknown",
        "workflow_lookup_unverified",
        "original_effect_send_fenced",
        "credential_delete_target_missing",
        "credential_delete_rejected",
        "credential_delete_outcome_unknown",
    }:
        raise ValueError("Unsupported retained effect error")
    step = original_operation.get("step")
    kind = (
        step.get("kind") if workflow and isinstance(step, dict) else original_operation.get("kind")
    )
    target = (
        step.get("target")
        if workflow and isinstance(step, dict)
        else original_operation.get("target_id")
    )
    if kind not in (
        {"lookup", "create", "update", "activate", "deactivate"}
        if workflow
        else {"create", "update", "delete"}
    ):
        raise ValueError("Unsupported retained operation kind")
    if target is not None and (not isinstance(target, str) or not target or len(target) > 128):
        raise ValueError("Invalid retained remote target")
    if remote_id is not None and (
        not isinstance(remote_id, str) or not remote_id or len(remote_id) > 128
    ):
        raise ValueError("Invalid retained remote result ID")
    if kind in {"update", "activate", "deactivate", "delete"}:
        if not isinstance(target, str) or remote_id is not None and remote_id != target:
            raise ValueError("Retained remote result does not match original target")
        remote_id = target
    elif kind == "create":
        if target is not None or outcome == "known_success" and remote_id is None:
            raise ValueError("Known create requires its exact validated remote ID")
    # Lookup is read-only; its result may describe an existing remote liability,
    # but cannot authorize a next lookup/update/activation under the journal.
    cleanup_state = (
        "confirmed"
        if outcome == "known_success" and kind in {"delete", "deactivate"}
        else "unknown"
        if outcome == "unknown"
        else "pending"
    )
    return {
        "disposition": "retained_effect_access_revoked",
        "outcome": outcome,
        "remote_id": remote_id,
        "error_code": error_code,
        "cleanup_state": cleanup_state,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def _store_retained_result(
    envelope: dict[str, object],
    annotation: dict[str, object],
) -> tuple[Literal["stored", "duplicate", "conflict"], dict[str, object]]:
    """Apply one bounded monotonic annotation without changing the original dispatch barrier.

    Identical duplicates ignore timestamp. Unknown may refine to a known result; conflicting
    known IDs/outcomes remain untouched. No unbounded history, executable step or ready state.
    """
    current = envelope.get("retained_effect_result")
    comparable = {key: value for key, value in annotation.items() if key != "recorded_at"}
    if isinstance(current, dict):
        existing = {key: value for key, value in current.items() if key != "recorded_at"}
        if existing == comparable:
            return "duplicate", envelope
        if (
            current.get("disposition") != "retained_effect_access_revoked"
            or current.get("outcome") != "unknown"
            or annotation.get("outcome") not in {"known_success", "known_rejection"}
            or current.get("remote_id") is not None
            and current.get("remote_id") != annotation.get("remote_id")
        ):
            return "conflict", envelope
    elif current is not None:
        return "conflict", envelope
    changed = copy.deepcopy(envelope)
    changed["retained_effect_result"] = annotation
    if annotation.get("cleanup_state") != "confirmed":
        changed["cleanup_required"] = True
    return "stored", changed


async def record_retained_credential_result_in_uow(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    access_fence: AccessFence,
    outcome: str,
    remote_id: str | None = None,
    error_code: str | None = None,
) -> Literal["stored", "duplicate", "conflict", "missing"]:
    """Journal only one trusted already-entered credential transport in a fresh transaction.

    Requires no active user authority: original Scope/access are immutable dispatch lineage.
    After definitive admission denial caller fully rolls back, then invokes this internal
    transport continuation before any transaction starts. Lock provisioning then this slot;
    compare committed dispatched/unknown envelope plus row operation/revision/generation/type.
    Change only its bounded result/cleanup annotation, never binding/readiness/siblings.
    No Source/auth/core read, network, insert, replay or commit. Caller commits stored/duplicate
    in this dedicated transaction, rolls back conflict/missing, and exposes no result to a
    revoked HTTP subject. Missing rows are never reconstructed or reported as stored.
    """
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise ValueError("Retained result requires an empty fresh journal transaction")
    if slot not in _ALL_CREDENTIAL_SLOTS:
        raise ValueError("Unsupported retained credential slot")
    if not _retained_operation_matches(
        original_operation,
        original_operation,
        source_id=source_id,
        scope=scope,
        access_fence=access_fence,
    ):
        return "conflict"
    if original_operation.get("state") not in {"dispatched", "unknown"}:
        return "conflict"
    if (
        not isinstance(original_operation.get("dispatch_started_at"), str)
        or not original_operation["dispatch_started_at"]
    ):
        return "conflict"
    annotation = _retained_result_annotation(
        original_operation,
        outcome=outcome,
        remote_id=remote_id,
        error_code=error_code,
        workflow=False,
    )
    parent = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        return "missing"
    row = await session.scalar(
        select(ConnectorManagedCredential)
        .where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.slot == slot,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not isinstance(row.operation_envelope, dict):
        return "missing"
    envelope = row.operation_envelope
    if (
        not _retained_operation_matches(
            envelope,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or envelope.get("state") not in {"dispatched", "unknown"}
        or str(row.operation_id) != envelope.get("id")
        or row.source_generation != envelope.get("source_generation")
        or row.operation_revision != envelope.get("revision")
        or row.credential_type != envelope.get("credential_type")
        or envelope.get("kind") in {"update", "delete"}
        and row.credential_id != envelope.get("target_id")
    ):
        return "conflict"
    disposition, changed = _store_retained_result(envelope, annotation)
    if disposition == "stored":
        row.operation_envelope = changed
        await session.flush()
    return disposition


async def record_retained_workflow_result_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    original_operation: dict[str, object],
    scope: Scope,
    access_fence: AccessFence,
    outcome: str,
    remote_id: str | None = None,
    error_code: str | None = None,
) -> Literal["stored", "duplicate", "conflict", "missing"]:
    """Journal only exact workflow transport/previously admitted recovery in a fresh UoW.

    Internal trusted driver freezes original operation/step/request/target and access after
    committed dispatched/unknown barrier, including its pre-admitted unknown-create lookup.
    After definitive admission denial release all SQL before entry. Lock only provisioning;
    compare the full original envelope, changing only bounded result/cleanup annotations.
    No current Source/config/credentials, renewed admission, executable successor, binding,
    activation, network, insert, replay or commit. Dedicated caller commits stored/duplicate
    or rolls back conflict/missing; journal acceptance never means remote cleanup completed.
    """
    if session.in_transaction() or session.new or session.dirty or session.deleted:
        raise ValueError("Retained result requires an empty fresh journal transaction")
    if not _retained_operation_matches(
        original_operation,
        original_operation,
        source_id=source_id,
        scope=scope,
        access_fence=access_fence,
    ):
        return "conflict"
    original_step = original_operation.get("step")
    if not isinstance(original_step, dict) or original_step.get("state") not in {
        "dispatched",
        "unknown",
    }:
        return "conflict"
    if (
        not isinstance(original_step.get("dispatch_started_at"), str)
        or not original_step["dispatch_started_at"]
    ):
        return "conflict"
    annotation = _retained_result_annotation(
        original_operation,
        outcome=outcome,
        remote_id=remote_id,
        error_code=error_code,
        workflow=True,
    )
    row = await session.scalar(
        select(ConnectorProvisioning)
        .where(
            ConnectorProvisioning.source_id == source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not isinstance(row.workflow_operation, dict):
        return "missing"
    envelope = row.workflow_operation
    step = envelope.get("step")
    if (
        not _retained_operation_matches(
            envelope,
            original_operation,
            source_id=source_id,
            scope=scope,
            access_fence=access_fence,
        )
        or not isinstance(step, dict)
        or step.get("state") not in {"dispatched", "unknown"}
        or step.get("kind") in {"update", "activate", "deactivate"}
        and (
            step.get("target") != envelope.get("workflow_id")
            or step.get("target") != row.workflow_id
        )
    ):
        return "conflict"
    disposition, changed = _store_retained_result(envelope, annotation)
    if disposition == "stored":
        row.workflow_operation = changed
        await session.flush()
    return disposition
