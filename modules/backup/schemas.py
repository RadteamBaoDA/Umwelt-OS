"""Credential-free contracts for backup control, operations, and admission."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


BackupPhase = Literal["idle", "draining", "quiesced", "snapshotting", "resuming", "failed_recovery_required"]
BackupStatus = Literal["pending", "draining", "quiesced", "snapshotting", "resuming", "completed", "incomplete", "failed", "failed_recovery_required"]


class AdmissionReceipt(BaseModel):
    """Identify the epoch admitted by the transaction-scoped shared barrier."""

    model_config = ConfigDict(frozen=True)

    epoch: int = Field(ge=1)
    phase: BackupPhase


class ActivityReceipt(BaseModel):
    """Carry the durable activity row identity needed for terminal publication."""

    model_config = ConfigDict(frozen=True)

    activity_id: UUID
    epoch: int = Field(ge=1)
    kind: str


class UnresolvedEffectRead(BaseModel):
    """Expose a bounded owner reason/count without leaking journal payloads."""

    owner: Literal["agents", "automations", "connectors", "tools", "temporal"]
    reason: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=1, le=1_000_000)


class BackupControlRead(BaseModel):
    """Expose the current maintenance state without exposing local paths or secrets."""

    phase: BackupPhase
    epoch: int = Field(ge=1)
    operation_id: UUID | None = None
    coordinator_id: str | None = None
    heartbeat_at: datetime | None = None
    updated_at: datetime
    active_activities: int = Field(ge=0)
    uncertain_activities: int = Field(ge=0)
    unresolved_owner_effects: list[UnresolvedEffectRead] = Field(max_length=40)


class BackupOperationRead(BaseModel):
    """Expose a redacted durable operation summary and actual component outcomes."""

    id: UUID
    epoch: int = Field(ge=1)
    status: BackupStatus
    consistency: Literal["quiesced"]
    completeness: Literal["complete", "incomplete"]
    archive_name: str | None = None
    stage_receipts: dict[str, object]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
