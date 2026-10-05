"""Public request and response DTOs for the single bounded agent workflow."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentRunStart(BaseModel):
    """Accept bounded task text and optional token/conversation settings; identity and tools remain server-derived."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8_000)
    token_budget: int | None = Field(default=None, ge=1, le=2_000_000)
    conversation_id: UUID | None = None


class AgentActivity(BaseModel):
    """Expose a bounded status record containing tool identity but no arguments or result data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["step", "tool", "status"]
    status: str = Field(min_length=1, max_length=32)
    tool_name: str | None = Field(default=None, max_length=160)
    created_at: datetime


class AgentRunRead(BaseModel):
    """Return bounded owner run state and disclose when configured token-budget enforcement is unavailable."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    agent_id: str
    status: Literal["queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"]
    answer: str | None = None
    error_code: str | None = None
    steps: int
    tool_calls: int
    active_seconds: int
    token_usage: int | None
    token_budget: int | None
    token_budget_available: bool
    token_usage_unknown: bool
    profile_id: str | None = None
    profile_revision_hash: str | None = None
    activities: list[AgentActivity]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class AgentProfileTool(BaseModel):
    """Identify one exact currently registered tool contract selected by an owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=40)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentProfileRead(BaseModel):
    """Expose bounded editable settings and server-derived capability availability."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    title: str
    enabled: bool
    revision: int
    model_alias: str
    prompt: str
    allowed_tools: list[AgentProfileTool]
    available_tools: list[AgentProfileTool]
    source_ids: list[UUID]
    capability: Literal["available", "partial", "unavailable"]
    unavailable_reasons: list[str]
    limits: dict[str, int]


class AgentProfilePatch(BaseModel):
    """Validate owner profile edits without allowing arbitrary tools, endpoints, or credentials."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool
    model_alias: str = Field(min_length=1, max_length=64)
    prompt: str = Field(max_length=8_000)
    allowed_tools: list[AgentProfileTool] = Field(max_length=32)
    source_ids: list[UUID] = Field(max_length=32)


class ProfileRunStart(BaseModel):
    """Bind one profile run to a client retry key, selected revision, and existing Chat thread."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8_000)
    expected_profile_revision: int = Field(ge=0)
    conversation_id: UUID
    client_request_id: str = Field(min_length=1, max_length=128)
    token_budget: int | None = Field(default=None, ge=1, le=2_000_000)


class AgentRunPage(BaseModel):
    """Return a bounded page of runs with an opaque continuation cursor."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[AgentRunRead]
    next_cursor: str | None


class ApprovalRead(BaseModel):
    """Show the exact action while its evidence is current, or redact arguments when its fences expire."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    action_id: UUID
    run_id: UUID
    conversation_id: UUID
    tool_name: str
    tool_version: str
    arguments: dict[str, Any] | None
    argument_hash: str
    destination_id: str
    destination_revision: str
    status: Literal["pending", "approved", "denied", "expired", "cancelled", "requires_review"]
    effect_status: Literal["reserved", "in_flight", "succeeded", "failed", "requires_review"] | None = None
    result_reference: str | None = None
    created_at: datetime
    expires_at: datetime


class ApprovalDecisionRequest(BaseModel):
    """Allow only optimistic digest matching; action identity and content remain server-derived."""

    model_config = ConfigDict(extra="forbid")
    expected_argument_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ApprovalDecisionRead(BaseModel):
    """Return the durable resolution state without provider response bodies or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: Literal["approved", "denied", "expired", "cancelled", "requires_review"]
    run_status: Literal["queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"]
