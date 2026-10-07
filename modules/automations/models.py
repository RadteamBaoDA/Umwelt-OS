"""Private persistence for automation rules and their immutable revisions."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Automation(Base):
    """Mutable rule head: identity, pause flag and current revision number.

    The definition itself lives only in ``AutomationRevision`` so a run can always
    cite the exact revision that fired. Deletion is soft to keep run history valid.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "automations"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_automations_revision"),
        Index("ix_automations_owner", "owner_id", "deleted_at"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_automations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_automations_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_automations_id"),
        Index("ix_w2_automations_scope", 'workspace_id', 'id'),
        Index("ix_w2_automations_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class AutomationRevision(Base):
    """Append-only snapshot of a rule definition; rows are never updated or deleted by the app."""

    __tablename__ = "automation_revisions"
    __table_args__ = (
        UniqueConstraint("automation_id", "revision", name="uq_automation_revisions_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    automation_id: Mapped[UUID] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trigger: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AutomationTrigger(Base):
    """Durable inbox of trigger events offered by producers; deduped on (owner, type, event key).

    ``depth`` and the origin columns carry the causal chain of events that an automation itself
    caused (0 = not caused by an automation), which bounds loops across modules.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "automation_triggers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "owner_id", "trigger_type", "event_key", name="uq_automation_triggers_event"),
        CheckConstraint("status IN ('pending','processed')", name="ck_automation_triggers_status"),
        CheckConstraint("depth BETWEEN 0 AND 50", name="ck_automation_triggers_depth"),
        Index("ix_automation_triggers_pending", "status", "created_at"),
        Index("ix_automation_triggers_document", "document_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_automation_triggers_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_automation_triggers_principal", ondelete="RESTRICT"),
        Index("ix_w2_automation_triggers_scope", 'workspace_id', 'id'),
        Index("ix_w2_automation_triggers_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Private provenance stays outside the condition payload whitelist and survives inbox processing.
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_evidence_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin_automation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    origin_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())



class AutomationSchedule(Base):
    """Per-rule internal schedule slot state: explicit timezone, next slot and misfire policy."""

    __tablename__ = "automation_schedules"
    __table_args__ = (
        CheckConstraint("misfire_policy IN ('coalesce')", name="ck_automation_schedules_misfire"),
        Index("ix_automation_schedules_next", "next_slot"),
    )

    automation_id: Mapped[UUID] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    next_slot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_slot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="coalesce")


class AutomationRun(Base):
    """One execution, identified by (automation, immutable revision, trigger key).

    The unique identity is the dedupe fence: a duplicate event, retry or double slot insert
    conflicts instead of creating a second run. ``revision`` cites the exact snapshot.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("automation_id", "revision", "trigger_key", name="uq_automation_runs_identity"),
        CheckConstraint(
            "status IN ('queued','running','awaiting_approval','succeeded','failed','skipped','dropped','requires_review')",
            name="ck_automation_runs_status"),
        CheckConstraint("depth BETWEEN 1 AND 50", name="ck_automation_runs_depth"),
        Index("ix_automation_runs_dispatch", "status", "next_attempt_at"),
        Index("ix_automation_runs_rule", "automation_id", "created_at"),
        Index("ix_automation_runs_document", "document_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_automation_runs_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_automation_runs_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "automation_id"], ["automations.workspace_id", "automations.id"], name="fk_w2_automation_runs_automation_id", ondelete="RESTRICT"),
        Index("ix_w2_automation_runs_scope", 'workspace_id', 'id'),
        Index("ix_w2_automation_runs_work", 'workspace_id', 'status', 'next_attempt_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    automation_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_key: Mapped[str] = mapped_column(String(240), nullable=False)
    trigger_event_id: Mapped[str | None] = mapped_column(String(200))
    scheduled_slot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    origin_automation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    origin_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    reason: Mapped[str | None] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Private provenance tracks the copied condition metadata through run admission and cleanup.
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_evidence_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class AutomationRunAction(Base):
    """Persisted outcome of one ordered action in a run; the per-action no-replay ledger."""

    __tablename__ = "automation_run_actions"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_automation_run_actions_slot"),
        Index("ix_automation_run_actions_reference", "result_reference"),
        CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_automation_run_actions_ordinal"),
        CheckConstraint(
            "status IN ('pending','awaiting_approval','approved','in_flight','succeeded','failed',"
            "'denied','skipped','requires_review')", name="ck_automation_run_actions_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48))
    result_reference: Mapped[str | None] = mapped_column(String(256))
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    destination_revision: Mapped[str | None] = mapped_column(String(64))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_session_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AutomationCursor(Base):
    """Durable position of one producer sweep: the last ``(ts, id)`` it handed to the trigger inbox.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "automation_cursors"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_automation_cursors_workspace", ondelete="RESTRICT"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)


    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))



class AutomationWebhookCredential(Base):
    """Hash-only bearer slot for one owner-configured inbound alias.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "automation_webhook_credentials"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="ck_automation_webhook_credentials_hash"),
        CheckConstraint("revision >= 1", name="ck_automation_webhook_credentials_revision"),
        Index("ix_automation_webhook_credentials_expiry", "expires_at"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_automation_webhook_credentials_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_automation_webhook_credentials_principal", ondelete="RESTRICT"),
        Index("ix_w2_automation_webhook_credentials_scope", 'workspace_id', 'owner_id', 'alias'),
        Index("ix_w2_automation_webhook_credentials_work", 'workspace_id', 'created_at', 'owner_id', 'alias'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    alias: Mapped[str] = mapped_column(String(40), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

