"""Automation execution constants, module-availability cache, cleanup progress DTO and run errors shared by the private execution submodules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

MAX_DEPTH = 5  # chain depth of automation-caused events
MAX_ACTIONS = 10
COOLDOWN_SECONDS = 60  # per-rule default between non-scheduled runs
MAX_RUNS_PER_HOUR = 30  # per-rule rate cap (scheduled runs included)
MAX_RUN_ATTEMPTS = 4  # worker passes over one run, including restarts
MAX_ACTION_ATTEMPTS = 3  # transient retries of one retry-safe action
DISPATCH_STALE_AFTER = timedelta(seconds=30)
RUNNING_STALE_AFTER = timedelta(seconds=200)  # longer than the 180 s ARQ job timeout
APPROVAL_ACTIONS = frozenset({"run_agent", "call_webhook"})
# Module that must be registered and enabled for each trigger / action type.
TRIGGER_MODULE = {
    "schedule": None,
    "new_event": "knowledge.timeline",
    "new_document": "knowledge.documents",
    "entity_changed": "knowledge.entities",
    "task_due": "tasks",
    "goal_deadline": "goals",
    "webhook": "tools",
    "connector_sync_result": "sources",
}
ACTION_MODULE = {
    "run_agent": "agents",
    "create_task": "tasks",
    "create_notification": "notifications",
    "generate_brief": "dashboard",
    "call_webhook": "tools",
}
_modules_cache: dict[str, Any] = {}
_TERMINAL_RUN = ("succeeded", "failed", "skipped", "dropped", "requires_review")


@dataclass(frozen=True)
class AutomationCleanupProgress:
    """Return bounded cleanup progress keyed by operation and stable owner-row classifications.

    Provisional unavailable IDs may be resolved by later reference pages; only IDs in
    ``unavailable_ids`` are terminal for the caller's operation. No copied payload is exposed.
    """

    next_cursor: UUID | None
    complete: bool
    changed_count: int
    operation_id: UUID
    provisional_unavailable_ids: tuple[UUID, ...] = ()
    unavailable_ids: tuple[UUID, ...] = ()


class RunMissing(Exception):
    """Raised for an absent or foreign-owned run or rule."""


class RunConflict(Exception):
    """Raised when a request no longer matches the run, action or rule state."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep a stable machine code and optional current revision for HTTP 409."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision
