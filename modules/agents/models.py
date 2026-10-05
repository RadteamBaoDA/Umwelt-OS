"""Private PostgreSQL records for agent runs and bounded run activity."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class AgentRun(Base):
    """Own run identity, budgets, and the durable marker for an originally linked Chat lifetime."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_agent_runs_single_owner"),
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
        UniqueConstraint("owner_id", "auth_session_hash", "client_request_id", name="uq_agent_runs_session_request"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("owner.id", ondelete="CASCADE"), nullable=False, default=1
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
    profile_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    profile_revision_hash: Mapped[str | None] = mapped_column(String(64))
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tool_contracts: Mapped[dict[str, dict[str, str]]] = mapped_column(JSONB, nullable=False)
    source_fences: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
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
    browser_budget_reservations: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    token_usage: Mapped[int | None] = mapped_column(Integer)
    token_usage_unknown: Mapped[bool] = mapped_column(nullable=False, default=False)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    answer: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    activities: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToolCall(Base):
    """Keep bounded tool identity, immutable arguments, and outcome under the owning run."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_agent_tool_calls_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_tool_calls_ordinal"),
        CheckConstraint("status IN ('started','approval_pending','succeeded','denied','failed')", name="ck_agent_tool_calls_status"),
        CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_tool_calls_argument_bytes"),
        Index("ix_agent_tool_calls_run", "run_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(40))
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentApproval(Base):
    """Persist an immutable owner decision request tied to one run tool ordinal and exact action."""

    __tablename__ = "agent_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_agent_approvals_run_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_agent_approvals_ordinal"),
        CheckConstraint("owner_id = 1", name="ck_agent_approvals_single_owner"),
        CheckConstraint("status IN ('pending','approved','denied','expired','cancelled','requires_review')", name="ck_agent_approvals_status"),
        CheckConstraint("octet_length(arguments::text) <= 64000", name="ck_agent_approvals_argument_bytes"),
        Index("ix_agent_approvals_owner_state_expiry", "owner_id", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fences: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AgentEffect(Base):
    """Keep a no-replay action tombstone independently of its purgeable provider payload."""

    __tablename__ = "agent_effects"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','in_flight','succeeded','failed','requires_review')", name="ck_agent_effects_state"),
        CheckConstraint("payload IS NULL OR octet_length(payload::text) <= 64000", name="ck_agent_effects_payload_bytes"),
        Index("ix_agent_effects_run_created", "run_id", "created_at"),
    )

    action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    profile_alias: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    result_status_code: Mapped[int | None] = mapped_column(Integer)
    result_reference: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AgentProfile(Base):
    """Own the current editable specialist profile while preserving each immutable revision."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_agent_profiles_single_owner"),
        CheckConstraint("profile_id IN ('supervisor','knowledge','research','personal','project','news','planning','automation')", name="ck_agent_profiles_id"),
        CheckConstraint("revision >= 1", name="ck_agent_profiles_revision"),
        CheckConstraint("octet_length(prompt) <= 32000", name="ck_agent_profiles_prompt_bytes"),
        CheckConstraint("jsonb_array_length(allowed_tools) <= 32", name="ck_agent_profiles_tools_count"),
        CheckConstraint("jsonb_array_length(source_ids) <= 32", name="ck_agent_profiles_sources_count"),
    )

    profile_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, default=list)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AgentProfileRevision(Base):
    """Retain a content-addressed profile snapshot referenced by durable runs."""

    __tablename__ = "agent_profile_revisions"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_agent_profile_revisions_single_owner"),
        CheckConstraint("revision >= 1", name="ck_agent_profile_revisions_revision"),
        UniqueConstraint("profile_id", "revision", name="uq_agent_profile_revisions_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
