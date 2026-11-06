"""Owner-scoped agent run creation, lookup, and cancellation contracts."""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING
from typing import Any as _Any

from core.tools import ToolRisk as ToolRisk
from modules.agents._public_approvals import (
    _approval_fences_current as _approval_fences_current,
)
from modules.agents._public_approvals import (
    _approval_run_is_revoked as _approval_run_is_revoked,
)
from modules.agents._public_approvals import (
    _invalidate_stale_approval as _invalidate_stale_approval,
)
from modules.agents._public_approvals import (
    get_approval as get_approval,
)
from modules.agents._public_approvals import (
    list_conversation_approvals as list_conversation_approvals,
)
from modules.agents._public_budget import (
    _browser_profile_current as _browser_profile_current,
)
from modules.agents._public_budget import (
    _epoch_current as _epoch_current,
)
from modules.agents._public_budget import (
    lock_write_admission as lock_write_admission,
)
from modules.agents._public_budget import (
    publish_agent_activity_safely as publish_agent_activity_safely,
)
from modules.agents._public_budget import (
    reserve_browser_run_budget_in_uow as reserve_browser_run_budget_in_uow,
)
from modules.agents._public_budget import (
    revalidate_browser_run_authority as revalidate_browser_run_authority,
)
from modules.agents._public_cleanup import (
    _admit_cleanup_evidence as _admit_cleanup_evidence,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_candidate_ids as _agent_cleanup_candidate_ids,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_fingerprint as _agent_cleanup_fingerprint,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_ledgers as _agent_cleanup_ledgers,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_marker_state as _agent_cleanup_marker_state,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_records as _agent_cleanup_records,
)
from modules.agents._public_cleanup import (
    _agent_cleanup_scope_fingerprint as _agent_cleanup_scope_fingerprint,
)
from modules.agents._public_cleanup import (
    _agent_scope_contains as _agent_scope_contains,
)
from modules.agents._public_cleanup import (
    _agent_unavailable_run_count as _agent_unavailable_run_count,
)
from modules.agents._public_cleanup import (
    _cleanup_existing_marker_phase_a as _cleanup_existing_marker_phase_a,
)
from modules.agents._public_cleanup import (
    _cleanup_finalized_marker as _cleanup_finalized_marker,
)
from modules.agents._public_cleanup import (
    _cleanup_one_run as _cleanup_one_run,
)
from modules.agents._public_cleanup import (
    _cleanup_phase_a as _cleanup_phase_a,
)
from modules.agents._public_cleanup import (
    _cleanup_phase_b as _cleanup_phase_b,
)
from modules.agents._public_cleanup import (
    _cleanup_unavailable_marker as _cleanup_unavailable_marker,
)
from modules.agents._public_cleanup import (
    _CleanupPass as _CleanupPass,
)
from modules.agents._public_cleanup import (
    _decode_agent_cleanup_cursor as _decode_agent_cleanup_cursor,
)
from modules.agents._public_cleanup import (
    _encode_agent_cleanup_cursor as _encode_agent_cleanup_cursor,
)
from modules.agents._public_cleanup import (
    _fences_match_agent_scope as _fences_match_agent_scope,
)
from modules.agents._public_cleanup import (
    _find_agent_scope_match as _find_agent_scope_match,
)
from modules.agents._public_cleanup import (
    _revoke_agent_approval as _revoke_agent_approval,
)
from modules.agents._public_cleanup import (
    _revoke_run_for_cleanup as _revoke_run_for_cleanup,
)
from modules.agents._public_cleanup import (
    _scrub_agent_scope_payloads as _scrub_agent_scope_payloads,
)
from modules.agents._public_cleanup import (
    _strict_agent_fences as _strict_agent_fences,
)
from modules.agents._public_cleanup import (
    preflight_document_copied_evidence_lease as preflight_document_copied_evidence_lease,
)
from modules.agents._public_cleanup import (
    purge_document_copied_evidence_page as purge_document_copied_evidence_page,
)
from modules.agents._public_lifecycle import (
    current_profile_revision as current_profile_revision,
)
from modules.agents._public_lifecycle import (
    purge_agent_runs as purge_agent_runs,
)
from modules.agents._public_lifecycle import (
    purge_conversation_actions as purge_conversation_actions,
)
from modules.agents._public_lifecycle import (
    request_cancel as request_cancel,
)
from modules.agents._public_lifecycle import (
    request_cancel_for_owner as request_cancel_for_owner,
)
from modules.agents._public_runs import (
    _read as _read,
)
from modules.agents._public_runs import (
    _read_current_result as _read_current_result,
)
from modules.agents._public_runs import (
    _read_current_results as _read_current_results,
)
from modules.agents._public_runs import (
    _restore_fences as _restore_fences,
)
from modules.agents._public_runs import (
    _result_principal as _result_principal,
)
from modules.agents._public_runs import (
    create_profile_run_in_uow as create_profile_run_in_uow,
)
from modules.agents._public_runs import (
    create_run as create_run,
)
from modules.agents._public_runs import (
    get_run as get_run,
)
from modules.agents._public_runs import (
    get_run_for_owner as get_run_for_owner,
)
from modules.agents._public_runs import (
    list_runs as list_runs,
)
from modules.agents._public_traces import (
    get_run_meta_by_id as get_run_meta_by_id,
)
from modules.agents._public_traces import (
    list_agent_trace_workspace_ids as list_agent_trace_workspace_ids,
)
from modules.agents._public_traces import (
    list_run_meta as list_run_meta,
)
from modules.agents._public_traces import (
    redact_expired_agent_traces as redact_expired_agent_traces,
)
from modules.agents._public_types import (
    AGENT_CLEANUP_LIMIT as AGENT_CLEANUP_LIMIT,
)
from modules.agents._public_types import (
    AGENT_INPUT_PROVENANCE_VERSION as AGENT_INPUT_PROVENANCE_VERSION,
)
from modules.agents._public_types import (
    APPROVAL_PROMPT_VERSION as APPROVAL_PROMPT_VERSION,
)
from modules.agents._public_types import (
    APPROVAL_WORKFLOW_TOOLS as APPROVAL_WORKFLOW_TOOLS,
)
from modules.agents._public_types import (
    APPROVAL_WORKFLOW_VERSION as APPROVAL_WORKFLOW_VERSION,
)
from modules.agents._public_types import (
    CHECKPOINT_SCHEMA_VERSION as CHECKPOINT_SCHEMA_VERSION,
)
from modules.agents._public_types import (
    PROMPT_VERSION as PROMPT_VERSION,
)
from modules.agents._public_types import (
    SPECIALIST_CHECKPOINT_SCHEMA_VERSION as SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
)
from modules.agents._public_types import (
    SPECIALIST_PROMPT_VERSION as SPECIALIST_PROMPT_VERSION,
)
from modules.agents._public_types import (
    SPECIALIST_WORKFLOW_VERSION as SPECIALIST_WORKFLOW_VERSION,
)
from modules.agents._public_types import (
    TERMINAL_STATUSES as TERMINAL_STATUSES,
)
from modules.agents._public_types import (
    WORKFLOW_TOOLS as WORKFLOW_TOOLS,
)
from modules.agents._public_types import (
    WORKFLOW_VERSION as WORKFLOW_VERSION,
)
from modules.agents._public_types import (
    AgentCleanupLeasePreflight as AgentCleanupLeasePreflight,
)
from modules.agents._public_types import (
    AgentCopiedEvidenceCleanupProgress as AgentCopiedEvidenceCleanupProgress,
)
from modules.agents._public_types import (
    BrowserRunAuthorization as BrowserRunAuthorization,
)
from modules.agents.access import read_workspace_modules
from modules.agents.account_checkpoints import (
    erase_account_checkpoints_page,
)
from modules.agents.internal_writes import run_approved_write, uuid_arg
from modules.agents.schemas import (
    AgentActivity,
    AgentProfilePatch,
    AgentProfileRead,
    AgentProfileTool,
    AgentRunPage,
    AgentRunRead,
    AgentRunStart,
    ApprovalDecisionRead,
    ApprovalDecisionRequest,
    ApprovalRead,
    ProfileRunStart,
)

# Re-exports of private submodules that themselves import this facade are resolved lazily (PEP 562) so the two cannot
# form an import cycle; type checkers see them through the TYPE_CHECKING block at the end of the module.
_LAZY_DTO_MODULES = {
    "mark_effect_outcome": "modules.agents.approvals",
}


def __getattr__(name: str) -> _Any:
    """Resolve the lazily re-exported names on first access (module-level ``__getattr__``).

    Raises ``AttributeError`` for any other missing attribute, preserving normal module semantics.
    """
    module_name = _LAZY_DTO_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "APPROVAL_PROMPT_VERSION",
    "APPROVAL_WORKFLOW_TOOLS",
    "APPROVAL_WORKFLOW_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "SPECIALIST_CHECKPOINT_SCHEMA_VERSION",
    "SPECIALIST_PROMPT_VERSION",
    "SPECIALIST_WORKFLOW_VERSION",
    "WORKFLOW_TOOLS",
    "WORKFLOW_VERSION",
    "AgentActivity",
    "AgentCleanupLeasePreflight",
    "AgentCopiedEvidenceCleanupProgress",
    "AgentProfilePatch",
    "AgentProfileRead",
    "AgentProfileTool",
    "AgentRunPage",
    "AgentRunRead",
    "AgentRunStart",
    "ApprovalDecisionRead",
    "ApprovalDecisionRequest",
    "ApprovalRead",
    "BrowserRunAuthorization",
    "ProfileRunStart",
    "create_profile_run_in_uow",
    "create_run",
    "current_profile_revision",
    "erase_account_checkpoints_page",
    "get_approval",
    "get_run",
    "get_run_for_owner",
    "get_run_meta_by_id",
    "list_agent_trace_workspace_ids",
    "list_conversation_approvals",
    "list_run_meta",
    "list_runs",
    "lock_write_admission",
    "mark_effect_outcome",
    "preflight_document_copied_evidence_lease",
    "purge_agent_runs",
    "purge_conversation_actions",
    "purge_document_copied_evidence_page",
    "read_workspace_modules",
    "redact_expired_agent_traces",
    "request_cancel",
    "request_cancel_for_owner",
    "reserve_browser_run_budget_in_uow",
    "revalidate_browser_run_authority",
    "run_approved_write",
    "uuid_arg",
]


if _TYPE_CHECKING:
    from modules.agents.approvals import (
        mark_effect_outcome,
    )
