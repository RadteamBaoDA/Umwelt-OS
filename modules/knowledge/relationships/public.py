"""Public relationships contract (facade).

Implementation lives in the private ``_*`` submodules of ``modules.knowledge.relationships``; every public name is
re-exported here so callers keep importing from this module.

Testing note: monkeypatch a helper on the submodule that looks it up (see ``tests/unit/relationships_patching.py``).
Patching a name on this facade only affects callers that resolve it through the facade at call time.
"""

# ruff: noqa: F401

from core.workspaces import public as workspaces
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships._cleanup import (
    _discover_support as _discover_support,
)
from modules.knowledge.relationships._cleanup import (
    _remove_support as _remove_support,
)
from modules.knowledge.relationships._cleanup import (
    lock_delete_closure as lock_delete_closure,
)
from modules.knowledge.relationships._cleanup import (
    lock_relationship_ids as lock_relationship_ids,
)
from modules.knowledge.relationships._cleanup import (
    prepare_support_cleanup_in_uow as prepare_support_cleanup_in_uow,
)
from modules.knowledge.relationships._cleanup import (
    purge_history_support as purge_history_support,
)
from modules.knowledge.relationships._cleanup import (
    remove_document_support as remove_document_support,
)
from modules.knowledge.relationships._cleanup import (
    remove_entity_closure as remove_entity_closure,
)
from modules.knowledge.relationships._cleanup import (
    remove_source_support as remove_source_support,
)
from modules.knowledge.relationships._cleanup import (
    support_cleanup_ids as support_cleanup_ids,
)
from modules.knowledge.relationships._common import (
    MAX_CLEANUP_SUPPORTS as MAX_CLEANUP_SUPPORTS,
)
from modules.knowledge.relationships._common import (
    RelationshipSupportClosure as RelationshipSupportClosure,
)
from modules.knowledge.relationships._common import (
    _admit as _admit,
)
from modules.knowledge.relationships._common import (
    _build_evidence_rows as _build_evidence_rows,
)
from modules.knowledge.relationships._common import (
    _evidence_read as _evidence_read,
)
from modules.knowledge.relationships._common import (
    _evidence_scope as _evidence_scope,
)
from modules.knowledge.relationships._common import (
    _relationship_read as _relationship_read,
)
from modules.knowledge.relationships._common import (
    _require_cleanup_fences as _require_cleanup_fences,
)
from modules.knowledge.relationships._common import (
    _schedule_relationship_change as _schedule_relationship_change,
)
from modules.knowledge.relationships._common import (
    _support_pairs as _support_pairs,
)
from modules.knowledge.relationships._corrections import (
    _refresh_derived_confidence as _refresh_derived_confidence,
)
from modules.knowledge.relationships._corrections import (
    _split_row_supports as _split_row_supports,
)
from modules.knowledge.relationships._corrections import (
    apply_entity_merge as apply_entity_merge,
)
from modules.knowledge.relationships._corrections import (
    apply_entity_split as apply_entity_split,
)
from modules.knowledge.relationships._corrections import (
    list_correction_relationship_refs as list_correction_relationship_refs,
)
from modules.knowledge.relationships._corrections import (
    lock_correction_closure as lock_correction_closure,
)
from modules.knowledge.relationships._corrections import (
    validate_entity_merge_plan as validate_entity_merge_plan,
)
from modules.knowledge.relationships._corrections import (
    validate_entity_split_plan as validate_entity_split_plan,
)
from modules.knowledge.relationships._export import (
    RELATIONSHIP_EXPORT_PAGE_MAX_BYTES as RELATIONSHIP_EXPORT_PAGE_MAX_BYTES,
)
from modules.knowledge.relationships._export import (
    _decode_relationship_export_cursor as _decode_relationship_export_cursor,
)
from modules.knowledge.relationships._export import (
    _encode_relationship_export_cursor as _encode_relationship_export_cursor,
)
from modules.knowledge.relationships._export import (
    _relationship_export_bytes as _relationship_export_bytes,
)
from modules.knowledge.relationships._export import (
    _relationship_export_count as _relationship_export_count,
)
from modules.knowledge.relationships._export import (
    _relationship_export_statement as _relationship_export_statement,
)
from modules.knowledge.relationships._export import (
    _relationship_source_generations as _relationship_source_generations,
)
from modules.knowledge.relationships._export import (
    export_page as export_page,
)
from modules.knowledge.relationships._export import (
    validate_export_fences as validate_export_fences,
)
from modules.knowledge.relationships._history import (
    _decode_bound_cursor as _decode_bound_cursor,
)
from modules.knowledge.relationships._history import (
    _hydrate_history_item as _hydrate_history_item,
)
from modules.knowledge.relationships._history import (
    _latest_snapshot_statement as _latest_snapshot_statement,
)
from modules.knowledge.relationships._history import (
    _list_relationships_as_of as _list_relationships_as_of,
)
from modules.knowledge.relationships._history import (
    _missing_history_ids as _missing_history_ids,
)
from modules.knowledge.relationships._history import (
    get_relationship_snapshot as get_relationship_snapshot,
)
from modules.knowledge.relationships._history import (
    record_relationship_history as record_relationship_history,
)
from modules.knowledge.relationships._reads import (
    _decode_neighbor_cursor as _decode_neighbor_cursor,
)
from modules.knowledge.relationships._reads import (
    _encode_neighbor_cursor as _encode_neighbor_cursor,
)
from modules.knowledge.relationships._reads import (
    _neighbor_items as _neighbor_items,
)
from modules.knowledge.relationships._reads import (
    get_neighbors as get_neighbors,
)
from modules.knowledge.relationships._reads import (
    get_neighbors_bulk as get_neighbors_bulk,
)
from modules.knowledge.relationships._reads import (
    list_relationship_evidence as list_relationship_evidence,
)
from modules.knowledge.relationships._reads import (
    list_relationships as list_relationships,
)
from modules.knowledge.relationships._writes import (
    create_relationship as create_relationship,
)
from modules.knowledge.relationships._writes import (
    publish_extracted_relationship as publish_extracted_relationship,
)
from modules.knowledge.relationships._writes import (
    remove_relationship as remove_relationship,
)
from modules.knowledge.relationships.schemas import (
    CorrectionRelationshipRef,
    NeighborPage,
    RelationshipSnapshot,
)
from modules.knowledge.relationships.seed import (
    ensure_demo_relationships,  # re-export: used by documents seed
)
from modules.sources import public as sources

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "CorrectionRelationshipRef",
    "NeighborPage",
    "RelationshipSnapshot",
    "ensure_demo_relationships",
]
