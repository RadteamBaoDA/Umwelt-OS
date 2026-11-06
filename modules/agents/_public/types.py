"""Agent public-contract constants, workflow/prompt versions and cleanup/authorization DTO dataclasses."""

from dataclasses import dataclass
from uuid import UUID

WORKFLOW_VERSION = "assistant-readonly-v1"
APPROVAL_WORKFLOW_VERSION = "assistant-approved-v1"
PROMPT_VERSION = "assistant-prompt-v1"
APPROVAL_PROMPT_VERSION = "assistant-approval-prompt-v1"
SPECIALIST_WORKFLOW_VERSION = "specialist-approved-v1"
SPECIALIST_PROMPT_VERSION = "specialist-prompt-v1"
CHECKPOINT_SCHEMA_VERSION = 1
SPECIALIST_CHECKPOINT_SCHEMA_VERSION = 2
AGENT_CLEANUP_LIMIT = 100
AGENT_INPUT_PROVENANCE_VERSION = 1
WORKFLOW_TOOLS = frozenset(
    {
        "knowledge.get_document",
        "knowledge.list_documents",
        "search.query",
        "sources.list_sources",
        "sources.get_source",
    }
)
APPROVAL_WORKFLOW_TOOLS = frozenset({*WORKFLOW_TOOLS, "webhook.send"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class BrowserRunAuthorization:
    """Detached current run/profile/session claim and bounded browser budget identity."""

    owner_id: int
    run_id: UUID
    tool_slot: int
    arguments_hash: str
    auth_session_hash: str
    conversation_id: UUID
    profile_id: str
    profile_revision_hash: str
    source_ids: frozenset[UUID]
    claim_generation: int
    remaining_jobs: int
    remaining_pages: int
    remaining_bytes: int
    remaining_active_seconds: int


@dataclass(frozen=True)
class AgentCopiedEvidenceCleanupProgress:
    """Report one bounded Agent cleanup page and whether legacy identity remains unavailable."""

    next_cursor: str | None
    complete: bool
    rows_processed: int
    unavailable: bool
    unavailable_count: int = 0
    lease_pending: bool = False
    preflight_stale: bool = False


@dataclass(frozen=True)
class AgentCleanupLeasePreflight:
    """Detached preparation for a single Agent run on the caller's still-open UoW."""

    operation_id: UUID
    scope_fingerprint: str
    candidate_run_id: UUID | None
    agent_cursor: str | None
    marker_state: str
    lease_required: bool
    lease_acquired: bool
    blocked: bool
