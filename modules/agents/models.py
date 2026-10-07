"""Private PostgreSQL records for agent runs and bounded run activity."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class AgentRun(Base):
    """Own run identity, budgets, and the durable marker for an originally linked Chat lifetime.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    Original membership/workspace-configuration epochs are a nullable positive pair with
    no defaults; NULL preserves unproven legacy authority. Execution, publication and
    reads must be quarantined by converted consumers until an original pair is proven.
    Scalar epochs survive payload redaction; admitted destructive cleanup remains possible.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "(membership_revision IS NULL AND configuration_revision IS NULL) OR "
            "(membership_revision IS NOT NULL AND configuration_revision IS NOT NULL "
            "AND membership_revision > 0 AND configuration_revision > 0)",
            name="ck_w2_agent_runs_original_epoch",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_approval','succeeded','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint("steps BETWEEN 0 AND 20", name="ck_agent_runs_steps"),
        CheckConstraint("tool_calls BETWEEN 0 AND 10", name="ck_agent_runs_tool_calls"),
        CheckConstraint("active_seconds BETWEEN 0 AND 300", name="ck_agent_runs_active_seconds"),
        CheckConstraint("browser_jobs BETWEEN 0 AND 2 AND browser_pages BETWEEN 0 AND 6 AND browser_bytes BETWEEN 0 AND 10485760", name="ck_agent_runs_browser_budget"),
        CheckConstraint("token_budget IS NULL OR token_budget BETWEEN 1 AND 2000000", name="ck_agent_runs_token_budget"),
        CheckConstraint("dispatch_generation >= 1 AND claim_generation >= 0", name="ck_agent_runs_generations"),
        CheckConstraint("length(workflow_version) BETWEEN 1 AND 40", name="ck_agent_runs_workflow_version"),
        Index("ix_agent_runs_dispatch", "status", "updated_at"),
        Index("ix_agent_runs_owner_created", "owner_id", "created_at", "id"),
        Index("ix_agent_runs_trace_retention", "trace_redacted_at", "status", "completed_at", "id"),
        Index("ix_agent_runs_source_fences_gin", "source_fences", postgresql_using="gin"),
        UniqueConstraint("workspace_id", "owner_id", "auth_session_hash", "client_request_id", name="uq_agent_runs_session_request"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_runs_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_agent_runs_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_agent_runs_id"),
        Index("ix_w2_agent_runs_scope", 'workspace_id', 'id'),
        Index("ix_w2_agent_runs_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    configuration_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    auth_session_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(40), nullable=False, default="assistant")
    workflow_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    checkpoint_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_thread_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    profile_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    profile_revision_hash: Mapped[str | None] = mapped_column(String(64))
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tool_contracts: Mapped[dict[str, dict[str, str]]] = mapped_column(JSONB, nullable=False)
    source_fences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # This durable denial survives any later selective scrub of the evidence fence itself.
    evidence_revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Retain the original link requirement after Chat's cascading link row is deleted.
    chat_link_required: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    browser_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    browser_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    browser_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    browser_budget_reservations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    token_usage: Mapped[int | None] = mapped_column(Integer)
    token_usage_unknown: Mapped[bool] = mapped_column(nullable=False, default=False)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    answer: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    activities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class AgentToolCall(Base):
    """Keep bounded tool identity, immutable arguments, and exact model-input evidence provenance."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_agent_tool_calls_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_tool_calls_ordinal"),
        CheckConstraint("status IN ('started','approval_pending','succeeded','denied','failed')", name="ck_agent_tool_calls_status"),
        CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_tool_calls_argument_bytes"),
        CheckConstraint(
            "input_provenance_version IS NULL OR input_provenance_version = 1",
            name="ck_agent_tool_calls_input_provenance_version",
        ),
        CheckConstraint(
            "(input_provenance_version IS NULL AND input_source_fences IS NULL) OR "
            "(input_provenance_version = 1 AND input_source_fences IS NOT NULL "
            "AND octet_length(input_source_fences::text) <= 64000)",
            name="ck_agent_tool_calls_input_provenance_shape",
        ),
        Index("ix_agent_tool_calls_run", "run_id", "ordinal"),
        Index("ix_agent_tool_calls_input_fences_gin", "input_source_fences", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(40))
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # NULL means legacy provenance was never captured; an empty strict fence is evidence-free.
    input_source_fences: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_provenance_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentApproval(Base):
    """Persist an immutable owner decision request tied to one run tool ordinal and exact action.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "agent_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_agent_approvals_run_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_approvals_ordinal"),
        CheckConstraint("status IN ('pending','approved','denied','expired','cancelled','requires_review')", name="ck_agent_approvals_status"),
        CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_approvals_argument_bytes"),
        Index("ix_agent_approvals_owner_state_expiry", "owner_id", "status", "expires_at"),
        Index("ix_agent_approvals_retention", "run_id", "status"),
        Index("ix_agent_approvals_source_fences_gin", "source_fences", postgresql_using="gin"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_approvals_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_agent_approvals_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["agent_runs.workspace_id", "agent_runs.id"], name="fk_w2_agent_approvals_run_id", ondelete="CASCADE"),
        Index("ix_w2_agent_approvals_scope", 'workspace_id', 'id'),
        Index("ix_w2_agent_approvals_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class AgentEvidenceCleanup(Base):
    """Retain an operation-scoped revocation and lease-finalization receipt for one run.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "agent_evidence_cleanups"
    __table_args__ = (
        UniqueConstraint("operation_id", "run_id", name="uq_agent_evidence_cleanups_operation_run"),
        CheckConstraint("state IN ('pending','finalized','unavailable')", name="ck_agent_evidence_cleanups_state"),
        CheckConstraint("matched_identity IS NULL OR octet_length(matched_identity::text) <= 2048", name="ck_agent_evidence_cleanups_identity_bytes"),
        Index("ix_agent_evidence_cleanups_operation_state_run", "operation_id", "state", "run_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_evidence_cleanups_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["agent_runs.workspace_id", "agent_runs.id"], name="fk_w2_agent_evidence_cleanups_run_id", ondelete="CASCADE"),
        Index("ix_w2_agent_evidence_cleanups_scope", 'workspace_id', 'id'),
        Index("ix_w2_agent_evidence_cleanups_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False,
    )
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # First exact immutable version/chunk identity that proved this run matched the operation.
    # NULL records an operation-level coverage gate where no exact run dependency was proven.
    matched_identity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class AgentEffect(Base):
    """Keep a no-replay action tombstone independently of its purgeable provider payload.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "agent_effects"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','in_flight','succeeded','failed','requires_review')", name="ck_agent_effects_state"),
        CheckConstraint("payload IS NULL OR octet_length(payload::text) <= 64000", name="ck_agent_effects_payload_bytes"),
        Index("ix_agent_effects_run_created", "run_id", "created_at"),
        Index("ix_agent_effects_retention", "run_id", "state"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_effects_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_agent_effects_principal", ondelete="RESTRICT"),
        Index("ix_w2_agent_effects_scope", 'workspace_id', 'action_id'),
        Index("ix_w2_agent_effects_work", 'workspace_id', 'created_at', 'action_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)


    action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    profile_alias: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    result_status_code: Mapped[int | None] = mapped_column(Integer)
    result_reference: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class AgentProfile(Base):
    """Own the current editable specialist profile while preserving each immutable revision.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "agent_profiles"
    __table_args__ = (
        CheckConstraint("profile_id IN ('supervisor','knowledge','research','personal','project','news','planning','automation')", name="ck_agent_profiles_id"),
        CheckConstraint("revision >= 1", name="ck_agent_profiles_revision"),
        CheckConstraint("octet_length(prompt) <= 32000", name="ck_agent_profiles_prompt_bytes"),
        CheckConstraint("jsonb_array_length(allowed_tools) <= 32", name="ck_agent_profiles_tools_count"),
        CheckConstraint("jsonb_array_length(source_ids) <= 32", name="ck_agent_profiles_sources_count"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_profiles_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_agent_profiles_principal", ondelete="RESTRICT"),
        Index("ix_w2_agent_profiles_work", 'workspace_id', 'updated_at', 'profile_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)


    profile_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, default=list)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class AgentProfileRevision(Base):
    """Retain a content-addressed profile snapshot referenced by durable runs.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "agent_profile_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_agent_profile_revisions_revision"),
        UniqueConstraint("workspace_id", "profile_id", "revision", name="uq_agent_profile_revisions_version"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_agent_profile_revisions_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_agent_profile_revisions_principal", ondelete="RESTRICT"),
        Index("ix_w2_agent_profile_revisions_scope", 'workspace_id', 'id'),
        Index("ix_w2_agent_profile_revisions_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

