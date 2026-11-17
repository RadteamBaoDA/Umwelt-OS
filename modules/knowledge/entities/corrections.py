"""Entity correction operations (merge, split, delete, suppress): facade re-exporting the private ``_corrections_*`` modules."""

from modules.knowledge.documents import (
    public as documents,  # noqa: F401  (patched via the facade by tests)
)
from modules.knowledge.entities import public as entities  # noqa: F401
from modules.knowledge.entities._corrections.common import (
    MAX_CORRECTION_ENTITIES,
    MAX_CORRECTION_EVIDENCE_REFS,
    MAX_CORRECTION_MEMBERSHIPS,
    CorrectionConflictError,
    _admit,
    _Closure,
    _conflict,
    _DeleteClosure,
    _temporal_before_relationships,
    _temporal_correction,
)
from modules.knowledge.entities._corrections.delete import (
    _delete_closure_rows,
    _delete_owned_rows,
    _delete_redirect_closure,
    _discover_delete_closure,
    _lock_delete_closure,
    delete_canonical_entity,
)
from modules.knowledge.entities._corrections.discover import (
    _discover,
    _discover_owned_rows,
    _discover_redirects,
)
from modules.knowledge.entities._corrections.merge import (
    _check_future_scope,
    _locked_closure,
    _merge_aliases,
    _merge_derived_name_alias,
    _merge_field_supports,
    _merge_identity_fields,
    _record_assignments,
    _validate_merge_request,
    _validate_split_request,
    merge_entity,
    preview_merge,
    preview_split,
)
from modules.knowledge.entities._corrections.split import (
    _split_alias_supports,
    _split_clear_source_fields,
    _split_move_evidence,
    split_entity,
    suppress_candidates,
)
from modules.knowledge.relationships import public as relationships  # noqa: F401
from modules.sources import public as sources  # noqa: F401
from modules.timeline import public as timeline  # noqa: F401

__all__ = [
    "CorrectionConflictError",
    "MAX_CORRECTION_ENTITIES",
    "MAX_CORRECTION_EVIDENCE_REFS",
    "MAX_CORRECTION_MEMBERSHIPS",
    "_Closure",
    "_DeleteClosure",
    "_admit",
    "_check_future_scope",
    "_conflict",
    "_delete_closure_rows",
    "_delete_owned_rows",
    "_delete_redirect_closure",
    "_discover",
    "_discover_delete_closure",
    "_discover_owned_rows",
    "_discover_redirects",
    "_lock_delete_closure",
    "_locked_closure",
    "_merge_aliases",
    "_merge_derived_name_alias",
    "_merge_field_supports",
    "_merge_identity_fields",
    "_record_assignments",
    "_split_alias_supports",
    "_split_clear_source_fields",
    "_split_move_evidence",
    "_temporal_before_relationships",
    "_temporal_correction",
    "_validate_merge_request",
    "_validate_split_request",
    "delete_canonical_entity",
    "merge_entity",
    "preview_merge",
    "preview_split",
    "split_entity",
    "suppress_candidates",
]
