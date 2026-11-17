"""Pure receipt-row rewrites for failed document cleanup stages (private to the worker)."""

from collections.abc import Callable

from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.knowledge.documents.public import SourceCleanupWakeup


def fail_copied_stage(
    operation: DocumentCleanupOperation,
    stage: str,
    code: str,
    *,
    reset: bool,
) -> None:
    """Record a retryable failure of one materialization/brief stage; a reset also restarts its cursor.

    A cursor reset zeroes the stage's unresolved count because the restart recounts from scratch.
    """
    setattr(operation, f"{stage}_status", "failed")
    setattr(operation, f"{stage}_error_code", code)
    if reset:
        setattr(operation, f"{stage}_cursor", None)
        setattr(operation, f"{stage}_unresolved_count", 0)
    operation.copied_status = "failed"
    operation.copied_error_code = code
    operation.status = "failed"
    operation.error_code = code


def rewrite_recovered_stage(
    operation: DocumentCleanupOperation,
    stage: str,
    code: str,
    malformed: bool,
    hints: list[SourceCleanupWakeup],
    record_hint: Callable[[list[SourceCleanupWakeup], DocumentCleanupOperation, str], None],
) -> bool:
    """Mark the failed cleanup stage on a locked receipt and queue its recovery wakeup.

    Pure row mutation (no I/O): rewrites the stage-specific status/error fields, resets the
    cursor when ``malformed``, appends a wakeup hint through ``record_hint``, and returns ``False`` when every stage is
    already terminal so no delivery reschedule is owed.

    """
    changed = True
    if (
        stage == "agent"
        and operation.agent_status != "succeeded"
        and not (
            operation.agent_status == "failed"
            and operation.agent_error_code == "evidence_identity_unavailable"
        )
    ):
        operation.agent_status = "queued"
        operation.agent_error_code = f"agent_{code}"
        if malformed:
            operation.agent_cursor = None
            operation.agent_waiting_for_lease = False
        operation.copied_status = "failed"
        operation.copied_error_code = f"agent_{code}"
        operation.status = "failed"
        operation.error_code = f"agent_{code}"
        record_hint(hints, operation, "agent-recovery")
    elif stage in {"materialization", "brief"}:
        fail_copied_stage(operation, stage, f"{stage}_{code}", reset=malformed)
        record_hint(hints, operation, f"{stage}-recovery")
    elif (
        stage == "memory"
        and operation.memory_status not in {"succeeded"}
        and operation.memory_error_code
        not in {
            "legacy_provenance_unresolved",
            "evidence_identity_unavailable",
        }
    ):
        operation.memory_status = "failed"
        operation.memory_error_code = f"memory_{code}"
        if malformed:
            operation.memory_cursor = None
            operation.memory_unresolved_count = 0
        operation.copied_status = "failed"
        operation.copied_error_code = f"memory_{code}"
        operation.status = "failed"
        operation.error_code = f"memory_{code}"
        record_hint(hints, operation, "memory-recovery")
    elif operation.chat_status != "succeeded":
        if malformed:
            operation.copied_cursor = None
        operation.chat_status = "failed"
        operation.chat_error_code = f"chat_{code}"
        operation.copied_status = "failed"
        operation.copied_error_code = f"chat_{code}"
        operation.status = "failed"
        operation.error_code = f"chat_{code}"
        record_hint(hints, operation, "chat-recovery")
    elif operation.raw_status not in {"not_present", "retained_shared", "succeeded"}:
        pass  # Only the delivery reschedule below; no stage state to rewrite.
    else:
        changed = False  # Terminal stages do not own a delivery rewrite.
    return changed
