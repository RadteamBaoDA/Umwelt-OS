"""Pure progress/terminal-state predicates and constants for document cleanup receipts, behind ``modules.knowledge.documents.worker``."""

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from modules.knowledge.documents.models import DocumentCleanupOperation

_RETRY_DELAY = timedelta(seconds=30)
_CONTINUATION_DELAY = timedelta(seconds=1)
# Failures with these codes are truthful terminal limitations (unavailable lineage); they never retry.
_COPIED_STAGE_TERMINAL_CODES = frozenset(
    {
        "evidence_identity_unavailable",
        "legacy_provenance_unresolved",
        "legacy_coverage_unavailable",
    }
)
# A raw URI that is not a valid storage path can never be cleaned by retrying: terminal, not retried.
_RAW_URI_UNAVAILABLE = "raw_uri_unavailable"
_MATERIALIZATION_PHASES = ("notifications", "triggers", "runs")


def _source_cleanup_progress_key(operation: DocumentCleanupOperation, stage: str) -> str:
    """Build a bounded deterministic Source event key from non-sensitive child-stage state."""
    state = (
        operation.raw_status,
        operation.error_code,
        operation.evidence_scope_status,
        operation.copied_status,
        operation.copied_error_code,
        operation.chat_status,
        operation.chat_error_code,
        operation.memory_status,
        operation.memory_error_code,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
        operation.materialization_status,
        operation.materialization_error_code,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        operation.brief_unresolved_count,
    )
    fingerprint = hashlib.sha256(
        json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode(),
    ).hexdigest()[:32]
    return f"document-cleanup:{stage}:{fingerprint}"


def _attempt_progress_snapshot(operation: DocumentCleanupOperation) -> tuple[object, ...]:
    """Capture the receipt fields recovery compares before writing stage errors.

    Callers may pass an unlocked read (Agent preflight) or the locked receipt; recovery
    re-compares under the receipt lock so a changed receipt is never overwritten.
    """
    return (
        operation.raw_status,
        operation.evidence_scope_status,
        operation.chat_status,
        operation.copied_status,
        dict(operation.copied_cursor)
        if isinstance(operation.copied_cursor, dict)
        else operation.copied_cursor,
        operation.memory_status,
        operation.memory_error_code,
        dict(operation.memory_cursor)
        if isinstance(operation.memory_cursor, dict)
        else operation.memory_cursor,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        dict(operation.agent_cursor)
        if isinstance(operation.agent_cursor, dict)
        else operation.agent_cursor,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
        operation.materialization_status,
        operation.materialization_error_code,
        dict(operation.materialization_cursor)
        if isinstance(operation.materialization_cursor, dict)
        else operation.materialization_cursor,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        dict(operation.brief_cursor)
        if isinstance(operation.brief_cursor, dict)
        else operation.brief_cursor,
        operation.brief_unresolved_count,
    )


def _raw_stage_done(operation: DocumentCleanupOperation) -> bool:
    """Return whether the raw stage is settled, including a terminal unavailable-URI failure."""
    return operation.raw_status in {"not_present", "retained_shared", "succeeded"} or (
        operation.raw_status == "failed" and operation.error_code == _RAW_URI_UNAVAILABLE
    )


def _copied_stage_terminal(status: str, error_code: str | None) -> bool:
    """Return whether a materialization/brief stage is finished, including unavailable limits."""
    return status == "succeeded" or (
        status == "failed" and error_code in _COPIED_STAGE_TERMINAL_CODES
    )


def _optional_uuid(value: object, label: str) -> UUID | None:
    """Parse one stored cursor component, rejecting anything but a canonical UUID string or null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Stored {label} cursor is malformed")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError(f"Stored {label} cursor is malformed")
    return parsed


def _memory_terminal(operation: DocumentCleanupOperation) -> bool:
    """Return whether the Memory stage is final: succeeded, or failed with an unretryable lineage code."""
    return operation.memory_status == "succeeded" or (
        operation.memory_status == "failed"
        and operation.memory_error_code
        in {
            "legacy_provenance_unresolved",
            "evidence_identity_unavailable",
        }
    )


def _receipt_fully_settled(operation: DocumentCleanupOperation) -> bool:
    """Return whether every stage is terminal and the receipt is already final (duplicate delivery)."""
    return (
        operation.raw_status in {"not_present", "retained_shared", "succeeded"}
        and operation.chat_status == "succeeded"
        and _memory_terminal(operation)
        and operation.agent_status in {"succeeded", "failed"}
        and _copied_stage_terminal(
            operation.materialization_status, operation.materialization_error_code
        )
        and _copied_stage_terminal(operation.brief_status, operation.brief_error_code)
        and not operation.memory_cache_pending
        and operation.status in {"succeeded", "failed"}
    )
