"""Public detached event queries, owner commands, and cleanup contracts (re-export facade over the private _* submodules)."""

from __future__ import annotations

from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from modules.knowledge.documents import public as documents  # noqa: F401
from modules.knowledge.entities import public as entities  # noqa: F401
from modules.sources import public as sources  # noqa: F401
from modules.timeline._cleanup import (
    _CLEANUP_LIMIT as _CLEANUP_LIMIT,
)
from modules.timeline._cleanup import (
    TimelineSupportClosure as TimelineSupportClosure,
)
from modules.timeline._cleanup import (
    _hide_unsupported as _hide_unsupported,
)
from modules.timeline._cleanup import (
    _remove_support as _remove_support,
)
from modules.timeline._cleanup import (
    _require_cleanup_fences as _require_cleanup_fences,
)
from modules.timeline._cleanup import (
    apply_entity_merge as apply_entity_merge,
)
from modules.timeline._cleanup import (
    apply_entity_split as apply_entity_split,
)
from modules.timeline._cleanup import (
    correction_event_ids as correction_event_ids,
)
from modules.timeline._cleanup import (
    list_changed_events_after as list_changed_events_after,
)
from modules.timeline._cleanup import (
    lock_event_ids as lock_event_ids,
)
from modules.timeline._cleanup import (
    prepare_support_cleanup_in_uow as prepare_support_cleanup_in_uow,
)
from modules.timeline._cleanup import (
    remove_document_support as remove_document_support,
)
from modules.timeline._cleanup import (
    remove_entity_participants as remove_entity_participants,
)
from modules.timeline._cleanup import (
    remove_source_support as remove_source_support,
)
from modules.timeline._cleanup import (
    revise_corrected_events as revise_corrected_events,
)
from modules.timeline._cleanup import (
    support_cleanup_ids as support_cleanup_ids,
)
from modules.timeline._common import (
    MAX_PAGE as MAX_PAGE,
)
from modules.timeline._common import (
    TIMELINE_EXPORT_PAGE_MAX_BYTES as TIMELINE_EXPORT_PAGE_MAX_BYTES,
)
from modules.timeline._common import (
    _admit as _admit,
)
from modules.timeline._common import (
    _cursor_decode as _cursor_decode,
)
from modules.timeline._common import (
    _cursor_encode as _cursor_encode,
)
from modules.timeline._common import (
    day_window as day_window,
)
from modules.timeline._export import (
    _decode_timeline_export_cursor as _decode_timeline_export_cursor,
)
from modules.timeline._export import (
    _encode_timeline_export_cursor as _encode_timeline_export_cursor,
)
from modules.timeline._export import (
    _timeline_export_count as _timeline_export_count,
)
from modules.timeline._export import (
    _timeline_export_json as _timeline_export_json,
)
from modules.timeline._export import (
    _timeline_export_record as _timeline_export_record,
)
from modules.timeline._export import (
    _timeline_export_statement as _timeline_export_statement,
)
from modules.timeline._export import (
    _TimelineExportIneligible as _TimelineExportIneligible,
)
from modules.timeline._export import (
    export_page as export_page,
)
from modules.timeline._export import (
    validate_export_fences as validate_export_fences,
)
from modules.timeline._extraction_work import (
    block_local_only_extraction_work as block_local_only_extraction_work,
)
from modules.timeline._extraction_work import (
    claim_extraction_work as claim_extraction_work,
)
from modules.timeline._extraction_work import (
    defer_blocked_extraction_recheck as defer_blocked_extraction_recheck,
)
from modules.timeline._extraction_work import (
    finish_extraction_work as finish_extraction_work,
)
from modules.timeline._extraction_work import (
    list_blocked_extraction_work as list_blocked_extraction_work,
)
from modules.timeline._extraction_work import (
    list_recoverable_extraction_work as list_recoverable_extraction_work,
)
from modules.timeline._extraction_work import (
    requeue_blocked_extraction_work as requeue_blocked_extraction_work,
)
from modules.timeline._extraction_work import (
    schedule_extraction_work as schedule_extraction_work,
)
from modules.timeline._extraction_work import (
    set_extraction_work_error as set_extraction_work_error,
)
from modules.timeline._publish import (
    _candidate_hash as _candidate_hash,
)
from modules.timeline._publish import (
    _event_fields_from_proposal as _event_fields_from_proposal,
)
from modules.timeline._publish import (
    _insert_derived_evidence as _insert_derived_evidence,
)
from modules.timeline._publish import (
    _link_derived_participants as _link_derived_participants,
)
from modules.timeline._publish import (
    _lock_extraction_entities as _lock_extraction_entities,
)
from modules.timeline._publish import (
    _new_derived_event as _new_derived_event,
)
from modules.timeline._publish import (
    _normalize_proposals as _normalize_proposals,
)
from modules.timeline._publish import (
    _proposal_participants as _proposal_participants,
)
from modules.timeline._publish import (
    _refresh_derived_event as _refresh_derived_event,
)
from modules.timeline._publish import (
    _schedule_temporal_event as _schedule_temporal_event,
)
from modules.timeline._publish import (
    publish_extracted_events as publish_extracted_events,
)
from modules.timeline._publish import (
    publish_provider_event as publish_provider_event,
)
from modules.timeline._publish import (
    summarize_source_events as summarize_source_events,
)
from modules.timeline._publish import (
    temporal_event_refs as temporal_event_refs,
)
from modules.timeline._reads import (
    _event_read as _event_read,
)
from modules.timeline._reads import (
    _evidence_rows as _evidence_rows,
)
from modules.timeline._reads import (
    _list_page as _list_page,
)
from modules.timeline._reads import (
    _list_partition as _list_partition,
)
from modules.timeline._reads import (
    _read_evidence_closure as _read_evidence_closure,
)
from modules.timeline._reads import (
    brief_event_support as brief_event_support,
)
from modules.timeline._reads import (
    get_event as get_event,
)
from modules.timeline._reads import (
    list_correlation_signals as list_correlation_signals,
)
from modules.timeline._reads import (
    list_event_evidence as list_event_evidence,
)
from modules.timeline._reads import (
    list_events as list_events,
)
from modules.timeline._reads import (
    list_timeline as list_timeline,
)
from modules.timeline._reads import (
    lock_brief_events as lock_brief_events,
)
from modules.timeline._writes import (
    _apply_scalar_corrections as _apply_scalar_corrections,
)
from modules.timeline._writes import (
    _event_support_snapshot as _event_support_snapshot,
)
from modules.timeline._writes import (
    _lock_correction_support as _lock_correction_support,
)
from modules.timeline._writes import (
    _normalize_optional_instant as _normalize_optional_instant,
)
from modules.timeline._writes import (
    _remove_unsupported_derived_participants as _remove_unsupported_derived_participants,
)
from modules.timeline._writes import (
    _replace_event_evidence as _replace_event_evidence,
)
from modules.timeline._writes import (
    _set_participants as _set_participants,
)
from modules.timeline._writes import (
    create_event as create_event,
)
from modules.timeline._writes import (
    delete_event as delete_event,
)
from modules.timeline._writes import (
    update_event as update_event,
)
from modules.timeline.schemas import (
    BriefEventSupport,
    CorrelationSignalPage,
    CorrelationSignalRead,
    EventCreate,
    EventPage,
    EventPatch,
    EventRead,
    TimelineExportEvidence,
    TimelineExportFence,
    TimelineExportFenceValidation,
    TimelineExportPage,
    TimelineExportParticipant,
    TimelineExportRead,
    TimelinePage,
    TimelineQuery,
)
from modules.timeline.seed import (  # deliberate late import
    ensure_demo_events,  # re-export: used by documents seed
)

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "BriefEventSupport",
    "CorrelationSignalPage",
    "CorrelationSignalRead",
    "EventCreate",
    "EventPage",
    "EventPatch",
    "EventRead",
    "TimelineExportEvidence",
    "TimelineExportFence",
    "TimelineExportFenceValidation",
    "TimelineExportPage",
    "TimelineExportParticipant",
    "TimelineExportRead",
    "TimelinePage",
    "TimelineQuery",
    "ensure_demo_events",
]
