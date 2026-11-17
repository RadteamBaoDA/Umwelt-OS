"""Public entities contract (facade).

Implementation lives in the private ``_*`` submodules of ``modules.knowledge.entities``; every public name is
re-exported here so callers keep importing from this module.

Testing note: monkeypatch a helper on the submodule that defines and uses it. Patching the same name on this
facade only affects callers that resolve it through the facade at call time.
"""

# ruff: noqa: F401
from __future__ import annotations

from modules.knowledge.entities._common import (
    _admit as _admit,
)
from modules.knowledge.entities._common import (
    _schedule_entity_change as _schedule_entity_change,
)
from modules.knowledge.entities._crud import (
    add_alias as add_alias,
)
from modules.knowledge.entities._crud import (
    create_entity as create_entity,
)
from modules.knowledge.entities._crud import (
    delete_alias as delete_alias,
)
from modules.knowledge.entities._crud import (
    delete_entity as delete_entity,
)
from modules.knowledge.entities._crud import (
    publish_derived_field as publish_derived_field,
)
from modules.knowledge.entities._crud import (
    update_entity as update_entity,
)
from modules.knowledge.entities._decisions import (
    _read_decision_rows as _read_decision_rows,
)
from modules.knowledge.entities._decisions import (
    _resolve_correction_decisions as _resolve_correction_decisions,
)
from modules.knowledge.entities._decisions import (
    get_document_correction_decisions as get_document_correction_decisions,
)
from modules.knowledge.entities._export import (
    ENTITY_EXPORT_PAGE_MAX_BYTES as ENTITY_EXPORT_PAGE_MAX_BYTES,
)
from modules.knowledge.entities._export import (
    _decode_entity_export_cursor as _decode_entity_export_cursor,
)
from modules.knowledge.entities._export import (
    _encode_entity_export_cursor as _encode_entity_export_cursor,
)
from modules.knowledge.entities._export import (
    _entity_export_children as _entity_export_children,
)
from modules.knowledge.entities._export import (
    _entity_export_count as _entity_export_count,
)
from modules.knowledge.entities._export import (
    _entity_export_field_is_supported as _entity_export_field_is_supported,
)
from modules.knowledge.entities._export import (
    _entity_export_item as _entity_export_item,
)
from modules.knowledge.entities._export import (
    _entity_export_payload_bytes as _entity_export_payload_bytes,
)
from modules.knowledge.entities._export import (
    _entity_export_source_generations as _entity_export_source_generations,
)
from modules.knowledge.entities._export import (
    _entity_export_statement as _entity_export_statement,
)
from modules.knowledge.entities._export import (
    export_page as export_page,
)
from modules.knowledge.entities._export import (
    validate_export_fences as validate_export_fences,
)
from modules.knowledge.entities._extraction import (
    claim_extraction_work as claim_extraction_work,
)
from modules.knowledge.entities._extraction import (
    create_extracted_entity as create_extracted_entity,
)
from modules.knowledge.entities._extraction import (
    defer_blocked_extraction_recheck as defer_blocked_extraction_recheck,
)
from modules.knowledge.entities._extraction import (
    find_extraction_entity as find_extraction_entity,
)
from modules.knowledge.entities._extraction import (
    finish_extraction_work as finish_extraction_work,
)
from modules.knowledge.entities._extraction import (
    get_extraction_status as get_extraction_status,
)
from modules.knowledge.entities._extraction import (
    list_blocked_extraction_work as list_blocked_extraction_work,
)
from modules.knowledge.entities._extraction import (
    list_recoverable_extraction_work as list_recoverable_extraction_work,
)
from modules.knowledge.entities._extraction import (
    list_resolution_candidates as list_resolution_candidates,
)
from modules.knowledge.entities._extraction import (
    record_extraction_membership as record_extraction_membership,
)
from modules.knowledge.entities._extraction import (
    requeue_blocked_extraction_work as requeue_blocked_extraction_work,
)
from modules.knowledge.entities._extraction import (
    schedule_extraction_work as schedule_extraction_work,
)
from modules.knowledge.entities._extraction import (
    set_extraction_work_error as set_extraction_work_error,
)
from modules.knowledge.entities._extraction import (
    terminalize_exhausted_extraction_work as terminalize_exhausted_extraction_work,
)
from modules.knowledge.entities._history import (
    _decode_identity_cursor as _decode_identity_cursor,
)
from modules.knowledge.entities._history import (
    get_temporal_node_seeds as get_temporal_node_seeds,
)
from modules.knowledge.entities._history import (
    list_entity_history as list_entity_history,
)
from modules.knowledge.entities._reads import (
    _MAX_CANONICAL_HOPS as _MAX_CANONICAL_HOPS,
)
from modules.knowledge.entities._reads import (
    RedirectedEntityConflict as RedirectedEntityConflict,
)
from modules.knowledge.entities._reads import (
    TerminalEntityConflict as TerminalEntityConflict,
)
from modules.knowledge.entities._reads import (
    _aliases as _aliases,
)
from modules.knowledge.entities._reads import (
    _entity_read as _entity_read,
)
from modules.knowledge.entities._reads import (
    _resolve_canonical_bulk as _resolve_canonical_bulk,
)
from modules.knowledge.entities._reads import (
    get_entities as get_entities,
)
from modules.knowledge.entities._reads import (
    get_entity as get_entity,
)
from modules.knowledge.entities._reads import (
    get_entity_refs as get_entity_refs,
)
from modules.knowledge.entities._reads import (
    get_membership_refs as get_membership_refs,
)
from modules.knowledge.entities._reads import (
    list_entities as list_entities,
)
from modules.knowledge.entities._reads import (
    list_entity_evidence as list_entity_evidence,
)
from modules.knowledge.entities._reads import (
    list_entity_evidence_bulk as list_entity_evidence_bulk,
)
from modules.knowledge.entities._reads import (
    list_retained_version_membership_refs as list_retained_version_membership_refs,
)
from modules.knowledge.entities._reads import (
    list_version_membership_refs as list_version_membership_refs,
)
from modules.knowledge.entities._reads import (
    observability_quality_summary as observability_quality_summary,
)
from modules.knowledge.entities._reads import (
    record_owner_action as record_owner_action,
)
from modules.knowledge.entities._reads import (
    resolve_canonical_entity_id as resolve_canonical_entity_id,
)
from modules.knowledge.entities._reads import (
    resolve_canonical_entity_ids as resolve_canonical_entity_ids,
)
from modules.knowledge.entities._review_actions import (
    _assign_prior_and_target as _assign_prior_and_target,
)
from modules.knowledge.entities._review_actions import (
    _assign_selector as _assign_selector,
)
from modules.knowledge.entities._review_actions import (
    _assign_snapshot as _assign_snapshot,
)
from modules.knowledge.entities._review_actions import (
    _candidate_snapshots as _candidate_snapshots,
)
from modules.knowledge.entities._review_actions import (
    _lock_review_extraction as _lock_review_extraction,
)
from modules.knowledge.entities._review_actions import (
    _lock_review_owner_source as _lock_review_owner_source,
)
from modules.knowledge.entities._review_actions import (
    _record_assign_decisions as _record_assign_decisions,
)
from modules.knowledge.entities._review_actions import (
    _relationship_endpoint_memberships as _relationship_endpoint_memberships,
)
from modules.knowledge.entities._review_actions import (
    _relationship_review_selector as _relationship_review_selector,
)
from modules.knowledge.entities._review_actions import (
    _set_review_decision as _set_review_decision,
)
from modules.knowledge.entities._review_actions import (
    assign_review_candidate as assign_review_candidate,
)
from modules.knowledge.entities._review_actions import (
    resolve_relationship_review as resolve_relationship_review,
)
from modules.knowledge.entities._review_list import (
    _attach_review_evidence as _attach_review_evidence,
)
from modules.knowledge.entities._review_list import (
    _collect_review_items as _collect_review_items,
)
from modules.knowledge.entities._review_list import (
    _decode_review_cursor as _decode_review_cursor,
)
from modules.knowledge.entities._review_list import (
    _review_candidate_item as _review_candidate_item,
)
from modules.knowledge.entities._review_list import (
    _review_cursor as _review_cursor,
)
from modules.knowledge.entities._review_list import (
    _review_endpoint_dtos as _review_endpoint_dtos,
)
from modules.knowledge.entities._review_list import (
    _review_entity_bindings as _review_entity_bindings,
)
from modules.knowledge.entities._review_list import (
    _review_hints as _review_hints,
)
from modules.knowledge.entities._review_list import (
    _review_snapshot_digest as _review_snapshot_digest,
)
from modules.knowledge.entities._review_list import (
    list_review_candidates as list_review_candidates,
)
from modules.knowledge.entities._support import (
    _CLEANUP_LIMIT as _CLEANUP_LIMIT,
)
from modules.knowledge.entities._support import (
    EntitySupportClosure as EntitySupportClosure,
)
from modules.knowledge.entities._support import (
    _clear_unsupported_derived_fields as _clear_unsupported_derived_fields,
)
from modules.knowledge.entities._support import (
    _remove_entity_support as _remove_entity_support,
)
from modules.knowledge.entities._support import (
    _require_cleanup_fences as _require_cleanup_fences,
)
from modules.knowledge.entities._support import (
    list_changed_entities_after as list_changed_entities_after,
)
from modules.knowledge.entities._support import (
    lock_entity_ids as lock_entity_ids,
)
from modules.knowledge.entities._support import (
    prepare_support_cleanup_in_uow as prepare_support_cleanup_in_uow,
)
from modules.knowledge.entities._support import (
    remove_document_support as remove_document_support,
)
from modules.knowledge.entities._support import (
    remove_source_support as remove_source_support,
)
from modules.knowledge.entities._support import (
    support_cleanup_ids as support_cleanup_ids,
)

# Cross-module contract re-exports (importers use this public module, never private submodules).
from modules.knowledge.entities.schemas import (
    EntityEvidencePage as EntityEvidencePage,
)
from modules.knowledge.entities.schemas import (
    EntityHistoryPage as EntityHistoryPage,
)
from modules.knowledge.entities.schemas import (
    EntityPage as EntityPage,
)
from modules.knowledge.entities.schemas import (
    EntityRead as EntityRead,
)
from modules.knowledge.entities.schemas import (
    EntityRelationshipReviewRequest as EntityRelationshipReviewRequest,
)
from modules.knowledge.entities.schemas import (
    EntityRelationshipReviewResult as EntityRelationshipReviewResult,
)
from modules.knowledge.entities.schemas import (
    EntityReviewAssignmentRequest as EntityReviewAssignmentRequest,
)
from modules.knowledge.entities.schemas import (
    EntityReviewAssignmentResult as EntityReviewAssignmentResult,
)
from modules.knowledge.entities.schemas import (
    EntityReviewPage as EntityReviewPage,
)
from modules.knowledge.entities.schemas import (
    EvidenceRef as EvidenceRef,
)
from modules.knowledge.entities.schemas import (
    validate_metadata as validate_metadata,
)
from modules.knowledge.entities.seed import (
    ensure_demo_entities as ensure_demo_entities,  # re-export: used by documents seed
)
from modules.sources import public as sources
from modules.sources.public import SourceExportFence as SourceExportFence
from modules.sources.public import SourceFence as SourceFence

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "EntityEvidencePage",
    "EntityHistoryPage",
    "EntityPage",
    "EntityRead",
    "EntityRelationshipReviewRequest",
    "EntityRelationshipReviewResult",
    "EntityReviewAssignmentRequest",
    "EntityReviewAssignmentResult",
    "EntityReviewPage",
    "EvidenceRef",
    "ensure_demo_entities",
    "validate_metadata",
]
