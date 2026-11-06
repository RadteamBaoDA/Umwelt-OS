"""Durable automation dispatch: trigger inbox, run planning, ordered action execution and recovery.

Invariants (read before changing anything here):

* Identity and dedupe. A run is identified by ``(automation_id, revision, trigger_key)`` where the
  key is the trigger event id, the scheduled slot or a manual client id. The unique index is the
  only dedupe fence; every producer path inserts with ON CONFLICT DO NOTHING.
* Revision fence. A run cites an immutable revision. Before each action, and again immediately
  before an external write, the rule head must still be live, enabled and at that revision. Edits,
  pauses and deletes all bump the revision, so "revision differs" is the single drop condition.
* Loop bounds. Runs carry ``depth`` (root = 1) and origin ids; depth above ``MAX_DEPTH``, a rule
  reacting to its own output, known loop pairs, the cooldown and an hourly rate cap all end in a
  recorded ``skipped`` run instead of work.
* No replay of ambiguous effects. External and non-atomic actions are committed ``in_flight`` before
  the call. A row found ``in_flight`` after a restart becomes ``requires_review`` and is never
  re-sent. In-database actions (task, notification) commit together with their outcome row, so they
  are exactly-once.
* Approvals. ``run_agent`` and ``call_webhook`` always stop at ``awaiting_approval``; only an
  owner decision (owner session, CSRF) lets the worker perform them.
"""

from __future__ import annotations

from core.workspaces import (
    public as workspaces,  # noqa: F401  (patched through the facade by tests)
)
from modules.automations._execution_actions import (
    _EXTERNAL_DISPATCHERS as _EXTERNAL_DISPATCHERS,
)
from modules.automations._execution_actions import (
    _action_step as _action_step,
)
from modules.automations._execution_actions import (
    _automation_conversation as _automation_conversation,
)
from modules.automations._execution_actions import (
    _dispatch_call_webhook as _dispatch_call_webhook,
)
from modules.automations._execution_actions import (
    _dispatch_run_agent as _dispatch_run_agent,
)
from modules.automations._execution_actions import (
    _ExternalDispatch as _ExternalDispatch,
)
from modules.automations._execution_actions import (
    _generate_brief as _generate_brief,
)
from modules.automations._execution_actions import (
    _in_database_action as _in_database_action,
)
from modules.automations._execution_actions import (
    _prepare_action as _prepare_action,
)
from modules.automations._execution_actions import (
    _request_action_approval as _request_action_approval,
)
from modules.automations._execution_actions import (
    _restore_paused_action as _restore_paused_action,
)
from modules.automations._execution_actions import (
    _send_webhook as _send_webhook,
)
from modules.automations._execution_actions import (
    _settle_missing_evidence as _settle_missing_evidence,
)
from modules.automations._execution_actions import (
    _start_agent as _start_agent,
)
from modules.automations._execution_actions import (
    _transient as _transient,
)
from modules.automations._execution_core import (
    _action_modules_enabled as _action_modules_enabled,
)
from modules.automations._execution_core import (
    _admission as _admission,
)
from modules.automations._execution_core import (
    dependencies_missing as dependencies_missing,
)
from modules.automations._execution_core import (
    enqueue_trigger as enqueue_trigger,
)
from modules.automations._execution_core import (
    live_rules as live_rules,
)
from modules.automations._execution_core import (
    loop_guard as loop_guard,
)
from modules.automations._execution_core import (
    origin_for_reference as origin_for_reference,
)
from modules.automations._execution_core import (
    plan_run as plan_run,
)
from modules.automations._execution_core import (
    run_exists as run_exists,
)
from modules.automations._execution_dispatch import (
    _heartbeat as _heartbeat,
)
from modules.automations._execution_dispatch import (
    _settle as _settle,
)
from modules.automations._execution_dispatch import (
    dispatch_runs as dispatch_runs,
)
from modules.automations._execution_dispatch import (
    process_run as process_run,
)
from modules.automations._execution_evidence import (
    _canonical_uuid as _canonical_uuid,
)
from modules.automations._execution_evidence import (
    _CleanupMatch as _CleanupMatch,
)
from modules.automations._execution_evidence import (
    _document_trigger_sources as _document_trigger_sources,
)
from modules.automations._execution_evidence import (
    _event_belongs_elsewhere as _event_belongs_elsewhere,
)
from modules.automations._execution_evidence import (
    _legacy_event_matches_cleanup as _legacy_event_matches_cleanup,
)
from modules.automations._execution_evidence import (
    _legacy_run_event_id as _legacy_run_event_id,
)
from modules.automations._execution_evidence import (
    _legacy_run_evidence_current as _legacy_run_evidence_current,
)
from modules.automations._execution_evidence import (
    _match_run_for_cleanup as _match_run_for_cleanup,
)
from modules.automations._execution_evidence import (
    _retained_document_evidence_current as _retained_document_evidence_current,
)
from modules.automations._execution_evidence import (
    _revoke_run_document_evidence as _revoke_run_document_evidence,
)
from modules.automations._execution_evidence import (
    _run_evidence_current as _run_evidence_current,
)
from modules.automations._execution_evidence import (
    _scrub_document_payload as _scrub_document_payload,
)
from modules.automations._execution_evidence import (
    _trigger_evidence_current as _trigger_evidence_current,
)
from modules.automations._execution_evidence import (
    scrub_document_runs as scrub_document_runs,
)
from modules.automations._execution_evidence import (
    scrub_document_triggers as scrub_document_triggers,
)
from modules.automations._execution_runs import (
    _approval_hash as _approval_hash,
)
from modules.automations._execution_runs import (
    _commit as _commit,
)
from modules.automations._execution_runs import (
    _destination_stale as _destination_stale,
)
from modules.automations._execution_runs import (
    _fence_current as _fence_current,
)
from modules.automations._execution_runs import (
    _finish as _finish,
)
from modules.automations._execution_runs import (
    _locked_run as _locked_run,
)
from modules.automations._execution_runs import (
    _mark as _mark,
)
from modules.automations._execution_runs import (
    _reads as _reads,
)
from modules.automations._execution_runs import (
    _skip_pending as _skip_pending,
)
from modules.automations._execution_runs import (
    decide_action as decide_action,
)
from modules.automations._execution_runs import (
    fan_out_triggers as fan_out_triggers,
)
from modules.automations._execution_runs import (
    list_runs as list_runs,
)
from modules.automations._execution_runs import (
    start_manual as start_manual,
)
from modules.automations._execution_types import (
    _TERMINAL_RUN as _TERMINAL_RUN,
)
from modules.automations._execution_types import (
    ACTION_MODULE as ACTION_MODULE,
)
from modules.automations._execution_types import (
    APPROVAL_ACTIONS as APPROVAL_ACTIONS,
)
from modules.automations._execution_types import (
    COOLDOWN_SECONDS as COOLDOWN_SECONDS,
)
from modules.automations._execution_types import (
    DISPATCH_STALE_AFTER as DISPATCH_STALE_AFTER,
)
from modules.automations._execution_types import (
    MAX_ACTION_ATTEMPTS as MAX_ACTION_ATTEMPTS,
)
from modules.automations._execution_types import (
    MAX_ACTIONS as MAX_ACTIONS,
)
from modules.automations._execution_types import (
    MAX_DEPTH as MAX_DEPTH,
)
from modules.automations._execution_types import (
    MAX_RUN_ATTEMPTS as MAX_RUN_ATTEMPTS,
)
from modules.automations._execution_types import (
    MAX_RUNS_PER_HOUR as MAX_RUNS_PER_HOUR,
)
from modules.automations._execution_types import (
    RUNNING_STALE_AFTER as RUNNING_STALE_AFTER,
)
from modules.automations._execution_types import (
    TRIGGER_MODULE as TRIGGER_MODULE,
)
from modules.automations._execution_types import (
    AutomationCleanupProgress as AutomationCleanupProgress,
)
from modules.automations._execution_types import (
    RunConflict as RunConflict,
)
from modules.automations._execution_types import (
    RunMissing as RunMissing,
)
from modules.automations._execution_types import (
    _modules_cache as _modules_cache,
)
from modules.settings import public as settings_public  # noqa: F401
