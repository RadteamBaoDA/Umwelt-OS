"""Entity-extraction arq worker jobs facade.

The implementation lives in the private ``modules.knowledge.entities._worker_*`` submodules; every name is re-exported
here so ``apps/worker/main.py`` job references and tests keep the original ``modules.knowledge.entities.worker`` surface.
"""

from core.job_denial import PERMISSION_LOST as PERMISSION_LOST
from core.job_denial import terminalize as terminalize
from core.model_gateway.client import ModelGateway as ModelGateway
from core.model_gateway.policy import may_send as may_send
from core.model_gateway.schemas import RequestPolicy as RequestPolicy
from core.realtime import commit_with_replay as commit_with_replay
from core.realtime import make_graph_change as make_graph_change

# Aliased imports below are re-exported only so tests can reach/patch ``worker.<alias>`` (shared module objects).
from core.worker_context import resolve_owner_job_scope as _workspace_job_scope  # noqa: F401
from core.workspaces import public as workspaces  # noqa: F401
from modules.ingestion import public as ingestion  # noqa: F401
from modules.knowledge.documents import public as documents  # noqa: F401
from modules.knowledge.entities import public as entities  # noqa: F401
from modules.knowledge.entities._worker_claim import (
    _admit_claim as _admit_claim,
)
from modules.knowledge.entities._worker_claim import (
    _block_work as _block_work,
)
from modules.knowledge.entities._worker_claim import (
    _call_provider as _call_provider,
)
from modules.knowledge.entities._worker_claim import (
    _candidate_key as _candidate_key,
)
from modules.knowledge.entities._worker_claim import (
    _capture_extraction_snapshot as _capture_extraction_snapshot,
)
from modules.knowledge.entities._worker_claim import (
    _Claim as _Claim,
)
from modules.knowledge.entities._worker_claim import (
    _claim as _claim,
)
from modules.knowledge.entities._worker_claim import (
    _KnownCandidates as _KnownCandidates,
)
from modules.knowledge.entities._worker_claim import (
    _lock_and_check_source as _lock_and_check_source,
)
from modules.knowledge.entities._worker_claim import (
    _Locked as _Locked,
)
from modules.knowledge.entities._worker_claim import (
    _OwnerDecision as _OwnerDecision,
)
from modules.knowledge.entities._worker_claim import (
    _Plan as _Plan,
)
from modules.knowledge.entities._worker_claim import (
    _Published as _Published,
)
from modules.knowledge.entities._worker_claim import (
    _recheck_before_send as _recheck_before_send,
)
from modules.knowledge.entities._worker_claim import (
    _relock_and_compare as _relock_and_compare,
)
from modules.knowledge.entities._worker_claim import (
    _RunState as _RunState,
)
from modules.knowledge.entities._worker_claim import (
    _Skip as _Skip,
)
from modules.knowledge.entities._worker_claim import (
    _Snapshot as _Snapshot,
)
from modules.knowledge.entities._worker_claim import (
    _validate_extraction_response as _validate_extraction_response,
)
from modules.knowledge.entities._worker_claim import (
    _with_digest as _with_digest,
)
from modules.knowledge.entities._worker_plan import (
    _apply_owner_decisions as _apply_owner_decisions,
)
from modules.knowledge.entities._worker_plan import (
    _current_owner_decision as _current_owner_decision,
)
from modules.knowledge.entities._worker_plan import (
    _entity_review_item as _entity_review_item,
)
from modules.knowledge.entities._worker_plan import (
    _initial_plan as _initial_plan,
)
from modules.knowledge.entities._worker_plan import (
    _list_known_candidates as _list_known_candidates,
)
from modules.knowledge.entities._worker_plan import (
    _lock_matched_entities as _lock_matched_entities,
)
from modules.knowledge.entities._worker_plan import (
    _owner_override as _owner_override,
)
from modules.knowledge.entities._worker_plan import (
    _pre_resolve_near_duplicates as _pre_resolve_near_duplicates,
)
from modules.knowledge.entities._worker_plan import (
    _read_evidence_by_chunk as _read_evidence_by_chunk,
)
from modules.knowledge.entities._worker_plan import (
    _recompute_plan_entry as _recompute_plan_entry,
)
from modules.knowledge.entities._worker_plan import (
    _refresh_plan as _refresh_plan,
)
from modules.knowledge.entities._worker_plan import (
    _resolve_entities as _resolve_entities,
)
from modules.knowledge.entities._worker_publish import (
    _publish_entities as _publish_entities,
)
from modules.knowledge.entities._worker_publish import (
    _publish_entity as _publish_entity,
)
from modules.knowledge.entities._worker_publish import (
    _publish_extraction as _publish_extraction,
)
from modules.knowledge.entities._worker_publish import (
    _publish_relationships as _publish_relationships,
)
from modules.knowledge.entities._worker_publish import (
    _record_failure as _record_failure,
)
from modules.knowledge.entities._worker_publish import (
    _retain_relationship_review as _retain_relationship_review,
)
from modules.knowledge.entities._worker_publish import (
    process_entity_extraction_work as process_entity_extraction_work,
)
from modules.knowledge.entities._worker_ready import (
    EXTRACTION_ROLE as EXTRACTION_ROLE,
)
from modules.knowledge.entities._worker_ready import (
    _ack_event as _ack_event,
)
from modules.knowledge.entities._worker_ready import (
    _dependency_snapshot as _dependency_snapshot,
)
from modules.knowledge.entities._worker_ready import (
    _lock_ready_lineage as _lock_ready_lineage,
)
from modules.knowledge.entities._worker_ready import (
    _policy_allows_extraction as _policy_allows_extraction,
)
from modules.knowledge.entities._worker_ready import (
    _schedule_ready_work as _schedule_ready_work,
)
from modules.knowledge.entities._worker_ready import (
    _ScheduledReady as _ScheduledReady,
)
from modules.knowledge.entities._worker_ready import (
    _stage_ready_event as _stage_ready_event,
)
from modules.knowledge.entities._worker_ready import (
    logger as logger,
)
from modules.knowledge.entities._worker_ready import (
    process_document_ready as process_document_ready,
)
from modules.knowledge.entities._worker_recover import (
    _WORKSPACE_CURSOR_KEY as _WORKSPACE_CURSOR_KEY,
)
from modules.knowledge.entities._worker_recover import (
    _WORKSPACE_PAGE as _WORKSPACE_PAGE,
)
from modules.knowledge.entities._worker_recover import (
    _ReadyPage as _ReadyPage,
)
from modules.knowledge.entities._worker_recover import (
    _recover_ready_ref as _recover_ready_ref,
)
from modules.knowledge.entities._worker_recover import (
    _recover_workspace as _recover_workspace,
)
from modules.knowledge.entities._worker_recover import (
    _recovery_workspace_ids as _recovery_workspace_ids,
)
from modules.knowledge.entities._worker_recover import (
    _requeue_blocked_item as _requeue_blocked_item,
)
from modules.knowledge.entities._worker_recover import (
    _scan_ready_page as _scan_ready_page,
)
from modules.knowledge.entities._worker_recover import (
    recover_entity_extraction_work as recover_entity_extraction_work,
)
from modules.knowledge.entities.extraction import extraction_messages as extraction_messages
from modules.knowledge.entities.extraction import response_content as response_content
from modules.knowledge.entities.extraction import response_schema as response_schema
from modules.knowledge.relationships import public as relationships  # noqa: F401
from modules.settings import public as settings_public  # noqa: F401
from modules.sources import public as sources  # noqa: F401
