"""Durable temporal execution with full owner fences, ordered receipts and exclusive graph dispatch (facade).

Implementation lives in the private ``_worker_*`` submodules of ``modules.knowledge.temporal``; every name is
re-exported here so ``apps/worker/main.py`` (arq job functions) and tests keep importing from this module.

Testing note: monkeypatch a helper on the submodule that looks it up (see
``tests/unit/modules/knowledge/temporal/_worker_patch.py``).
Patching a name on this facade only affects callers that resolve it through the facade at call time.
"""

# ruff: noqa: F401

from core.job_denial import terminalize
from core.model_gateway.client import ModelGateway
from core.workspaces import public as workspaces
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.temporal._worker_actions import (
    _bind_node_actions as _bind_node_actions,
)
from modules.knowledge.temporal._worker_actions import (
    _build_fact_actions as _build_fact_actions,
)
from modules.knowledge.temporal._worker_actions import (
    _current_relationship_snapshot as _current_relationship_snapshot,
)
from modules.knowledge.temporal._worker_actions import (
    _fresh_fact_action as _fresh_fact_action,
)
from modules.knowledge.temporal._worker_actions import (
    _initial_node_actions as _initial_node_actions,
)
from modules.knowledge.temporal._worker_actions import (
    _prepare_node_actions as _prepare_node_actions,
)
from modules.knowledge.temporal._worker_actions import (
    _readback_matches_desired as _readback_matches_desired,
)
from modules.knowledge.temporal._worker_actions import (
    _recovery_authorization as _recovery_authorization,
)
from modules.knowledge.temporal._worker_actions import (
    _validate_fact_replacement as _validate_fact_replacement,
)
from modules.knowledge.temporal._worker_admission import (
    LEASE_SECONDS as LEASE_SECONDS,
)
from modules.knowledge.temporal._worker_admission import (
    OWNER_BUDGET_SECONDS as OWNER_BUDGET_SECONDS,
)
from modules.knowledge.temporal._worker_admission import (
    RECOVERY_CURSOR_KEY as RECOVERY_CURSOR_KEY,
)
from modules.knowledge.temporal._worker_admission import (
    RECOVERY_WORKSPACE_PAGE as RECOVERY_WORKSPACE_PAGE,
)
from modules.knowledge.temporal._worker_admission import (
    _Admission as _Admission,
)
from modules.knowledge.temporal._worker_admission import (
    _admit as _admit,
)
from modules.knowledge.temporal._worker_admission import (
    _admit_job as _admit_job,
)
from modules.knowledge.temporal._worker_admission import (
    _authorize_scheduled_rebuild as _authorize_scheduled_rebuild,
)
from modules.knowledge.temporal._worker_admission import (
    _claim as _claim,
)
from modules.knowledge.temporal._worker_admission import (
    _commit as _commit,
)
from modules.knowledge.temporal._worker_admission import (
    _dependency_fingerprint as _dependency_fingerprint,
)
from modules.knowledge.temporal._worker_admission import (
    _get as _get,
)
from modules.knowledge.temporal._worker_admission import (
    _lease as _lease,
)
from modules.knowledge.temporal._worker_admission import (
    _mark_dependents_for_rebuild as _mark_dependents_for_rebuild,
)
from modules.knowledge.temporal._worker_admission import (
    admit_workspace as admit_workspace,
)
from modules.knowledge.temporal._worker_finish import (
    _find_ancestor_operation as _find_ancestor_operation,
)
from modules.knowledge.temporal._worker_finish import (
    _finish as _finish,
)
from modules.knowledge.temporal._worker_finish import (
    _join_local_cleanup as _join_local_cleanup,
)
from modules.knowledge.temporal._worker_finish import (
    _publish_clean_projection as _publish_clean_projection,
)
from modules.knowledge.temporal._worker_finish import (
    _verify_prior_dispatches as _verify_prior_dispatches,
)
from modules.knowledge.temporal._worker_journal import (
    _bindings as _bindings,
)
from modules.knowledge.temporal._worker_journal import (
    _inventory as _inventory,
)
from modules.knowledge.temporal._worker_journal import (
    _receipt_inventory as _receipt_inventory,
)
from modules.knowledge.temporal._worker_journal import (
    _receipts as _receipts,
)
from modules.knowledge.temporal._worker_journal import (
    _record as _record,
)
from modules.knowledge.temporal._worker_journal import (
    _WitnessIndex as _WitnessIndex,
)
from modules.knowledge.temporal._worker_owner import (
    _context as _context,
)
from modules.knowledge.temporal._worker_owner import (
    _lock_owner_state as _lock_owner_state,
)
from modules.knowledge.temporal._worker_owner import (
    _OwnerCallbacks as _OwnerCallbacks,
)
from modules.knowledge.temporal._worker_owner import (
    _record_embedding_identity as _record_embedding_identity,
)
from modules.knowledge.temporal._worker_recover import (
    _complete_recovery as _complete_recovery,
)
from modules.knowledge.temporal._worker_recover import (
    _recover as _recover,
)
from modules.knowledge.temporal._worker_recover import (
    _recover_ancestor as _recover_ancestor,
)
from modules.knowledge.temporal._worker_recover import (
    _recovery_workspaces as _recovery_workspaces,
)
from modules.knowledge.temporal._worker_recover import (
    recover_graph_work as recover_graph_work,
)
from modules.knowledge.temporal._worker_run import (
    _project_current_episode as _project_current_episode,
)
from modules.knowledge.temporal._worker_run import (
    _run_operation as _run_operation,
)
from modules.knowledge.temporal._worker_run import (
    process_graph_operation as process_graph_operation,
)
from modules.knowledge.temporal.adapter import CanonicalNodeRecoveryAction, GraphConfiguration
from modules.knowledge.temporal.models import GraphOperation
from modules.settings import public as settings_public
from modules.sources import public as sources
