import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import ReplayDraft, commit_with_replay, make_source_change
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from fastapi import HTTPException
from modules.connectors.backends import CURRENT_TEMPLATE_REVISION, backend_admits
from modules.connectors.models import (
    ConnectorCollectionRequest,
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    ConnectorSchedule,
    ConnectorWorldCredential,
    GithubOAuthGrant,
)
from modules.connectors.public import (
    NativeCredentialRevocationSnapshot, NativeCredentialSnapshot,
    _connector_access, _connector_actor, _read_scoped_source,
)
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

_ALL_CREDENTIAL_SLOTS = ("collector", "manual_trigger", "provider")


class RetainedEffectAdmissionDenied(HTTPException):
    """Identify definitive original admission denial at the retained Source boundary.

    Trusted drivers release the failed transaction before journal-only bookkeeping;
    database/programming/missing-operation failures never become this exception.
    journal_disposition is internal bookkeeping status, never an HTTP projection or
    evidence that remote cleanup completed. Normal permission-loss detail stays unchanged.
    """

    journal_disposition: Literal["stored", "duplicate", "conflict", "missing"] | None = None


async def _read_connector_rows(
    session: AsyncSession, source_id: UUID, slots: tuple[str, ...] = (), *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Freshly read owned rows without locks after scoped owner/Source admission."""
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None:
        return None, None, {}
    fence = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    credentials = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
        ConnectorManagedCredential.slot.in_(sorted(set(slots))),
    ).order_by(ConnectorManagedCredential.slot).execution_options(populate_existing=True))) if slots else []
    return fence, row, {credential.slot: credential for credential in credentials}


async def _lock_connector_rows(
    session: AsyncSession, source_id: UUID, slots: tuple[str, ...] = (), *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Lock only provisioning then sorted slots under already-held access and Source locks.

    Callers acquired admission/Source before entering and retain them through final commit.
    This is an owner-private continuation, not permission to skip upstream admission.
    """
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None:
        return None, None, {}
    fence = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    credentials = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
        ConnectorManagedCredential.slot.in_(sorted(set(slots))),
    ).order_by(ConnectorManagedCredential.slot).with_for_update().execution_options(populate_existing=True))) if slots else []
    return fence, row, {credential.slot: credential for credential in credentials}


def _operation_identity(scope: Scope, access_fence: AccessFence) -> dict[str, object]:
    """Capture the admitted principal/config epoch in durable operation JSON without secrets."""
    return {
        "workspace_id": str(scope.workspace_id), "actor_user_id": _connector_actor(scope),
        "membership_revision": scope.membership_revision,
        "workspace_configuration_revision": access_fence.configuration_revision,
    }


async def _operation_matches(
    session: AsyncSession, envelope: object, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Reject unbound/stale principals and revoked-effect journals for ordinary execution.

    Journal disposition never regains send/publication authority after regrant/re-enable.
    Original scoped access/config admission is checked without upstream lock acquisition.
    """
    fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return isinstance(envelope, dict) and not envelope.get("retained_effect_result") and all(
        type(envelope.get(key)) is type(value) and envelope.get(key) == value
        for key, value in _operation_identity(scope, fence).items()
    )


def _retained_operation_matches(
    current: object, original_operation: dict[str, object], *, source_id: UUID,
    scope: Scope, access_fence: AccessFence,
) -> bool:
    """Compare original durable principal/config/operation/step/target without renewal.

    Lifecycle may only mark cleanup_required/error while the exact in-flight envelope
    remains retained. All other keys, including dispatch state/time, request, ciphertext,
    remote target, Source generation and revision, must match the captured snapshot.
    This pure CAS proves lineage only; Source owner separately admits original access.
    """
    if not isinstance(current, dict) or not isinstance(original_operation, dict):
        return False
    if (access_fence.workspace_id != scope.workspace_id
            or access_fence.user_id != _connector_actor(scope)
            or access_fence.membership_revision != scope.membership_revision):
        return False
    if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
        scope.source_id != source_id
        or scope.source_generation != original_operation.get("source_generation")
    ):
        return False
    identity = _operation_identity(scope, access_fence)
    if any(type(current.get(key)) is not type(value) or current.get(key) != value
           or type(original_operation.get(key)) is not type(value)
           or original_operation.get(key) != value for key, value in identity.items()):
        return False
    if (type(original_operation.get("source_generation")) is not int
            or original_operation["source_generation"] <= 0
            or type(original_operation.get("revision")) is not int
            or original_operation["revision"] <= 0
            or not isinstance(original_operation.get("id"), str)):
        return False
    step = original_operation.get("step")
    if isinstance(step, dict):
        if (original_operation.get("kind") not in {"enable", "deactivate"}
                or step.get("kind") not in {"lookup", "create", "update", "activate", "deactivate"}
                or original_operation.get("kind") == "deactivate" and step.get("kind") != "deactivate"):
            return False
    elif original_operation.get("kind") not in {"create", "update", "delete"}:
        return False
    # JSON preserves nested value types and absent-vs-null fields in the exact request.
    excluded = {"cleanup_required", "error", "retained_effect_result"}
    try:
        return json.dumps({key: value for key, value in current.items() if key not in excluded}, sort_keys=True) == json.dumps(
            {key: value for key, value in original_operation.items() if key not in excluded}, sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


async def _read_retained_connector_rows(
    session: AsyncSession, source_id: UUID, *, original_operation: dict[str, object],
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Reread exact effect rows without locks under held original admission/Source/slots.

    Source returns lifecycle metadata only, allowing this same anchor's later generation.
    Caller separately CASes immutable operation/step/target before mutation. No Source
    current configuration or renewed epoch is exposed; no commit or external I/O.
    """
    if not _retained_operation_matches(
        original_operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence,
    ) or original_operation.get("retained_effect_result"):
        return None, None, {}
    source = await sources.get_connector_retained_effect_anchor_in_uow(
        session, source_id, source_generation=original_operation["source_generation"],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if source is None:
        return None, None, {}
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    slots = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
        ConnectorManagedCredential.slot.in_(_ALL_CREDENTIAL_SLOTS),
    ).order_by(ConnectorManagedCredential.slot).execution_options(populate_existing=True)))
    return source, row, {credential.slot: credential for credential in slots}


async def lock_retained_connector_effect(
    session: AsyncSession, source_id: UUID, *, original_operation: dict[str, object],
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> "ConnectorObservation | None":
    """Capture effect observation by original admission->Source->provisioning->all slots.

    Enter before domain locks using the immutable pre-I/O operation/access snapshot.
    Later Source generation grants cleanup/reconciliation only; callbacks separately
    CAS exact owned effect. Caller retains locks through final commit; no network/commit.
    Original revoked access raises and is never replaced with a current principal.
    """
    if not _retained_operation_matches(
        original_operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence,
    ) or original_operation.get("retained_effect_result"):
        return None
    try:
        source = await sources.lock_connector_retained_effect_anchor(
            session, source_id, source_generation=original_operation["source_generation"],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    except HTTPException as exc:
        if exc.status_code not in {401, 403, 404, 409}:
            raise
        raise RetainedEffectAdmissionDenied(status_code=exc.status_code, detail=exc.detail) from exc
    if source is None:
        return None
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    slots = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
        ConnectorManagedCredential.slot.in_(_ALL_CREDENTIAL_SLOTS),
    ).order_by(ConnectorManagedCredential.slot).with_for_update().execution_options(populate_existing=True)))
    return _connector_observation(source, row, {credential.slot: credential for credential in slots}, access_fence)


async def commit_retained_connector_effect(
    session: AsyncSession, before: "ConnectorObservation | None", *, source_id: UUID,
    original_operation: dict[str, object], scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Commit exact effect settlement under held original locks, never publish stale success.

    Recheck original access and the metadata-only anchor nonlockingly. Current-generation
    observations retain normal replay; an advanced Source commits only retained effect
    state without inventing a new generation Scope or replay authority. Caller callbacks
    already proved operation/step/remote target CAS. No acquiring locks or network.
    """
    if before is None or before.access_fence != access_fence:
        raise HTTPException(status_code=409, detail="Retained Connector observation unavailable")
    source = await sources.get_connector_retained_effect_anchor_in_uow(
        session, source_id, source_generation=original_operation["source_generation"],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if source != before.fence:
        raise HTTPException(status_code=409, detail="Retained Connector Source changed")
    if source.generation == original_operation["source_generation"]:
        await commit_connector_observation(
            session, before, operation_id=UUID(original_operation["id"]),
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    else:
        await session.flush()
        await session.commit()


def _retained_result_annotation(
    original_operation: dict[str, object], *, outcome: str, remote_id: str | None,
    error_code: str | None, workflow: bool,
) -> dict[str, object]:
    """Validate bounded secret-free transport disposition for one committed original effect.

    Finite outcome/error categories accept no provider bodies, bindings, secrets or raw
    exceptions. Exact-target outcomes retain that target; only a create may add a new ID.
    Confirmed cleanup means only this already-dispatched delete/deactivate was confirmed.
    """
    if outcome not in {"known_success", "known_rejection", "unknown", "not_sent"}:
        raise ValueError("Unsupported retained effect outcome")
    if error_code not in {
        None, "original_access_revoked", "n8n_credential_rejected",
        "credential_operation_outcome_unknown", "n8n_request_rejected",
        "n8n_outcome_unknown", "workflow_lookup_unverified", "original_effect_send_fenced",
        "credential_delete_target_missing", "credential_delete_rejected", "credential_delete_outcome_unknown",
    }:
        raise ValueError("Unsupported retained effect error")
    step = original_operation.get("step")
    kind = step.get("kind") if workflow and isinstance(step, dict) else original_operation.get("kind")
    target = step.get("target") if workflow and isinstance(step, dict) else original_operation.get("target_id")
    if kind not in ({"lookup", "create", "update", "activate", "deactivate"} if workflow else {"create", "update", "delete"}):
        raise ValueError("Unsupported retained operation kind")
    if target is not None and (not isinstance(target, str) or not target or len(target) > 128):
        raise ValueError("Invalid retained remote target")
    if remote_id is not None and (not isinstance(remote_id, str) or not remote_id or len(remote_id) > 128):
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
        "confirmed" if outcome == "known_success" and kind in {"delete", "deactivate"}
        else "unknown" if outcome == "unknown" else "pending"
    )
    return {
        "disposition": "retained_effect_access_revoked", "outcome": outcome,
        "remote_id": remote_id, "error_code": error_code,
        "cleanup_state": cleanup_state, "recorded_at": datetime.now(UTC).isoformat(),
    }


def _store_retained_result(
    envelope: dict[str, object], annotation: dict[str, object],
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
            or current.get("remote_id") is not None and current.get("remote_id") != annotation.get("remote_id")
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
    session: AsyncSession, source_id: UUID, slot: str, *,
    original_operation: dict[str, object], scope: Scope, access_fence: AccessFence,
    outcome: str, remote_id: str | None = None, error_code: str | None = None,
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
        original_operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence,
    ):
        return "conflict"
    if original_operation.get("state") not in {"dispatched", "unknown"}:
        return "conflict"
    if not isinstance(original_operation.get("dispatch_started_at"), str) or not original_operation["dispatch_started_at"]:
        return "conflict"
    annotation = _retained_result_annotation(
        original_operation, outcome=outcome, remote_id=remote_id, error_code=error_code, workflow=False,
    )
    parent = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if parent is None:
        return "missing"
    row = await session.scalar(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id, ConnectorManagedCredential.slot == slot,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None or not isinstance(row.operation_envelope, dict):
        return "missing"
    envelope = row.operation_envelope
    if (
        not _retained_operation_matches(envelope, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or envelope.get("state") not in {"dispatched", "unknown"}
        or str(row.operation_id) != envelope.get("id")
        or row.source_generation != envelope.get("source_generation")
        or row.operation_revision != envelope.get("revision")
        or row.credential_type != envelope.get("credential_type")
        or envelope.get("kind") in {"update", "delete"} and row.credential_id != envelope.get("target_id")
    ):
        return "conflict"
    disposition, changed = _store_retained_result(envelope, annotation)
    if disposition == "stored":
        row.operation_envelope = changed
        await session.flush()
    return disposition


async def record_retained_workflow_result_in_uow(
    session: AsyncSession, source_id: UUID, *, original_operation: dict[str, object],
    scope: Scope, access_fence: AccessFence, outcome: str,
    remote_id: str | None = None, error_code: str | None = None,
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
        original_operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence,
    ):
        return "conflict"
    original_step = original_operation.get("step")
    if not isinstance(original_step, dict) or original_step.get("state") not in {"dispatched", "unknown"}:
        return "conflict"
    if not isinstance(original_step.get("dispatch_started_at"), str) or not original_step["dispatch_started_at"]:
        return "conflict"
    annotation = _retained_result_annotation(
        original_operation, outcome=outcome, remote_id=remote_id, error_code=error_code, workflow=True,
    )
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None or not isinstance(row.workflow_operation, dict):
        return "missing"
    envelope = row.workflow_operation
    step = envelope.get("step")
    if (
        not _retained_operation_matches(envelope, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or not isinstance(step, dict) or step.get("state") not in {"dispatched", "unknown"}
        or step.get("kind") in {"update", "activate", "deactivate"}
        and (step.get("target") != envelope.get("workflow_id") or step.get("target") != row.workflow_id)
    ):
        return "conflict"
    disposition, changed = _store_retained_result(envelope, annotation)
    if disposition == "stored":
        row.workflow_operation = changed
        await session.flush()
    return disposition


async def _settle_retained_credential_after_io(
    session: AsyncSession, source_id: UUID, slot: str, *,
    original_operation: dict[str, object], scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, outcome: str, remote_id: str | None = None,
    binding: dict[str, object] | None = None, error_code: str | None = None,
) -> bool:
    """Settle one trusted credential transport or durably journal definitive access loss.

    Ordinary admitted path acquires original parents then exact callback and replay/commit.
    Only RetainedEffectAdmissionDenied releases all locks before fresh journal-only write.
    Journal stores no binding or secret; after commit rethrow original permission loss with
    internal journal_disposition. Missing/conflict rolls back and is never progress success.
    """
    try:
        before = await lock_retained_connector_effect(
            session, source_id, original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_credential_result_in_uow(
            session, source_id, slot, original_operation=original_operation, scope=scope,
            access_fence=access_fence, outcome=outcome, remote_id=remote_id, error_code=error_code,
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    operation_id = UUID(str(original_operation["id"]))
    if outcome == "known_success":
        if original_operation.get("kind") == "delete":
            changed = await acknowledge_credential_delete(
                session, source_id, slot, operation_id, str(original_operation["target_id"]),
                original_operation=original_operation, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            )
        else:
            changed = await complete_credential_operation(
                session, source_id, slot, operation_id, credential_id=remote_id,
                binding=binding or {}, original_operation=original_operation, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            )
    else:
        changed = await fail_credential_operation(
            session, source_id, slot, operation_id, error_code or "credential_operation_outcome_unknown",
            unknown=outcome == "unknown", original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    if changed:
        await commit_retained_connector_effect(
            session, before, source_id=source_id, original_operation=original_operation,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    else:
        await session.rollback()
    return changed


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
    step = original_operation["step"]
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


async def _credential_send_allowed(
    session: AsyncSession, source_id: UUID, slot: str, envelope: dict[str, object], *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> bool:
    """Freshly admit exact dispatched credential/Source/config before send and release SQL.

    Retained envelope principal is compared to original caller scope; no actor/revision is
    rebuilt from current credentials. Denial preserves the durable dispatch barrier for
    reconciliation. Cleanup sends may dispose known remote effects on paused/local Source.
    """
    from modules.settings.public import module_is_enabled

    try:
        await lock_retained_connector_effect(
            session, source_id, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
        source, row, slots = await _read_retained_connector_rows(
            session, source_id, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
        credential = slots.get(slot)
        current = credential.operation_envelope if credential is not None else None
        cleanup = envelope.get("kind") == "delete"
        return bool(
            source is not None and row is not None and credential is not None
            and _retained_operation_matches(current, envelope, source_id=source_id, scope=scope, access_fence=access_fence)
            and not current.get("retained_effect_result")
            and current == envelope and envelope.get("state") == "dispatched"
            and credential.operation_id is not None and str(credential.operation_id) == envelope.get("id")
            and credential.operation_revision == envelope.get("revision")
            and credential.source_generation == envelope.get("source_generation")
            and credential.credential_type == envelope.get("credential_type")
            and (cleanup and current.get("target_id") == credential.credential_id
                 or not cleanup and source.generation == row.source_generation == envelope.get("source_generation")
                 and row.desired_revision == envelope.get("revision")
                 and (envelope.get("kind") == "create" and envelope.get("target_id") is None
                      or envelope.get("kind") == "update" and envelope.get("target_id") == credential.credential_id)
                 and source.status == "active" and not source.local_only
                 and await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled))
        )
    except RetainedEffectAdmissionDenied as denied:
        await session.rollback()
        disposition = await record_retained_credential_result_in_uow(
            session, source_id, slot, original_operation=envelope, scope=scope,
            access_fence=access_fence, outcome="not_sent", error_code="original_access_revoked",
        )
        if disposition in {"stored", "duplicate"}:
            await session.commit()
        else:
            await session.rollback()
        denied.journal_disposition = disposition
        raise
    finally:
        await session.rollback()


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
            source is not None and row is not None and isinstance(step, dict)
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


async def get_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> NativeCredentialSnapshot | None:
    """Lock only the native row under caller-held access/Source/provisioning collection proof.

    Fresh Source/provisioning reads acquire no earlier locks. Return a private detached
    snapshot only for exact active generation/revision; caller retains or releases its UoW.
    """
    source, row, _slots = await _read_connector_rows(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        source is None or source.status != "active" or row is None or source.generation != source_generation
        or row.desired_revision != connector_revision or row.source_generation != source_generation
    ):
        return None
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    if (
        native.source_generation != source_generation
        or native.configuration_revision != connector_revision
    ):
        return None
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return NativeCredentialSnapshot(
        access_fence=access_fence,
        workspace_id=source.workspace_id, source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def get_retained_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> NativeCredentialSnapshot | None:
    """Return a retained binding only under the active owner's requested source/revision fence.

    Source, provisioning, managed slots, and native row are locked in the normal
    connector order before an owner may decrypt or remotely revalidate a token.
    A missing native row returns None; a stale source or revision raises ValueError.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.status != "active" or source.generation != source_generation:
        raise ValueError("Connector credential fence is stale")
    if row is None:
        if connector_revision == 0:
            return None
        raise ValueError("Connector credential fence is stale")
    if row.source_generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    if row.state == "disabled" and row.error_code == "deactivation_pending":
        raise ValueError("Connector deactivation is pending")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return NativeCredentialSnapshot(
        access_fence=access_fence,
        workspace_id=source.workspace_id, source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def get_native_credential_revocation_snapshot(
    session: AsyncSession, source_id: UUID, *, source_generation: int, connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> NativeCredentialRevocationSnapshot:
    """Acquire original admission/Source/provisioning/sorted slots then paused native capture.

    Enter without domain locks, retaining the initial browser/job AccessFence. Removal alone
    requires exact paused Telegram Source G and disabled provisioning G/desired R with no
    deactivation pending. Unavailable/stale lifecycle/revision or malformed provider row
    raises ValueError; typed admission failures propagate. True native absence yields None
    operation only after valid parents; an older stored native G/R yields its actual UUID.
    Detach original access/current paused fence/requested R without cipher/bot/readiness or
    provider authority. No mutation/commit/I/O; caller detaches other data before release and
    later saves/revokes with this original operation-or-absence CAS, never a fresh rescue.
    """
    source, row, _slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
    )
    source_view = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source is None or source.id != source_id or source.workspace_id != scope.workspace_id
            or source.status != "paused" or source.generation != source_generation
            or source_view is None or source_view.provider != "telegram"
            or row is None or row.source_id != source_id or row.source_generation != source_generation
            or row.desired_revision != connector_revision or row.desired_enabled
            or row.state != "disabled" or row.error_code == "deactivation_pending"):
        raise ValueError("Paused native credential removal fence is stale")
    native = await session.scalar(select(ConnectorNativeCredential).where(
        ConnectorNativeCredential.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if native is not None and (
        native.source_id != source_id or native.provider != "telegram"
        or not isinstance(native.operation_id, UUID)
        or native.source_generation > source_generation or native.configuration_revision > connector_revision
    ):
        raise ValueError("Native credential removal identity is malformed")
    current_access = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    return NativeCredentialRevocationSnapshot(
        access_fence=current_access, source_fence=source, connector_revision=connector_revision,
        operation_id=native.operation_id if native is not None else None,
    )


async def save_native_credential(
    session: AsyncSession,
    *,
    source_id: UUID,
    operation_id: UUID,
    source_generation: int,
    connector_revision: int,
    encrypted_token: str,
    token_fingerprint: str,
    verified_bot_id: str,
    validated_at: datetime,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Acquire original access/Source/config and native-operation CAS before saving verified token.

    Caller supplies original AccessFence and prior native operation (None for observed absence)
    retained across verification I/O. Current row must still match; flush only, caller commits.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if (
        source is None or row is None or source.generation != source_generation
        or row.source_generation != source_generation or row.desired_revision != connector_revision
    ):
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (native.operation_id if native is not None else None) != expected_native_operation_id:
        raise HTTPException(status_code=409, detail="Native credential operation changed")
    if native is not None and (
        native.bound_bot_id not in (None, verified_bot_id)
        or native.verified_bot_id not in (None, verified_bot_id)
    ):
        raise ValueError("Telegram bot identity cannot change for an existing source")
    if native is None:
        native = ConnectorNativeCredential(source_id=source_id, operation_id=operation_id)
        session.add(native)
    native.provider = "telegram"
    native.operation_id = operation_id
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.encrypted_token = encrypted_token
    native.token_fingerprint = token_fingerprint
    native.verified_bot_id = verified_bot_id
    # Keep historical identity separate from the unique live reservation.
    native.bound_bot_id = verified_bot_id
    native.state = "ready"
    native.validated_at = validated_at
    native.error_code = None
    row.credential_revision += 1
    await session.flush()


async def revoke_native_credential(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    release_bot_reservation: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Acquire original admission/Source/provisioning/slots before captured native revoke.

    Caller supplies the retained AccessFence/native operation; enter before domain locks.
    Clear ciphertext and optional live reservation while preserving historical bot binding.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    return await revoke_native_credential_in_uow(
        session, source_id, source_generation=source_generation, connector_revision=connector_revision,
        release_bot_reservation=release_bot_reservation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        expected_native_operation_id=expected_native_operation_id,
    )


async def revoke_native_credential_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    release_bot_reservation: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Revoke captured native operation under held admission/Source/provisioning/slot locks.

    Freshly compare original AccessFence and Source/config; lock only the native row and
    require original operation CAS. Clear ciphertext/reservation, preserve historical bot;
    flush only, caller owns commit. Do not enter after a native/grant/ingestion lock.
    """
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source, row, _slots = await _read_connector_rows(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or row is None or source.generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (native.operation_id if native is not None else None) != expected_native_operation_id:
        raise HTTPException(status_code=409, detail="Native credential operation changed")
    if native is None:
        return
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.operation_id = uuid4()
    native.encrypted_token = None
    native.token_fingerprint = None
    native.validated_at = None
    native.state = "revoked"
    native.error_code = None
    if release_bot_reservation:
        # The unique active reservation is reusable; the source's bot binding is immutable.
        native.verified_bot_id = None
    await session.flush()


@dataclass(frozen=True)
class ConnectorObservation:
    """Capture owner-visible connector state while omitting credential material."""
    fence: SourceFence
    access_fence: AccessFence
    desired_revision: int
    applied_revision: int
    state: str
    error_code: str | None
    credential_recovery: str
    desired_enabled: bool
    credential_presence: tuple[tuple[str, bool], ...]
    header_auth_configured: bool


def _connector_observation(
    source: SourceFence | None,
    row: ConnectorProvisioning | None,
    slots: dict[str, ConnectorManagedCredential],
    access_fence: AccessFence,
) -> ConnectorObservation | None:
    """Project locked connector rows into a redacted observable state snapshot."""
    if source is None:
        return None
    if row is None:
        return ConnectorObservation(
            fence=source, access_fence=access_fence, desired_revision=0, applied_revision=0,
            state="saved_not_active", error_code=None, credential_recovery="supported",
            desired_enabled=False, credential_presence=(), header_auth_configured=False,
        )
    unresolved = any(
        credential.state in {"dispatching", "reconciliation_required", "delete_pending"}
        for credential in slots.values()
    )
    state = row.state
    error_code = row.error_code
    if unresolved:
        error_code = "credential_operation_pending"
        if state != "disabled":
            state = "reconciliation_required"
    desired_configuration = row.desired_configuration
    return ConnectorObservation(
        fence=source, access_fence=access_fence,
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        state=state,
        error_code=error_code,
        credential_recovery="unsupported_operation" if unresolved else "supported",
        desired_enabled=row.desired_enabled,
        credential_presence=tuple(
            (slot, bool(slots.get(slot) and slots[slot].credential_id))
            for slot in _ALL_CREDENTIAL_SLOTS
        ),
        header_auth_configured=bool(
            isinstance(desired_configuration, dict)
            and desired_configuration.get("auth_method") == "http_header"
        ),
    )


async def capture_connector_observation(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorObservation | None:
    """Read a coherent source/provisioning/credential snapshot under lock order."""
    source, row, slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return _connector_observation(source, row, slots, access_fence)


async def commit_connector_observation(
    session: AsyncSession,
    before: ConnectorObservation | None,
    *,
    operation_id: UUID | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Commit connector state and publish a source event when its safe view changed.

    Always flushes and commits the caller's session, even when the redacted
    snapshot is unchanged and no realtime event is emitted. ``operation_id``
    correlates an emitted source change with its durable provider operation.
    With ``before=None`` there is no comparable snapshot and no event draft.
    """
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=before.access_fence if before is not None else None)
    if before is not None and await sources.get_source_fence(session, before.fence.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) != before.fence:
        raise HTTPException(status_code=409, detail="Connector observation Source changed")
    await session.flush()
    drafts: list[ReplayDraft] = []
    if before is not None:
        result = await session.execute(
            select(
                ConnectorProvisioning.desired_revision,
                ConnectorProvisioning.applied_revision,
                ConnectorProvisioning.state,
                ConnectorProvisioning.error_code,
                ConnectorProvisioning.desired_enabled,
                ConnectorProvisioning.desired_configuration,
            ).where(ConnectorProvisioning.source_id == before.fence.id)
        )
        row = result.one_or_none()
        credential_rows = list((await session.execute(
            select(
                ConnectorManagedCredential.slot,
                ConnectorManagedCredential.credential_id.is_not(None),
                ConnectorManagedCredential.state,
            ).where(ConnectorManagedCredential.source_id == before.fence.id)
        )).all())
        if row is None:
            after = ConnectorObservation(
                fence=before.fence, access_fence=access_fence, desired_revision=0, applied_revision=0,
                state="saved_not_active", error_code=None, credential_recovery="supported",
                desired_enabled=False, credential_presence=(), header_auth_configured=False,
            )
        else:
            values = {slot: (present, state) for slot, present, state in credential_rows}
            unresolved = any(state in {"dispatching", "reconciliation_required", "delete_pending"}
                             for _present, state in values.values())
            state = row.state
            error_code = row.error_code
            if unresolved:
                error_code = "credential_operation_pending"
                if state != "disabled":
                    state = "reconciliation_required"
            desired_configuration = row.desired_configuration
            after = ConnectorObservation(
                fence=before.fence, access_fence=access_fence,
                desired_revision=row.desired_revision,
                applied_revision=row.applied_revision,
                state=state,
                error_code=error_code,
                credential_recovery="unsupported_operation" if unresolved else "supported",
                desired_enabled=row.desired_enabled,
                credential_presence=tuple(
                    (slot, bool(values.get(slot, (False, "queued"))[0]))
                    for slot in _ALL_CREDENTIAL_SLOTS
                ),
                header_auth_configured=bool(
                    isinstance(desired_configuration, dict)
                    and desired_configuration.get("auth_method") == "http_header"
                ),
            )
        if after != before:
            drafts.append(make_source_change(
                before.fence.id,
                before.fence.generation,
                before.fence.status,
                connector_state=after.state,
                operation_id=operation_id, scope=scope,
            ))
    await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)


async def lock_connector(
    session: AsyncSession,
    source_id: UUID,
    slots: tuple[str, ...] = (),
    *, scope: Scope, multi_workspace_enabled: bool, expected_access_fence: AccessFence | None = None,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Lock source, provisioning, then credential slots in the one supported order."""
    _connector_actor(scope)
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("The actual configured workspace flag is required")
    source = await sources.lock_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected_access_fence)
    if source is None:
        return None, None, {}
    return await _lock_connector_rows(
        session, source_id, slots, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def get_managed_credential(
    session: AsyncSession, source_id: UUID, slot: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorManagedCredential | None:
    """Read one credential slot without acquiring the provisioning lock chain."""
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return await session.scalar(
        select(ConnectorManagedCredential)
        .where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.slot == slot,
        )
        .execution_options(populate_existing=True)
    )


async def activation_status(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Read a session-bound provisioning ORM row as a current-state hint.

    Returns None when no row exists. The row remains managed by the supplied
    session; callers must not treat it as a detached DTO or use it after that
    session closes. This lookup refreshes the identity-map row but does not
    acquire the connector provisioning lock.
    """
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source_id)
        .execution_options(populate_existing=True)
    )


def _step(
    kind: str,
    target: str | None,
    request: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a durable prepared step with a unique ID and empty history."""
    return {
        "id": str(uuid4()),
        "kind": kind,
        "target": target,
        "request": copy.deepcopy(request or {}),
        "state": "prepared",
        "dispatch_started_at": None,
        "history": [],
    }


def new_workflow_operation(
    *,
    operation_id: UUID,
    kind: str,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_id: str | None,
    workflow_name: str,
    body: dict[str, object] | None,
    activation_id: UUID | None = None,
    scope: Scope, access_fence: AccessFence,
) -> dict[str, object]:
    """Create a workflow operation envelope and its first prepared step."""
    step_kind = (
        "update" if kind == "enable" and workflow_id
        else "lookup" if kind == "enable"
        else "deactivate"
    )
    return {
        **_operation_identity(scope, access_fence),
        "id": str(operation_id),
        "kind": kind,
        "source_generation": source_generation,
        "revision": revision,
        "configuration": copy.deepcopy(configuration),
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "activation_id": str(activation_id) if activation_id else None,
        "phase": step_kind,
        "step": _step(step_kind, workflow_id, body),
        "cleanup_required": False,
        "error": None,
    }


def _new_deactivation(row: ConnectorProvisioning, generation: int, *, scope: Scope, access_fence: AccessFence) -> dict[str, Any] | None:
    """Build cleanup work for the known workflow, or None when no workflow exists."""
    if not row.workflow_id:
        return None
    operation_id = uuid4()
    return new_workflow_operation(
        operation_id=operation_id,
        kind="deactivate",
        source_generation=generation,
        revision=row.desired_revision,
        configuration={},
        workflow_id=row.workflow_id,
        workflow_name=row.workflow_name or f"BBD-OS connector {row.source_id}",
        body=None, scope=scope, access_fence=access_fence,
    )


def _required_credentials_match(
    required: dict[str, dict[str, object]],
    slots: dict[str, ConnectorManagedCredential],
) -> bool:
    """Require ready credential IDs, bindings, and any succeeded operation identities."""
    for slot, value in required.items():
        if not isinstance(value, dict):
            return False
        credential = slots.get(slot)
        if (
            credential is None or credential.state != "ready" or not credential.credential_id
            or credential.resolved_binding != value.get("binding")
        ):
            return False
        expected_id = value.get("credential_id")
        if expected_id is not None and credential.credential_id != expected_id:
            return False
        expected_operation = value.get("operation_id")
        if expected_operation is not None:
            envelope = credential.operation_envelope
            if (
                not isinstance(envelope, dict)
                or envelope.get("id") != expected_operation
                or envelope.get("state") != "succeeded"
            ):
                return False
    return True


async def save_desired(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    expected_revision: int,
    configuration: dict[str, object],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Acquire admission/Source then desired-state rows; caller enters before domain locks."""
    source = await sources.lock_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        return None
    return await _save_desired_in_uow(
        session, source_id, source_generation, expected_revision, configuration,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def _save_desired_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    expected_revision: int,
    configuration: dict[str, object],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Continue held admission/Source into provisioning/sorted slots then revision-fenced mutation.

    Source setter holds only earlier parents. No earlier acquisition or commit; share the
    ordinary save_desired wrapper's complete interrupted activation/workflow cleanup body.
    """
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source, row, slots = await _lock_connector_rows(
        session, source_id, ("collector", "manual_trigger", "provider"),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.generation != source_generation:
        return None
    if row is None:
        if expected_revision != 0:
            return None
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=source_generation,
            desired_revision=1,
            desired_configuration=copy.deepcopy(configuration),
            state="saved_not_active",
            desired_enabled=False,
        )
        session.add(row)
        await session.flush()
        return row
    if row.desired_revision != expected_revision:
        return None

    prior_enabled = row.desired_enabled or row.state == "active"
    row.source_generation = source_generation
    row.desired_revision += 1
    world_credential = await session.get(ConnectorWorldCredential, source_id, with_for_update=True)
    if world_credential is not None:
        await session.delete(world_credential)
    row.desired_configuration = copy.deepcopy(configuration)
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = None
    activation = copy.deepcopy(row.activation_intent)
    if isinstance(activation, dict):
        required = activation.get("required_credentials")
        unresolved = False
        if isinstance(required, dict):
            for slot_name in required:
                credential = slots.get(str(slot_name))
                envelope = credential.operation_envelope if credential is not None else None
                if (
                    isinstance(envelope, dict)
                    and envelope.get("activation_id") == activation.get("id")
                ):
                    if envelope.get("state") == "prepared":
                        assert credential is not None
                        credential.operation_id = None
                        credential.operation_envelope = None
                        credential.state = "ready" if credential.credential_id else "queued"
                    elif envelope.get("state") in {"dispatched", "unknown"}:
                        unresolved = True
        if unresolved:
            activation["state"] = "stale_unresolved"
            row.activation_intent = activation
            row.state = "reconciliation_required"
            row.error_code = "activation_outcome_pending"
        else:
            row.activation_intent = None
    if row.workflow_operation is not None:
        operation = copy.deepcopy(row.workflow_operation)
        step = operation.get("step")
        if operation.get("kind") == "deactivate" and isinstance(step, dict) and step.get("state") == "prepared":
            row.error_code = "deactivation_pending"
        elif isinstance(step, dict) and step.get("state") in {"prepared", "blocked"}:
            row.workflow_operation = None
            new_operation = _new_deactivation(row, source_generation, scope=scope, access_fence=access_fence) if prior_enabled else None
            if new_operation is not None:
                row.workflow_operation = new_operation
                row.error_code = "deactivation_pending"
            else:
                row.error_code = None
        else:
            operation["cleanup_required"] = True
            operation["error"] = "desired_state_changed_during_dispatch"
            row.workflow_operation = operation
            row.error_code = "workflow_operation_pending"
    elif prior_enabled:
        new_operation = _new_deactivation(row, source_generation, scope=scope, access_fence=access_fence)
        if new_operation is not None:
            row.workflow_operation = new_operation
            row.error_code = "deactivation_pending"
    await session.flush()
    return row


async def begin_enable(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_name: str,
    body: dict[str, object],
    operation_id: UUID | None = None,
    required_credentials: dict[str, dict[str, object]] | None = None,
    activation_id: UUID | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Acquire admission/Source/provisioning/slots before the held begin_enable mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source, row, slots = await lock_connector(
        session, source_id, tuple((required_credentials or {}).keys()),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return await begin_enable_in_uow(
        session, source_id, source_generation, revision, configuration, workflow_name, body,
        operation_id, required_credentials, activation_id,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )


async def begin_enable_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    workflow_name: str,
    body: dict[str, object],
    operation_id: UUID | None = None,
    required_credentials: dict[str, dict[str, object]] | None = None,
    activation_id: UUID | None = None,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> UUID | None:
    """Create enable work under held admission/Source/provisioning/required credential locks.

    Fresh nonlocking proof checks the original AccessFence and durable activation principal;
    no upstream locks, provider I/O or commit. Caller captured parents before entering.
    """
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source, row, slots = await _read_connector_rows(
        session, source_id, tuple((required_credentials or {}).keys()),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None or source.status != "active" or source.generation != source_generation
        or row is None or row.source_generation != source_generation
        or row.desired_revision != revision or row.workflow_operation is not None
        or row.activation_intent is None
        or row.activation_intent.get("id") != str(activation_id)
        or not await _operation_matches(session, row.activation_intent, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        or not _required_credentials_match(required_credentials or {}, slots)
    ):
        return None
    operation_id = operation_id or uuid4()
    row.desired_enabled = True
    row.state = "provisioning"
    row.error_code = None
    row.workflow_operation = new_workflow_operation(
        operation_id=operation_id,
        kind="enable",
        source_generation=source_generation,
        revision=revision,
        configuration=configuration,
        workflow_id=row.workflow_id,
        workflow_name=workflow_name,
        body=body,
        activation_id=activation_id, scope=scope, access_fence=access_fence,
    )
    row.workflow_operation["required_credentials"] = copy.deepcopy(required_credentials or {})
    row.workflow_operation["backend_revision"] = row.backend_revision
    await session.flush()
    return operation_id


async def begin_activation_bundle(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    activation_id: UUID,
    required_credentials: dict[str, dict[str, object]],
    credential_intents: dict[str, dict[str, object]],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Acquire admission/Source/provisioning/slots before the held begin_activation_bundle mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    slots_to_lock = tuple(sorted(set(required_credentials) | set(credential_intents)))
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source, row, slots = await lock_connector(session, source_id, slots_to_lock, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await begin_activation_bundle_in_uow(
        session, source_id, source_generation, revision, configuration, activation_id,
        required_credentials, credential_intents,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )


async def begin_activation_bundle_in_uow(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    revision: int,
    configuration: dict[str, object],
    activation_id: UUID,
    required_credentials: dict[str, dict[str, object]],
    credential_intents: dict[str, dict[str, object]],
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> bool:
    """Stage activation under held admission/Source/provisioning/all required credential locks.

    Compare captured AccessFence with fresh nonlocking proof; flush without I/O or commit.
    False may follow partial slot/dictionary mutation and requires rollback; True requires
    caller commit. Enter only after acquiring every required/intended slot in sorted order.
    """
    slots_to_lock = tuple(sorted(set(required_credentials) | set(credential_intents)))
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source, row, slots = await _read_connector_rows(session, source_id, slots_to_lock, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        source is None or source.status != "active" or source.generation != source_generation
        or row is None or row.source_generation != source_generation
        or row.desired_revision != revision or row.desired_enabled
        or row.state == "provisioning" or row.workflow_operation is not None
        or row.activation_intent is not None
    ):
        return False

    for slot, intent in credential_intents.items():
        existing = slots.get(slot)
        if existing is not None and existing.state in {
            "dispatching", "reconciliation_required", "delete_pending"
        }:
            return False
        if (existing.credential_id if existing is not None else None) != intent.get("target_id"):
            return False
        operation_id = UUID(str(intent["operation_id"]))
        envelope = {
            **_operation_identity(scope, access_fence),
            "id": str(operation_id),
            "activation_id": str(activation_id),
            "kind": str(intent["kind"]),
            "state": "prepared",
            "source_generation": source_generation,
            "revision": revision,
            "credential_type": str(intent["credential_type"]),
            "target_id": intent.get("target_id"),
            "binding": copy.deepcopy(intent["binding"]),
            "input_ciphertext": str(intent["input_ciphertext"]),
            "dispatch_started_at": None,
        }
        if existing is None:
            existing = ConnectorManagedCredential(
                source_id=source_id,
                slot=slot,
                credential_id=intent.get("target_id"),
                operation_id=operation_id,
                operation_revision=revision,
                source_generation=source_generation,
                credential_type=str(intent["credential_type"]),
                state="queued",
                operation_envelope=envelope,
            )
            session.add(existing)
        else:
            existing.operation_id = operation_id
            existing.operation_revision = revision
            existing.source_generation = source_generation
            existing.credential_type = str(intent["credential_type"])
            existing.state = "queued"
            existing.error_code = None
            existing.operation_envelope = envelope

    for slot, required in required_credentials.items():
        if slot in credential_intents:
            required["operation_id"] = str(credential_intents[slot]["operation_id"])
            required["credential_id"] = credential_intents[slot].get("target_id")
        else:
            existing = slots.get(slot)
            if (
                existing is None or existing.state != "ready" or not existing.credential_id
                or existing.resolved_binding != required.get("binding")
            ):
                return False
            required["credential_id"] = existing.credential_id
            required["operation_id"] = None

    row.desired_enabled = True
    row.state = "provisioning"
    row.error_code = None
    if credential_intents:
        row.credential_revision += 1
    row.activation_intent = {
        **_operation_identity(scope, access_fence),
        "id": str(activation_id),
        "source_generation": source_generation,
        "revision": revision,
        "configuration": copy.deepcopy(configuration),
        "required_credentials": copy.deepcopy(required_credentials),
        "state": "prepared",
    }
    await session.flush()
    return True


async def reject_activation(
    session: AsyncSession, source_id: UUID, revision: int, error_code: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Reject a matching activation revision when no workflow operation is in flight."""
    _, row, _ = await lock_connector(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if row is None or row.desired_revision != revision or row.workflow_operation is not None:
        return False
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = error_code
    await session.flush()
    return True


async def prepare_workflow_step(
    session: AsyncSession, source_id: UUID, operation_id: UUID, step_id: str,
    kind: str, target: str | None, request: dict[str, object] | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Settle dispatched lookup under held original retained-effect parent locks.

    Current Source/config permits next create/update; stale lifecycle schedules exact
    known-target cleanup or removes a no-effect lookup. Original envelope/step/request
    CAS remains mandatory. Flush-only; retained effect commit owns transaction release.
    """
    from modules.settings.public import module_is_enabled

    source, row, _ = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or source is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched" or step.get("kind") != "lookup"
        or kind not in {"create", "update"}
        or kind == "create" and target is not None
        or kind == "update" and (not isinstance(target, str) or not target)
    ):
        return False
    current = bool(
        await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        and source.status == "active" and not source.local_only and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
    )
    if not current and target is None:
        # A lookup that found no workflow has no remote effect to repeat or dispose.
        row.workflow_operation = None
        row.state = "disabled" if source.status != "active" else "saved_not_active"
        row.error_code = None
        await session.flush()
        return True
    if target is not None:
        operation["workflow_id"] = target
        row.workflow_id = target
    operation["phase"] = kind if current else "deactivate"
    operation["step"] = _step(kind, target, request) if current else _step("deactivate", target)
    if not current:
        operation["cleanup_required"] = True
        row.error_code = "deactivation_pending"
        row.state = "disabled" if source.status != "active" else "saved_not_active"
    row.workflow_operation = operation
    await session.flush()
    return True


async def clear_retired_source_credentials(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Acquire native then GitHub grant and retire archived Source credentials, flush only.

    Legacy acquiring continuation requires caller-held admission/Source/provisioning and
    no already-acquired later rows. Ordinary lifecycle apply instead passes its prepared
    native and later-held grant to the private held mutation below. Peer retirement retains
    the exact archived anchor and pending last-source provider liability; no network I/O.
    """
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
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
        raise HTTPException(status_code=409, detail="Retired Source credential identity changed")
    if native is not None and (native.state != "revoked" or native.verified_bot_id is not None):
        native.operation_id = uuid4()
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
        raise HTTPException(status_code=409, detail="GitHub retirement grant changed")
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source_fence = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source_fence is None:
        raise HTTPException(status_code=409, detail="GitHub retirement Source changed")
    current = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).execution_options(populate_existing=True))
    if current is None or tuple(getattr(current, field) for field in identity_fields) != observed:
        raise HTTPException(status_code=409, detail="GitHub retirement grant changed")
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
        raise HTTPException(status_code=404, detail="Source not found")
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
                        assert credential is not None
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
        raise HTTPException(status_code=409, detail="Source lifecycle fence changed")
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


async def require_n8n_backend(session: AsyncSession, source_id: UUID) -> None:
    """Reject packaged-workflow (bearer) calls unless the row is an idle n8n source.

    A native source, or any source mid-transition, never accepts a late or stale n8n webhook.
    Nonlocking read under the caller-held Source lock; a missing row is left to later fences.
    """
    row = await session.scalar(
        select(ConnectorProvisioning).where(ConnectorProvisioning.source_id == source_id)
        .execution_options(populate_existing=True))
    if row is not None and (row.execution_backend != "n8n" or row.transition_phase != "idle"):
        raise HTTPException(status_code=409, detail="backend_inactive")


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


async def claim_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[str, object] | None:
    """Durably claim a prepared credential operation before external dispatch.

    Rechecks source, activation/delete intent, generation, and revision; returns
    the dispatched envelope or None when absent/stale. Claiming commits the
    dispatch barrier (and stale-intent cleanup when applicable), preventing blind
    re-dispatch after an uncertain provider response.
    """
    before = await capture_connector_observation(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source, desired, slots = await _read_connector_rows(
        session, source_id, ("collector", "manual_trigger", "provider"),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = slots.get(slot)
    if row is None or row.operation_id != operation_id:
        return None
    envelope = copy.deepcopy(row.operation_envelope)
    if not isinstance(envelope, dict):
        return None
    if not await _operation_matches(session, envelope, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        return None
    if envelope.get("state") != "prepared":
        return None
    valid_enable = bool(
        envelope.get("kind") in {"create", "update"}
        and source is not None and source.status == "active"
        and desired is not None and desired.desired_enabled
        and isinstance(desired.activation_intent, dict)
        and envelope.get("activation_id") == desired.activation_intent.get("id")
        and isinstance(desired.activation_intent.get("required_credentials"), dict)
        and isinstance(desired.activation_intent["required_credentials"].get(slot), dict)
        and desired.activation_intent["required_credentials"][slot].get("operation_id") == str(operation_id)
        and source.generation == envelope.get("source_generation")
        and desired.source_generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    valid_delete = bool(
        envelope.get("kind") == "delete"
        and source is not None and source.status == "paused"
        and desired is not None and not desired.desired_enabled
        and source.generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    if not valid_enable and not valid_delete:
        activation_id = envelope.get("activation_id")
        row.operation_envelope = None
        row.operation_id = None
        row.state = "ready" if row.credential_id else "queued"
        row.error_code = "prepared_credential_intent_stale"
        if isinstance(activation_id, str) and desired is not None and isinstance(desired.activation_intent, dict):  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
            if desired.activation_intent.get("id") == activation_id:
                for sibling in slots.values():
                    sibling_envelope = sibling.operation_envelope
                    if (
                        sibling is not row and isinstance(sibling_envelope, dict)
                        and sibling_envelope.get("activation_id") == activation_id
                        and sibling_envelope.get("state") == "prepared"
                    ):
                        sibling.operation_id = None
                        sibling.operation_envelope = None
                        sibling.state = "ready" if sibling.credential_id else "queued"
                desired.activation_intent = None
                desired.desired_enabled = False
                desired.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
                desired.error_code = "activation_intent_stale"
        await commit_connector_observation(session, before, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return None
    envelope["state"] = "dispatched"
    envelope["dispatch_started_at"] = datetime.now(UTC).isoformat()
    row.operation_envelope = envelope
    row.state = "dispatching"
    # Commit the dispatch barrier before the driver makes the external n8n call.
    await commit_connector_observation(session, before, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return envelope


async def drive_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    client: Any,
    encryption_key: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> bool:
    """Dispatch current credentials; settle exact effects with original access/envelope CAS.

    ``access_fence`` is the caller's original captured fence; current access must still equal it.

    Provider I/O holds no SQL locks. Later Source/config retains known remote IDs or unknown
    recovery material for reconciliation and cannot publish stale activation. Original
    access revocation requires explicit journal-only reconciliation authority.
    """
    from modules.connectors.credentials import (
        CredentialEncryptionUnavailable,
        CredentialOutcomeUnknown,
        CredentialRequestRejected,
        CredentialUpdateOutcomeUnknown,
        decrypt_credential_input,
    )

    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    row = await get_managed_credential(session, source_id, slot, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if row is None or row.operation_id is None:
        return False
    operation_id = row.operation_id
    prepared = row.operation_envelope
    if not isinstance(prepared, dict) or prepared.get("state") != "prepared":
        return False
    try:
        request, binding = decrypt_credential_input(
            encryption_key,
            str(prepared["input_ciphertext"]),
            source_id=source_id,
            slot=slot,
            operation_id=operation_id,
        )
    except CredentialEncryptionUnavailable:
        await session.rollback()
        return False
    envelope = await claim_credential_operation(session, source_id, slot, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if envelope is None:
        await session.rollback()
        return False
    if not await _credential_send_allowed(session, source_id, slot, envelope, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence):
        await _settle_retained_credential_after_io(
            session, source_id, slot, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="not_sent", error_code="original_effect_send_fenced",
        )
        return False
    try:
        data = request["data"]
        if not isinstance(data, dict):
            raise CredentialEncryptionUnavailable("Stored connector credential request is invalid")
        name = str(request["name"])
        header = str(data["name"])
        secret = str(data["value"])
        target = envelope.get("target_id")
        if envelope.get("kind") == "create" and target is None:
            credential_id = await client.create_http_header(name, header, secret)
        elif envelope.get("kind") == "update" and isinstance(target, str):
            await client.rotate_http_header(target, name, header, secret)
            credential_id = target
        else:
            raise CredentialEncryptionUnavailable("Stored connector credential operation is invalid")
    except asyncio.CancelledError:
        await _settle_retained_credential_after_io(
            session, source_id, slot, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="unknown", error_code="credential_operation_outcome_unknown",
        )
        raise
    except CredentialRequestRejected:
        await _settle_retained_credential_after_io(
            session, source_id, slot, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="known_rejection", error_code="n8n_credential_rejected",
        )
        return False
    except (CredentialOutcomeUnknown, CredentialUpdateOutcomeUnknown, CredentialEncryptionUnavailable):
        await _settle_retained_credential_after_io(
            session, source_id, slot, original_operation=envelope, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            outcome="unknown", error_code="credential_operation_outcome_unknown",
        )
        return False
    return await _settle_retained_credential_after_io(
        session, source_id, slot, original_operation=envelope, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        outcome="known_success", remote_id=credential_id, binding=binding,
    )


async def complete_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    *,
    credential_id: str | None,
    binding: dict[str, object],
    scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Acknowledge a matching dispatched credential operation in the caller transaction.

    Clears encrypted request material and fences stale activation intent; returns
    False for an operation that no longer owns the dispatched slot. Flushes only,
    leaving commit to the driver that also records the safe observation/event.

    Caller holds lock_retained_connector_effect's original admission/Source/provisioning/
    slots. Compare captured envelope/target under original access; later Source/config
    only selects retained reconciliation, never ready activation. Flush-only; commit through
    commit_retained_connector_effect. Revoked access needs explicit reconciliation authority.
    """
    from modules.settings.public import module_is_enabled

    source, desired, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or not isinstance(envelope, dict)
        or envelope.get("state") != "dispatched"
        or envelope.get("id") != str(operation_id)
        or row.operation_revision != envelope.get("revision") or row.source_generation != envelope.get("source_generation")
        or envelope.get("retained_effect_result")
        or not _retained_operation_matches(envelope, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None or desired is None
        or envelope.get("kind") not in {"create", "update"}
        or not isinstance(credential_id, str) or not credential_id
        or envelope.get("kind") == "update" and envelope.get("target_id") != credential_id
    ):
        return False
    if credential_id is not None:
        row.credential_id = credential_id
    row.resolved_binding = copy.deepcopy(binding)
    current = bool(
        await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        and source.status == "active" and not source.local_only
        and source.generation == desired.source_generation == envelope.get("source_generation")
        and desired.desired_revision == envelope.get("revision")
    )
    row.state = "ready" if current else "reconciliation_required"
    row.error_code = None if current else "retained_credential_effect_requires_reconciliation"
    desired.credential_revision += 1
    envelope["state"] = "succeeded"
    if not current:
        envelope["cleanup_required"] = True
        envelope["error"] = "retained_credential_effect_requires_reconciliation"
    envelope.pop("input_ciphertext", None)
    row.operation_envelope = envelope
    if (
        desired is not None and isinstance(desired.activation_intent, dict)
        and desired.activation_intent.get("id") == envelope.get("activation_id")
        and not current
    ):
        desired.activation_intent = None
        desired.desired_enabled = False
        if source is None or source.status != "active":
            desired.state = "disabled"
        else:
            desired.state = "saved_not_active"
        desired.error_code = "activation_intent_stale"
    await session.flush()
    return True


async def fail_credential_operation(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    error_code: str,
    *,
    unknown: bool,
    scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Record a known rejection or ambiguous outcome in the caller transaction.

    Unknown outcomes retain recovery information; returns False for a stale
    operation identity. Flushes only, so the operation driver owns commit/rollback.

    Caller holds original admission/retained Source/provisioning/all slots. Immutable
    captured operation/target CAS is independent of current generation/revision; unknown
    outcomes retain exact encrypted recovery material and never become redispatchable.
    Flush-only; commit through commit_retained_connector_effect under original access.
    """
    source, desired, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or not isinstance(envelope, dict)
        or envelope.get("state") != "dispatched"
        or envelope.get("id") != str(operation_id)
        or row.operation_revision != envelope.get("revision") or row.source_generation != envelope.get("source_generation")
        or envelope.get("retained_effect_result")
        or not _retained_operation_matches(envelope, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None or desired is None
    ):
        return False
    envelope["state"] = "unknown" if unknown else "rejected"
    if unknown and (source.generation != envelope.get("source_generation")
                    or desired.desired_revision != envelope.get("revision")
                    or source.status != "active" or source.local_only):
        envelope["cleanup_required"] = True
    if not unknown:
        envelope.pop("input_ciphertext", None)
        row.state = "ready" if row.credential_id else "queued"
        row.operation_id = None
        row.operation_envelope = None
        row.error_code = error_code
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            for sibling in slots.values():
                sibling_envelope = sibling.operation_envelope
                if (
                    sibling is not row and isinstance(sibling_envelope, dict)
                    and sibling_envelope.get("activation_id") == activation_id
                    and sibling_envelope.get("state") == "prepared"
                ):
                    sibling.operation_id = None
                    sibling.operation_envelope = None
                    sibling.state = "ready" if sibling.credential_id else "queued"
            if desired is not None and isinstance(desired.activation_intent, dict) and desired.activation_intent.get("id") == activation_id:
                desired.activation_intent = None
        if (
            desired is not None
            and desired.desired_revision == int(envelope["revision"])
            and desired.source_generation == envelope.get("source_generation")
        ):
            desired.desired_enabled = False
            desired.state = "saved_not_active"
            desired.error_code = error_code
        await session.flush()
        return True
    else:
        row.state = "reconciliation_required"
        activation_id = envelope.get("activation_id")
        if isinstance(activation_id, str):
            for sibling in slots.values():
                sibling_envelope = sibling.operation_envelope
                if (
                    sibling is not row and isinstance(sibling_envelope, dict)
                    and sibling_envelope.get("activation_id") == activation_id
                    and sibling_envelope.get("state") == "prepared"
                ):
                    sibling.operation_id = None
                    sibling.operation_envelope = None
                    sibling.state = "ready" if sibling.credential_id else "queued"
            if desired is not None and isinstance(desired.activation_intent, dict) and desired.activation_intent.get("id") == activation_id:
                desired.activation_intent["state"] = "outcome_unknown"
    row.error_code = error_code
    row.operation_envelope = envelope
    if (
        desired is not None and unknown and desired.desired_revision == int(envelope["revision"])
        and desired.source_generation == envelope.get("source_generation")
    ):
        desired.state = "reconciliation_required"
        desired.error_code = error_code
    await session.flush()
    return True


async def create_delete_intent(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    expected_revision: int,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[ConnectorProvisioning, ConnectorManagedCredential, UUID] | None:
    """Acquire admission/Source/provisioning/slots before the held create_delete_intent mutation.

    Enter before domain locks; caller retains ordered parents until final commit.
    """
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source, desired, slots = await lock_connector(session, source_id, (slot,), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await create_delete_intent_in_uow(
        session, source_id, slot, expected_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )


async def create_delete_intent_in_uow(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    expected_revision: int,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> tuple[ConnectorProvisioning, ConnectorManagedCredential, UUID] | None:
    """Prepare deletion under held admission/Source/provisioning/exact credential slot locks.

    Compare original AccessFence without locks; Source must be paused/config disabled.
    Stamp the original principal/config in the durable intent; flush only, caller commits.
    """
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source, desired, slots = await _read_connector_rows(session, source_id, (slot,), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = slots.get(slot)
    if (
        source is None or source.status != "paused" or desired is None
        or desired.desired_revision != expected_revision or desired.desired_enabled
        or desired.workflow_operation is not None
        or desired.state != "disabled" or not row or not row.credential_id
        or row.state in {"dispatching", "reconciliation_required", "delete_pending"}
    ):
        return None
    operation_id = uuid4()
    row.operation_id = operation_id
    row.operation_revision = expected_revision
    row.source_generation = source.generation
    row.state = "delete_pending"
    row.error_code = None
    desired.credential_revision += 1
    row.operation_envelope = {
        **_operation_identity(scope, access_fence),
        "id": str(operation_id),
        "kind": "delete",
        "state": "prepared",
        "source_generation": source.generation,
        "revision": expected_revision,
        "target_id": row.credential_id,
        "credential_type": row.credential_type,
        "dispatch_started_at": None,
    }
    await session.flush()
    return desired, row, operation_id


async def acknowledge_credential_delete(
    session: AsyncSession,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    target_id: str,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Acknowledge exact dispatched deletion under held observation admission/Source/slots.

    Caller acquired lock_retained_connector_effect after I/O with original scope/access.
    Compare immutable original operation/remote target, allowing later Source/config only
    for this exact delete result. Flush-only; caller commits retained effect or rolls back.
    """
    source, desired, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    row = slots.get(slot)
    envelope = copy.deepcopy(row.operation_envelope) if row is not None else None
    if (
        row is None or row.operation_id != operation_id or row.credential_id != target_id
        or row.state != "dispatching" or not isinstance(envelope, dict)
        or envelope.get("id") != str(operation_id) or envelope.get("target_id") != target_id
        or envelope.get("state") != "dispatched"
        or envelope.get("kind") != "delete"
        or row.operation_revision != envelope.get("revision") or row.source_generation != envelope.get("source_generation")
        or envelope.get("retained_effect_result")
        or not _retained_operation_matches(envelope, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None or desired is None
    ):
        return False
    row.credential_id = None
    row.operation_id = None
    row.state = "queued"
    row.error_code = None
    row.operation_envelope = None
    row.resolved_binding = None
    await session.flush()
    return True


async def claim_workflow_step(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> dict[str, object] | None:
    """Claim current activation or exact retained prepared deactivation under original access.

    Returns the dispatched operation or None when there is no eligible step;
    None can still commit blocked-credential or stale-intent cleanup. A claim
    commits its dispatch barrier before external n8n work, preventing automatic
    blind replay when the provider outcome is uncertain.
    Supplied envelope is captured before entry; CAS includes original step/request/target.
    Deactivation remains usable after Source/config advancement without changing its lineage.
    """
    before = await lock_retained_connector_effect(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    source, row, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if source is None or row is None or not isinstance(row.workflow_operation, dict):
        return None
    operation = copy.deepcopy(row.workflow_operation)
    step = operation.get("step")
    if operation.get("retained_effect_result") or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence):
        return None
    if not isinstance(step, dict) or step.get("state") != "prepared":
        return None
    cleanup = step.get("kind") == "deactivate"
    if cleanup and (not isinstance(step.get("target"), str)
                    or step.get("target") != operation.get("workflow_id")
                    or step.get("target") != row.workflow_id):
        return None
    if not cleanup and operation.get("kind") == "enable" and not _required_credentials_match(
        operation.get("required_credentials", {}), slots
    ):
        operation["error"] = "required_credential_binding_unresolved"
        step["state"] = "blocked"
        operation["step"] = step
        row.workflow_operation = operation
        row.state = "reconciliation_required"
        row.error_code = "required_credential_binding_unresolved"
        await commit_retained_connector_effect(session, before, source_id=source_id, original_operation=original_operation, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        return None
    if not cleanup and operation.get("kind") == "enable" and (
        operation.get("cleanup_required") or not row.desired_enabled or source.status != "active" or source.local_only
        or row.source_generation != operation.get("source_generation")
        or row.desired_revision != operation.get("revision")
        or source.generation != operation.get("source_generation")
    ):
        if row.workflow_id:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", row.workflow_id)
            operation["workflow_id"] = row.workflow_id
            row.workflow_operation = operation
            row.error_code = "deactivation_pending"
            row.state = "saved_not_active"
        else:
            row.workflow_operation = None
            row.state = "disabled" if source.status != "active" else "saved_not_active"
        row.desired_enabled = False
        if isinstance(row.activation_intent, dict) and row.activation_intent.get("id") == operation.get("activation_id"):
            row.activation_intent = None
        await commit_retained_connector_effect(session, before, source_id=source_id, original_operation=original_operation, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        return None
    step["state"] = "dispatched"
    step["dispatch_started_at"] = datetime.now(UTC).isoformat()
    operation["step"] = step
    row.workflow_operation = operation
    # Commit the step's dispatch barrier before the workflow driver calls n8n.
    await commit_retained_connector_effect(session, before, source_id=source_id, original_operation=original_operation, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    return operation


async def acknowledge_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    *,
    workflow_id: str | None = None,
    scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Acknowledge a matching dispatched step and flush its workflow transition.

    Returns False for a stale operation/step identity. The driver performs the
    final commit together with the safe source observation/event.

    Caller holds original admission/retained Source/provisioning/all slots. Immutable
    original envelope/step/request/target CAS precedes current activation-versus-cleanup
    selection. Later generation/config never discards a known effect or publishes stale
    activation. Flush-only; caller uses commit_retained_connector_effect after settlement.
    """
    from modules.settings.public import module_is_enabled

    source, row, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched"
        or step.get("kind") in {"update", "activate", "deactivate"}
        and (step.get("target") != operation.get("workflow_id") or step.get("target") != row.workflow_id)
        or step.get("kind") == "update" and workflow_id != step.get("target")
        or step.get("kind") == "create" and (step.get("target") is not None or not workflow_id)
    ):
        return False
    step["state"] = "succeeded"
    history = step.get("history")
    if isinstance(history, list):
        history.append({"id": step_id, "kind": step.get("kind"), "state": "succeeded"})
    operation["step"] = step
    if workflow_id is not None:
        operation["workflow_id"] = workflow_id
        row.workflow_id = workflow_id
        row.workflow_name = str(operation.get("workflow_name") or row.workflow_name or "")
    kind = step.get("kind")
    current = bool(
        await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        and source is not None and source.status == "active" and not source.local_only and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
        and row.execution_backend == "n8n"
        and row.transition_phase in ("idle", "activating_new")
        and operation.get("backend_revision", row.backend_revision) == row.backend_revision
        and _required_credentials_match(operation.get("required_credentials", {}), slots)
    )
    target = str(operation.get("workflow_id") or row.workflow_id or "") or None
    if kind in {"create", "update"}:
        operation["workflow_id"] = target
        if current:
            operation["phase"] = "activate"
            operation["step"] = _step("activate", target)
        else:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", target)
            row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
            row.error_code = "deactivation_pending"
        row.workflow_operation = operation
    elif kind == "activate" and current:
        row.state = "active"
        row.applied_revision = int(operation["revision"])
        row.error_code = None
        row.workflow_operation = None
        finalize_backend(row)
        if (
            isinstance(row.activation_intent, dict)
            and row.activation_intent.get("id") == operation.get("activation_id")
        ):
            row.activation_intent = None
    elif kind in {"activate", "create", "update"}:
        operation["cleanup_required"] = True
        operation["phase"] = "deactivate"
        operation["step"] = _step("deactivate", target)
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        row.error_code = "deactivation_pending"
        row.workflow_operation = operation
        row.desired_enabled = False
    elif kind == "deactivate":
        row.workflow_operation = None
        row.error_code = None
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        if (
            operation.get("cleanup_required")
            and isinstance(row.activation_intent, dict)
            and row.activation_intent.get("id") == operation.get("activation_id")
        ):
            row.activation_intent = None
    else:
        operation["error"] = "unsupported_workflow_step"
        row.workflow_operation = operation
        row.error_code = "workflow_operation_unsupported"
        row.state = "disabled" if not row.desired_enabled else "saved_not_active"
    await session.flush()
    return True


async def resolve_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    workflow_id: str,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Attach recovered workflow under held observation admission/Source/provisioning locks.

    Caller holds lock_retained_connector_effect's original admission/Source/Connector
    rows and supplies the exact unknown-create envelope retained through identity lookup.
    Retain discovered remote ID and choose activation or exact cleanup from current fences;
    never repeat the uncertain create. Flush-only; use commit_retained_connector_effect.
    """
    from modules.settings.public import module_is_enabled

    source, row, slots = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None or not isinstance(workflow_id, str) or not workflow_id
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("kind") != "create" or step.get("state") != "unknown"
        or step.get("target") is not None
    ):
        return False
    row.workflow_id = workflow_id
    row.workflow_name = str(operation.get("workflow_name") or row.workflow_name or "")
    operation["workflow_id"] = workflow_id
    current = bool(
        await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        and source is not None and source.status == "active" and not source.local_only and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
        and _required_credentials_match(operation.get("required_credentials", {}), slots)
    )
    if current:
        operation["phase"] = "activate"
        operation["step"] = _step("activate", workflow_id)
        row.state = "provisioning"
        row.error_code = None
    else:
        operation["cleanup_required"] = True
        operation["phase"] = "deactivate"
        operation["step"] = _step("deactivate", workflow_id)
        row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        row.error_code = "deactivation_pending"
    row.workflow_operation = operation
    await session.flush()
    return True


async def defer_unknown_workflow_create(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID | str,
    step_id: str,
    *, scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Acquire original retained parents and move one exact unknown-create barrier later.

    Later Source/config does not erase the uncertain effect. Exact immutable original
    envelope/step CAS remains mandatory; no blind redispatch or commit is performed.
    """
    await lock_retained_connector_effect(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    source, row, _ = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    operation = row.workflow_operation if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None or row is None
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("kind") != "create" or step.get("state") != "unknown"
    ):
        return False
    latest = await session.scalar(
        select(func.max(ConnectorProvisioning.updated_at)).where(
            ConnectorProvisioning.source_id == source_id,
            ConnectorProvisioning.workflow_operation["step"]["state"].astext == "unknown",
            ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
        )
    )
    now = datetime.now(UTC)
    if latest is not None and now <= latest:
        now = latest + timedelta(microseconds=1)
    assert row is not None
    row.updated_at = now
    await session.flush()
    return True


async def fail_workflow_step(
    session: AsyncSession,
    source_id: UUID,
    operation_id: UUID,
    step_id: str,
    error_code: str,
    *,
    unknown: bool,
    scope: Scope, multi_workspace_enabled: bool,
    original_operation: dict[str, object], access_fence: AccessFence,
) -> bool:
    """Record a rejected or ambiguous workflow result and flush recovery state.

    Schedules deactivation when desired state has changed; returns False for a
    stale step identity. The workflow driver owns the final transaction commit.

    Caller holds original admission/retained Source/provisioning/all slots and supplies
    the immutable dispatched envelope. Known rejection and unknown outcome settle that
    exact step even after generation/config changes; unknown work never becomes prepared.
    Flush-only; use commit_retained_connector_effect without renewed epoch or scope.
    """
    from modules.settings.public import module_is_enabled

    source, row, _ = await _read_retained_connector_rows(
        session, source_id, original_operation=original_operation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    operation = copy.deepcopy(row.workflow_operation) if row is not None else None
    step = operation.get("step") if isinstance(operation, dict) else None
    if (
        row is None or not isinstance(operation, dict) or not isinstance(step, dict)
        or operation.get("retained_effect_result")
        or not _retained_operation_matches(operation, original_operation, source_id=source_id, scope=scope, access_fence=access_fence)
        or source is None
        or operation.get("id") != str(operation_id) or step.get("id") != step_id
        or step.get("state") != "dispatched"
    ):
        return False
    current = bool(
        await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        and source is not None and source.status == "active" and not source.local_only and row.desired_enabled
        and not operation.get("cleanup_required")
        and source.generation == operation.get("source_generation")
        and row.source_generation == operation.get("source_generation")
        and row.desired_revision == operation.get("revision")
    )
    if not unknown and step.get("kind") in {"lookup", "create", "update", "activate"}:
        target = str(operation.get("workflow_id") or row.workflow_id or "") or None
        if not current and target is not None:
            operation["cleanup_required"] = True
            operation["phase"] = "deactivate"
            operation["step"] = _step("deactivate", target)
            row.workflow_operation = operation
            row.error_code = "deactivation_pending"
            row.state = "disabled" if source is None or source.status != "active" else "saved_not_active"
        else:
            row.workflow_operation = None
            if current:
                row.desired_enabled = False
                row.state = "saved_not_active"
                row.error_code = error_code
                if isinstance(row.activation_intent, dict) and row.activation_intent.get("id") == operation.get("activation_id"):
                    row.activation_intent = None
            elif not row.desired_enabled or source is None or source.status != "active":
                row.state = "disabled"
                row.error_code = "workflow_operation_rejected" if source is not None and source.generation == operation.get("source_generation") else row.error_code
            else:
                row.state = "saved_not_active"
        await session.flush()
        return True
    step["state"] = "unknown" if unknown else "rejected"
    operation["step"] = step
    operation["error"] = error_code
    if unknown and not current:
        operation["cleanup_required"] = True
    row.workflow_operation = operation
    row.error_code = error_code if current else "workflow_operation_pending"
    row.state = (
        "reconciliation_required" if current
        else "disabled" if not row.desired_enabled or source is None or source.status != "active"
        else "saved_not_active"
    )
    await session.flush()
    return True


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
    from modules.connectors.n8n import workflow_matches

    for _ in range(4):
        operation = await claim_workflow_step(
            session, source_id, original_operation=original_operation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
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
        target = step.get("target")
        body = step.get("request")
        try:
            if kind == "lookup":
                matches = await api.find_workflows(str(operation["workflow_name"]))
                if len(matches) > 1:
                    raise ValueError("n8n has multiple workflows for this connector operation")
                if matches:
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
                        return False
                    candidate = await api.get_workflow(workflow_id)
                    if not workflow_matches(body, candidate):
                        raise ValueError("n8n workflow lookup returned a mismatched identity")
                    next_kind = "update"
                    next_target = workflow_id
                else:
                    next_kind = "create"
                    next_target = None
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
            if not isinstance(body, dict) and kind in {"create", "update"}:
                raise ValueError("Prepared workflow request body is invalid")
            if kind == "create":
                workflow_id = await api.create_workflow(body)
            elif kind == "update" and isinstance(target, str):
                await api.update_workflow(target, body)
                workflow_id = target
            elif kind == "activate" and isinstance(target, str):
                await api.set_active(target, True)
                workflow_id = None
            elif kind == "deactivate" and isinstance(target, str):
                await api.set_active(target, False)
                workflow_id = None
            else:
                raise ValueError("Unsupported prepared workflow step")
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


async def mark_reconciliation(
    session: AsyncSession,
    source_id: UUID,
    desired_revision: int,
    state: str,
    *,
    error_code: str | None = None,
    workflow_id: str | None = None,
    applied_revision: int | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Update provisioning status only while the expected desired revision is current."""
    _, row, _ = await lock_connector(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if row is None or row.desired_revision != desired_revision:
        return False
    row.state = state
    row.error_code = error_code
    if workflow_id is not None:
        row.workflow_id = workflow_id
    if applied_revision is not None:
        row.applied_revision = applied_revision
    await session.flush()
    return True


async def unresolved_credential_error(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> str | None:
    """Return a stable error code when any source credential operation needs recovery."""
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    rows = await session.scalars(
        select(ConnectorManagedCredential).where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.state.in_(
                ("dispatching", "reconciliation_required", "delete_pending")
            ),
        )
    )
    return "credential_operation_pending" if rows.first() is not None else None


# --------------------------------------------------------------------------- C4 backend transition


def finalize_backend(row: ConnectorProvisioning) -> None:
    """Mark the current backend revision (and for n8n the installed template) applied and go idle."""
    row.applied_backend_revision = row.backend_revision
    if row.execution_backend == "n8n":
        row.template_revision = row.applied_template_revision = CURRENT_TEMPLATE_REVISION
    row.transition_phase = "idle"
    row.target_backend = row.transition_operation_id = None
    row.old_workflow_id = None


async def fence_active_requests(session: AsyncSession, source_id: UUID) -> None:
    """Cancel queued requests, fence running ones (token invalidated, slot freed) and pause the schedule.

    Caller holds Source/provisioning locks. A remote call already sent cannot be recalled, but its
    result can no longer be accepted because the request is no longer running.
    """
    from modules.connectors import scheduler

    active = (await session.scalars(
        select(ConnectorCollectionRequest)
        .where(ConnectorCollectionRequest.source_id == source_id,
               ConnectorCollectionRequest.status.in_(("queued", "running")))
        .with_for_update().execution_options(populate_existing=True))).all()
    for request in active:
        if request.status == "running" and request.active_admission_token is not None:
            await scheduler.settle_admission_in_uow(
                session, request.id, request.active_admission_token, outcome="cancelled", error_code="revision_changed")
        else:
            request.status, request.error_code = "cancelled", "revision_changed"
    schedule = await session.get(ConnectorSchedule, source_id, with_for_update=True)
    if schedule is not None:
        schedule.enabled = False
    await session.flush()


async def begin_backend_transition_in_uow(
    session: AsyncSession, source_id: UUID, expected_revision: int, target_backend: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ConnectorProvisioning:
    """Invalidate the backend revision, stop admission and fence current work; caller commits.

    Covers backend switches and n8n template upgrades (target == current backend with a stale
    template). Raises 404/409; nothing is sent. Source/provisioning/all slots are locked in order.
    """
    source_fence, row, slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if source_fence is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source_fence.status != "active" or row is None or row.source_generation != source_fence.generation:
        raise HTTPException(status_code=409, detail="Enable this source from connector settings first")
    if row.desired_revision != expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if row.transition_phase != "idle":
        raise HTTPException(status_code=409, detail="A backend transition is already in progress")
    if (
        row.state == "provisioning" or row.workflow_operation is not None or row.activation_intent is not None
        or any(c.state in {"dispatching", "reconciliation_required", "delete_pending"} for c in slots.values())
    ):
        raise HTTPException(status_code=409, detail="Resolve the pending connector operation first")
    stale_template = row.execution_backend == "n8n" and row.applied_template_revision < CURRENT_TEMPLATE_REVISION
    if target_backend == row.execution_backend and not stale_template:
        raise HTTPException(status_code=409, detail="Source already uses this backend")
    row.backend_revision += 1
    row.target_backend = target_backend
    row.transition_phase = "draining"
    row.transition_operation_id = uuid4()
    row.old_workflow_id = row.workflow_id if row.execution_backend == "n8n" else None
    row.desired_enabled = False
    row.state = "saved_not_active"
    row.error_code = "backend_transition_pending"
    await fence_active_requests(session, source_id)
    await session.flush()
    return row


async def resolve_backend_transition_in_uow(
    session: AsyncSession, source_id: UUID, expected_revision: int, action: str,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ConnectorProvisioning:
    """Leave reconciliation_required by retrying the old-workflow stop or on explicit owner attestation.

    ``retry`` prepares a fresh idempotent deactivation; ``confirm_inactive`` records the owner's
    out-of-band confirmation that the old workflow is inactive. Neither admits any backend.
    """
    source_fence, row, _slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if source_fence is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if row is None or row.transition_phase != "reconciliation_required" or row.desired_revision != expected_revision:
        raise HTTPException(status_code=409, detail="No reconciliation is pending for this revision")
    if action == "retry":
        operation = _new_deactivation(row, row.source_generation, scope=scope, access_fence=access_fence)
        if operation is None:
            raise HTTPException(status_code=409, detail="Old workflow is unknown; confirm it inactive instead")
        row.workflow_operation = operation
        row.transition_phase = "deactivating_old"
    else:
        row.workflow_operation = None
        row.transition_phase = "activating_new"
    row.error_code = "backend_transition_pending"
    await session.flush()
    return row


async def advance_backend_transition(
    session: AsyncSession, source_id: UUID, api: Any | None,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> str:
    """Drive a persisted transition as far as safe and return its resulting phase.

    draining -> deactivating_old (n8n stop through the reviewed workflow saga, confirmed only by its
    acknowledgement) -> activating_new. An unconfirmed stop becomes reconciliation_required and no
    backend admits. Native activation runs here; n8n activation waits for the owner's /activate.
    """
    from modules.connectors.activation import activate_native_in_uow

    for _ in range(8):
        source_fence, row, slots = await lock_connector(
            session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
        if source_fence is None or row is None:
            await session.rollback()
            return "idle"
        phase = row.transition_phase
        if phase in ("idle", "reconciliation_required"):
            await session.rollback()
            return phase
        before = _connector_observation(source_fence, row, slots, access_fence)
        if phase == "draining":
            await fence_active_requests(session, source_id)
            operation = _new_deactivation(row, row.source_generation, scope=scope, access_fence=access_fence) if row.old_workflow_id else None
            if row.old_workflow_id and operation is None:
                row.transition_phase, row.error_code = "reconciliation_required", "deactivation_unconfirmed"
            else:
                row.workflow_operation = operation
                row.transition_phase = "deactivating_old" if operation is not None else "activating_new"
        elif phase == "deactivating_old":
            operation = row.workflow_operation
            step = operation.get("step") if isinstance(operation, dict) else None
            if operation is None:
                row.transition_phase = "activating_new"  # only a succeeded stop acknowledgement clears the operation
                row.error_code = "backend_transition_pending"
            elif (
                operation.get("kind") != "deactivate" or not isinstance(step, dict)
                or step.get("state") in {"dispatched", "unknown", "rejected", "blocked"}
            ):
                row.transition_phase, row.error_code = "reconciliation_required", "deactivation_unconfirmed"
            elif api is None:  # n8n is not configured: the stop stays pending, never assumed
                await session.rollback()
                return phase
            else:
                original = copy.deepcopy(operation)
                await session.rollback()
                await drive_workflow_operation(
                    session, source_id, api, original_operation=original, access_fence=access_fence,
                    multi_workspace_enabled=multi_workspace_enabled, scope=scope)
                continue
        else:  # activating_new: the old backend is confirmed inactive
            row.execution_backend = row.target_backend or row.execution_backend
            if row.execution_backend == "native":
                error = await activate_native_in_uow(
                    session, source_id, source_fence, row, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled)
                row.error_code = error or None
            else:
                row.error_code = "activation_required"
            resulting = row.transition_phase
            await commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
            return resulting
        await commit_connector_observation(session, before, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return "deactivating_old"
