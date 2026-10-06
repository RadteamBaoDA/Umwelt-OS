"""Durable epoch barrier and owner-facing backup operation contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modules.backup.models import BackupActivity, BackupControl, BackupOperation
from modules.backup.schemas import (
    ActivityReceipt, AdmissionReceipt, BackupControlRead, BackupOperationRead,
    UnresolvedEffectRead,
)

OWNER_ID = 1
ADMISSION_LOCK_KEY = 0x5031324241434B55
TERMINAL_DRAIN_KINDS = frozenset({"activity_finish", "activity_uncertain", "cancel", "privacy_cleanup", "effect_terminal"})


class BackupAdmissionDenied(HTTPException):
    """Indicate that ordinary work is fenced by a durable backup epoch."""

    def __init__(self, phase: str) -> None:
        """Keep only the stable maintenance phase; no caller data is retained."""
        self.phase = phase
        super().__init__(
            status_code=503,
            detail="Writes are paused for a consistent backup",
            headers={"Retry-After": "30"},
        )


async def _shared_barrier(session: AsyncSession) -> None:
    """Acquire the global transaction-scoped shared lock before any owner row lock."""
    await session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": ADMISSION_LOCK_KEY})


async def _exclusive_barrier(session: AsyncSession) -> None:
    """Wait for all admitted transactions, then own the global maintenance transition lock."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK_KEY})


async def _control(session: AsyncSession, *, lock: bool = False) -> BackupControl:
    """Read the durable singleton after the caller has acquired the global barrier."""
    statement = select(BackupControl).where(BackupControl.id == 1)
    if lock:
        statement = statement.with_for_update()
    row = await session.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(status_code=503, detail="Backup control schema is not installed")
    return row


async def admit_write(
    session: AsyncSession, kind: str, work_id: str | None = None, epoch: int | None = None,
) -> AdmissionReceipt:
    """Fence one durable write transaction before its domain locks are acquired.

    The shared advisory lock is held until this transaction ends. During drain,
    only terminal updates belonging to an already admitted earlier epoch pass.
    """
    del work_id  # Work identity belongs to the activity journal, never admission telemetry.
    await _shared_barrier(session)
    control = await _control(session)
    if control.phase == "idle":
        if epoch is not None and epoch != control.epoch:
            raise BackupAdmissionDenied(control.phase)
        return AdmissionReceipt(epoch=control.epoch, phase=control.phase)
    if (control.phase == "draining" and kind in TERMINAL_DRAIN_KINDS
            and epoch is not None and epoch < control.epoch):
        return AdmissionReceipt(epoch=epoch, phase=control.phase)
    raise BackupAdmissionDenied(control.phase)


async def register_activity(session: AsyncSession, kind: str, work_id: str | None = None) -> ActivityReceipt:
    """Persist an admitted request/job identity before it performs source writes or effects."""
    if not kind or len(kind) > 80 or (work_id is not None and len(work_id) > 255):
        raise HTTPException(status_code=422, detail="Invalid backup activity identity")
    admission = await admit_write(session, kind, work_id)
    activity = BackupActivity(id=uuid4(), epoch=admission.epoch, kind=kind, work_id=work_id)
    session.add(activity)
    await session.flush()
    return ActivityReceipt(activity_id=activity.id, epoch=activity.epoch, kind=activity.kind)


async def register_request_activity(
    request: Request, session: AsyncSession, kind: str, work_id: str | None = None,
) -> ActivityReceipt:
    """Commit one request's activity receipt before it enters a durable owner mutation."""
    receipt = getattr(request.state, "backup_activity", None)
    if receipt is None:
        receipt = await register_activity(session, kind, work_id)
        request.state.backup_activity = receipt
        await session.commit()
    return receipt


async def finish_activity(
    session: AsyncSession, receipt: ActivityReceipt, *, uncertain: bool = False, interrupted: bool = False,
) -> bool:
    """Publish a terminal receipt during drain without opening new work.

    ``interrupted`` is for a request lifetime that ended without a delivered response. It
    does not stand in for external-effect uncertainty; those owners keep separate activity
    receipts and mark them uncertain until their own durable reconciliation completes.
    """
    if uncertain and interrupted:
        raise ValueError("An activity cannot be both interrupted and uncertain")
    await admit_write(session, "activity_uncertain" if uncertain else "activity_finish", epoch=receipt.epoch)
    row = await session.scalar(select(BackupActivity).where(
        BackupActivity.id == receipt.activity_id,
        BackupActivity.epoch == receipt.epoch,
        BackupActivity.state == "active",
    ).with_for_update())
    if row is None:
        return False
    row.state = "uncertain" if uncertain else ("interrupted" if interrupted else "finished")
    row.finished_at = datetime.now(UTC)
    await session.flush()
    return True


async def active_activity_count(session: AsyncSession, epoch_before: int | None = None) -> int:
    """Count unfinished admitted activities, optionally through a captured epoch."""
    statement = select(func.count()).select_from(BackupActivity).where(BackupActivity.state == "active")
    if epoch_before is not None:
        statement = statement.where(BackupActivity.epoch < epoch_before)
    return int(await session.scalar(statement) or 0)


async def uncertain_activity_count(session: AsyncSession, epoch_before: int | None = None) -> int:
    """Count unresolved effect outcomes that require owner reconciliation before snapshot."""
    statement = select(func.count()).select_from(BackupActivity).where(BackupActivity.state == "uncertain")
    if epoch_before is not None:
        statement = statement.where(BackupActivity.epoch < epoch_before)
    return int(await session.scalar(statement) or 0)


async def unresolved_owner_effects(session: AsyncSession) -> list[UnresolvedEffectRead]:
    """Aggregate bounded owner reasons/counts for externally unresolved effects.

    Each owner decides which durable states prove an attempted external action has an unknown
    outcome. Backup never reaches into another module's ORM tables or interprets its statuses.
    Missing owner contracts fail closed so a snapshot cannot silently omit a journal.
    """
    from modules.agents import public as agents
    from modules.automations import public as automations
    from modules.connectors import public as connectors
    from modules.tools import public as tools
    from modules.knowledge.temporal import public as temporal

    owners = (
        ("agents", agents), ("automations", automations), ("connectors", connectors),
        ("tools", tools), ("temporal", temporal),
    )
    result: list[UnresolvedEffectRead] = []
    for owner_name, owner in owners:
        projection = getattr(owner, "unresolved_backup_effects", None)
        if projection is None:
            result.append(UnresolvedEffectRead(
                owner=owner_name, reason="owner_projection_unavailable", count=1,
            ))
            continue
        counts = await projection(session)
        if not isinstance(counts, dict) or len(counts) > 8:
            raise HTTPException(status_code=503, detail="Owner effect reconciliation projection is invalid")
        for reason, count in counts.items():
            if not isinstance(reason, str) or len(reason) > 64 or type(count) is not int or count < 0:
                raise HTTPException(status_code=503, detail="Owner effect reconciliation projection is invalid")
            if count:
                result.append(UnresolvedEffectRead(owner=owner_name, reason=reason, count=min(count, 1_000_000)))
    return result


async def begin_operation(session: AsyncSession, coordinator_id: str) -> BackupOperationRead:
    """Atomically fence new work and publish the next draining epoch."""
    if not coordinator_id or len(coordinator_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid coordinator identity")
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    if control.phase != "idle" or control.operation_id is not None:
        raise BackupAdmissionDenied(control.phase)
    epoch = control.epoch + 1
    operation = BackupOperation(id=uuid4(), epoch=epoch, status="draining", stage_receipts={})
    session.add(operation)
    control.epoch = epoch
    control.phase = "draining"
    control.operation_id = operation.id
    control.coordinator_id = coordinator_id
    control.heartbeat_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def create_operation_intent(session: AsyncSession) -> BackupOperationRead:
    """Create or return the single pending owner request without pausing writes."""
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    if control.phase != "idle" or control.operation_id is not None:
        raise BackupAdmissionDenied(control.phase)
    pending = await session.scalar(select(BackupOperation).where(
        BackupOperation.status == "pending",
    ).order_by(BackupOperation.created_at).limit(1).with_for_update())
    if pending is not None:
        return BackupOperationRead.model_validate(pending, from_attributes=True)
    operation = BackupOperation(id=uuid4(), epoch=control.epoch + 1, status="pending", stage_receipts={})
    session.add(operation)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def claim_operation(
    session: AsyncSession, operation_id: UUID, coordinator_id: str,
) -> BackupOperationRead:
    """Claim one pending intent and publish its new draining epoch exactly once."""
    if not coordinator_id or len(coordinator_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid coordinator identity")
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    if operation is None:
        raise HTTPException(status_code=404, detail="Backup operation not found")
    if control.phase != "idle" or control.operation_id is not None or operation.status != "pending":
        raise HTTPException(status_code=409, detail="Backup operation cannot be claimed")
    epoch = control.epoch + 1
    control.epoch = epoch
    control.phase = "draining"
    control.operation_id = operation.id
    control.coordinator_id = coordinator_id
    control.heartbeat_at = datetime.now(UTC)
    operation.epoch = epoch
    operation.status = "draining"
    operation.started_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def transition_operation(
    session: AsyncSession, operation_id: UUID, expected: str, phase: str,
    *, stage: str | None = None, receipt: dict[str, object] | None = None,
) -> BackupOperationRead:
    """Compare-and-swap a coordinator phase and merge one redacted stage receipt."""
    transitions = {
        "draining": {"quiesced", "resuming", "failed_recovery_required"},
        "quiesced": {"snapshotting", "resuming", "failed_recovery_required"},
        "snapshotting": {"resuming", "failed_recovery_required"},
        "resuming": {"completed", "incomplete", "failed", "failed_recovery_required"},
        "failed_recovery_required": {"resuming"},
    }
    if (phase not in transitions.get(expected, set())
            or (stage is not None and (not stage or len(stage) > 64))):
        raise HTTPException(status_code=422, detail="Invalid backup operation transition")
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    if operation is None or control.operation_id != operation_id:
        raise HTTPException(status_code=404, detail="Backup operation not found")
    if control.phase != expected or operation.status != expected:
        raise HTTPException(status_code=409, detail="Backup operation changed; reload before continuing")
    if phase in {"quiesced", "snapshotting"}:
        if await active_activity_count(session, operation.epoch):
            raise HTTPException(status_code=409, detail="Admitted activity is still active")
        if await uncertain_activity_count(session, operation.epoch):
            raise HTTPException(status_code=409, detail="Uncertain external activity requires reconciliation")
        blockers = await unresolved_owner_effects(session)
        if blockers:
            raise HTTPException(status_code=409, detail={
                "code": "owner_effects_require_reconciliation",
                "unresolved_owner_effects": [item.model_dump(mode="json") for item in blockers],
            })
    control.phase = phase if phase not in {"completed", "incomplete", "failed"} else "resuming"
    control.heartbeat_at = datetime.now(UTC)
    operation.status = phase
    if operation.started_at is None and phase in {"draining", "quiesced", "snapshotting"}:
        operation.started_at = datetime.now(UTC)
    if stage is not None and receipt is not None:
        current = dict(operation.stage_receipts or {})
        current[stage] = _safe_stage_receipt(receipt)
        operation.stage_receipts = current
    if phase in {"completed", "incomplete", "failed", "failed_recovery_required"}:
        operation.finished_at = datetime.now(UTC)
    if phase in {"completed", "incomplete", "failed", "failed_recovery_required"}:
        operation.completeness = "complete" if phase == "completed" else "incomplete"
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


def _safe_stage_receipt(receipt: dict[str, object]) -> dict[str, object]:
    """Allow only small outcome facts, never paths, arbitrary logs, or credential values."""
    allowed = {
        "status", "files", "bytes", "schema_version", "detail_code", "components",
        "workflow_states", "workflow_id", "original_active", "effect_status", "archive_name",
    }
    if not receipt or set(receipt) - allowed:
        raise HTTPException(status_code=422, detail="Backup stage receipt contains unsupported fields")
    result: dict[str, object] = {}
    for key, value in receipt.items():
        if key in {"files", "bytes"}:
            if not isinstance(value, int) or value < 0:
                raise HTTPException(status_code=422, detail="Backup stage receipt count is invalid")
        elif key == "status":
            if value not in {"complete", "incomplete", "failed", "not_configured", "validated"}:
                raise HTTPException(status_code=422, detail="Backup stage receipt status is invalid")
        elif key in {"schema_version", "detail_code"}:
            if not isinstance(value, str) or not value or len(value) > 64:
                raise HTTPException(status_code=422, detail="Backup stage receipt label is invalid")
        elif key == "archive_name":
            if (not isinstance(value, str) or not value or len(value) > 255
                    or "/" in value or "\\" in value or any(ord(char) < 32 for char in value)):
                raise HTTPException(status_code=422, detail="Backup archive name is invalid")
        elif key == "workflow_states":
            if not isinstance(value, list) or len(value) > 10_000:
                raise HTTPException(status_code=422, detail="Backup workflow state inventory is invalid")
            identifiers: set[str] = set()
            for item in value:
                if (not isinstance(item, dict) or set(item) != {"workflow_id", "original_active"}
                        or not isinstance(item["workflow_id"], str)
                        or not item["workflow_id"] or len(item["workflow_id"]) > 256
                        or any(ord(char) < 32 for char in item["workflow_id"])
                        or not isinstance(item["original_active"], bool)
                        or item["workflow_id"] in identifiers):
                    raise HTTPException(status_code=422, detail="Backup workflow state is invalid")
                identifiers.add(item["workflow_id"])
        elif key == "workflow_id":
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 32 for char in value):
                raise HTTPException(status_code=422, detail="Backup workflow identity is invalid")
        elif key == "original_active":
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail="Backup workflow state is invalid")
        elif key == "effect_status":
            if value not in {"attempted", "succeeded", "uncertain", "failed"}:
                raise HTTPException(status_code=422, detail="Backup workflow effect state is invalid")
        elif key == "components":
            if (not isinstance(value, dict) or len(value) > 16
                    or any(not isinstance(name, str) or len(name) > 64
                           or state not in {"complete", "incomplete", "not_configured"}
                           for name, state in value.items())):
                raise HTTPException(status_code=422, detail="Backup stage component receipt is invalid")
        result[key] = value
    return result


async def record_stage_receipt(
    session: AsyncSession, operation_id: UUID, expected: str, stage: str,
    receipt: dict[str, object],
) -> BackupOperationRead:
    """Persist one sanitized component outcome without mutating the maintenance phase."""
    if not stage or len(stage) > 64:
        raise HTTPException(status_code=422, detail="Invalid backup stage")
    safe_receipt = _safe_stage_receipt(receipt)
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    phase_matches = control.phase == expected or (
        expected in {"resuming", "failed_recovery_required"}
        and control.phase == "idle" and control.operation_id == operation_id
    )
    if (operation is None or control.operation_id != operation_id
            or not phase_matches or operation.status != expected):
        raise HTTPException(status_code=409, detail="Backup operation changed; reload before recording")
    current = dict(operation.stage_receipts or {})
    if stage in current:
        raise HTTPException(status_code=409, detail="Backup stage receipt is already recorded")
    current[stage] = safe_receipt
    operation.stage_receipts = current
    control.heartbeat_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def update_stage_receipt(
    session: AsyncSession, operation_id: UUID, expected: str, stage: str,
    receipt: dict[str, object],
) -> BackupOperationRead:
    """Replace one existing sanitized receipt while reconciling a durable effect."""
    if not stage or len(stage) > 64:
        raise HTTPException(status_code=422, detail="Invalid backup stage")
    safe_receipt = _safe_stage_receipt(receipt)
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    phase_matches = control.phase == expected or (
        expected in {"resuming", "failed_recovery_required"}
        and control.phase == "idle" and control.operation_id == operation_id
    )
    if (operation is None or control.operation_id != operation_id
            or not phase_matches or operation.status != expected
            or stage not in (operation.stage_receipts or {})):
        raise HTTPException(status_code=409, detail="Backup stage receipt cannot be reconciled")
    current = dict(operation.stage_receipts or {})
    current[stage] = safe_receipt
    operation.stage_receipts = current
    control.heartbeat_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def release_operation_admission(session: AsyncSession, operation_id: UUID) -> BackupOperationRead:
    """Open ordinary writes after core recovery while retaining the reconciliation lock."""
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    if (operation is None or control.operation_id != operation_id
            or control.phase != "resuming" or operation.status != "resuming"):
        raise HTTPException(status_code=409, detail="Backup operation is not ready to release admission")
    control.phase = "idle"
    control.heartbeat_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def mark_open_recovery_required(
    session: AsyncSession, operation_id: UUID,
) -> BackupOperationRead:
    """Keep admission open but block new maintenance until effect recovery completes."""
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    if (operation is None or control.operation_id != operation_id
            or control.phase != "idle" or operation.status not in {"resuming", "failed_recovery_required"}):
        raise HTTPException(status_code=409, detail="Backup recovery state changed")
    operation.status = "failed_recovery_required"
    operation.completeness = "incomplete"
    control.heartbeat_at = datetime.now(UTC)
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def resume_operation(
    session: AsyncSession, operation_id: UUID, outcome: str, *, archive_name: str | None = None,
) -> BackupOperationRead:
    """Publish resume only after the host runner restored its prior service state."""
    if outcome not in {"completed", "incomplete", "failed", "failed_recovery_required"}:
        raise HTTPException(status_code=422, detail="Invalid backup outcome")
    await _exclusive_barrier(session)
    control = await _control(session, lock=True)
    operation = await session.scalar(select(BackupOperation).where(
        BackupOperation.id == operation_id,
    ).with_for_update())
    if (operation is None or control.operation_id != operation_id
            or control.phase not in {"resuming", "failed_recovery_required", "idle"}
            or operation.status not in {"resuming", "failed_recovery_required"}):
        raise HTTPException(status_code=409, detail="Backup operation is not ready to resume")
    if archive_name is not None and ("/" in archive_name or "\\" in archive_name or len(archive_name) > 255):
        raise HTTPException(status_code=422, detail="Invalid archive name")
    operation.status = outcome
    operation.completeness = "complete" if outcome == "completed" else "incomplete"
    operation.archive_name = archive_name
    operation.finished_at = datetime.now(UTC)
    keep_open_recovery = outcome == "failed_recovery_required" and control.phase == "idle"
    control.phase = "idle" if outcome != "failed_recovery_required" or keep_open_recovery else "failed_recovery_required"
    control.coordinator_id = None if control.phase == "idle" and outcome != "failed_recovery_required" else control.coordinator_id
    control.heartbeat_at = datetime.now(UTC)
    if control.phase == "idle" and outcome != "failed_recovery_required":
        control.operation_id = None
    await session.flush()
    return BackupOperationRead.model_validate(operation, from_attributes=True)


async def read_control(session: AsyncSession) -> BackupControlRead:
    """Return a pure status projection; the read path never initializes or rotates state."""
    control = await _control(session)
    return BackupControlRead(
        phase=control.phase, epoch=control.epoch, operation_id=control.operation_id,
        coordinator_id=control.coordinator_id, heartbeat_at=control.heartbeat_at,
        updated_at=control.updated_at, active_activities=await active_activity_count(session),
        uncertain_activities=await uncertain_activity_count(session),
        unresolved_owner_effects=await unresolved_owner_effects(session),
    )


async def read_operation(session: AsyncSession, operation_id: UUID) -> BackupOperationRead:
    """Return one owner-authorized operation using the detached credential-free schema."""
    operation = await session.get(BackupOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Backup operation not found")
    return BackupOperationRead.model_validate(operation, from_attributes=True)
