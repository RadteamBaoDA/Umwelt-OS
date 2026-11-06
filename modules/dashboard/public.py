"""Owner-facing dashboard queries and atomic configuration mutations (re-export facade over the private _* submodules)."""

from __future__ import annotations

from modules.dashboard import briefs, gadgets, layouts  # noqa: F401
from modules.dashboard._common import (
    DASHBOARD_QUOTA_LOCK_NAMESPACE as DASHBOARD_QUOTA_LOCK_NAMESPACE,
)
from modules.dashboard._common import (
    HIGHLIGHT_MATCHES_PER_PAGE_MAX as HIGHLIGHT_MATCHES_PER_PAGE_MAX,
)
from modules.dashboard._common import (
    HIGHLIGHT_SCAN_PAGE_LIMIT as HIGHLIGHT_SCAN_PAGE_LIMIT,
)
from modules.dashboard._common import (
    MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION as MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION,
)
from modules.dashboard._common import (
    MAX_REVISION as MAX_REVISION,
)
from modules.dashboard._common import (
    PREVIEW_MATCH_TIMEOUT_SECONDS as PREVIEW_MATCH_TIMEOUT_SECONDS,
)
from modules.dashboard._common import (
    PREVIEW_MAX_MATCHES as PREVIEW_MAX_MATCHES,
)
from modules.dashboard._common import (
    PREVIEW_MAX_PAGES as PREVIEW_MAX_PAGES,
)
from modules.dashboard._common import (
    PREVIEW_PAGE_SIZE as PREVIEW_PAGE_SIZE,
)
from modules.dashboard._common import (
    DashboardConflict as DashboardConflict,
)
from modules.dashboard._common import (
    DashboardMissing as DashboardMissing,
)
from modules.dashboard._common import (
    _admit as _admit,
)
from modules.dashboard._common import (
    _bump as _bump,
)
from modules.dashboard._common import (
    _check_revision as _check_revision,
)
from modules.dashboard._common import (
    _lock_dashboard as _lock_dashboard,
)
from modules.dashboard._common import (
    _lock_definition as _lock_definition,
)
from modules.dashboard._common import (
    _lock_owner_creation_quota as _lock_owner_creation_quota,
)
from modules.dashboard._common import (
    _owner_tzinfo as _owner_tzinfo,
)
from modules.dashboard._crud import (
    _resolve_instance_group as _resolve_instance_group,
)
from modules.dashboard._crud import (
    create_dashboard as create_dashboard,
)
from modules.dashboard._crud import (
    create_definition as create_definition,
)
from modules.dashboard._crud import (
    create_group as create_group,
)
from modules.dashboard._crud import (
    create_instance as create_instance,
)
from modules.dashboard._crud import (
    delete_dashboard as delete_dashboard,
)
from modules.dashboard._crud import (
    delete_definition as delete_definition,
)
from modules.dashboard._crud import (
    delete_group as delete_group,
)
from modules.dashboard._crud import (
    delete_instance as delete_instance,
)
from modules.dashboard._crud import (
    get_dashboard as get_dashboard,
)
from modules.dashboard._crud import (
    list_groups as list_groups,
)
from modules.dashboard._crud import (
    patch_dashboard as patch_dashboard,
)
from modules.dashboard._crud import (
    patch_definition as patch_definition,
)
from modules.dashboard._crud import (
    patch_group as patch_group,
)
from modules.dashboard._crud import (
    patch_instance as patch_instance,
)
from modules.dashboard._crud import (
    replace_layout as replace_layout,
)
from modules.dashboard._export import (
    _brief_export_citations as _brief_export_citations,
)
from modules.dashboard._export import (
    _brief_export_eligibility as _brief_export_eligibility,
)
from modules.dashboard._export import (
    _brief_export_page as _brief_export_page,
)
from modules.dashboard._export import (
    _brief_export_row_digest as _brief_export_row_digest,
)
from modules.dashboard._export import (
    _brief_export_validation as _brief_export_validation,
)
from modules.dashboard._export import (
    _brief_schedule_export_fence as _brief_schedule_export_fence,
)
from modules.dashboard._export import (
    _brief_schedule_export_page as _brief_schedule_export_page,
)
from modules.dashboard._export import (
    _brief_schedule_export_validation as _brief_schedule_export_validation,
)
from modules.dashboard._export import (
    _brief_schedule_export_value as _brief_schedule_export_value,
)
from modules.dashboard._export import (
    _dashboard_export_scope as _dashboard_export_scope,
)
from modules.dashboard._export import (
    _decode_dashboard_export_cursor as _decode_dashboard_export_cursor,
)
from modules.dashboard._export import (
    _decode_owner_export_cursor as _decode_owner_export_cursor,
)
from modules.dashboard._export import (
    _definition_export_page as _definition_export_page,
)
from modules.dashboard._export import (
    _definition_export_read as _definition_export_read,
)
from modules.dashboard._export import (
    _definition_export_row_digest as _definition_export_row_digest,
)
from modules.dashboard._export import (
    _definition_export_scope as _definition_export_scope,
)
from modules.dashboard._export import (
    _definition_export_validation as _definition_export_validation,
)
from modules.dashboard._export import (
    _encode_dashboard_export_cursor as _encode_dashboard_export_cursor,
)
from modules.dashboard._export import (
    _encode_owner_export_cursor as _encode_owner_export_cursor,
)
from modules.dashboard._export import (
    _newer_export_definition_exists as _newer_export_definition_exists,
)
from modules.dashboard._export import (
    export_page as export_page,
)
from modules.dashboard._export import (
    validate_export_fences as validate_export_fences,
)
from modules.dashboard._highlights import (
    HighlightRuleError as HighlightRuleError,
)
from modules.dashboard._highlights import (
    _deliver_highlight_matches as _deliver_highlight_matches,
)
from modules.dashboard._highlights import (
    _evaluate_highlights_read_only as _evaluate_highlights_read_only,
)
from modules.dashboard._highlights import (
    _highlight_read as _highlight_read,
)
from modules.dashboard._highlights import (
    _HighlightScan as _HighlightScan,
)
from modules.dashboard._highlights import (
    _lock_highlight_scan as _lock_highlight_scan,
)
from modules.dashboard._highlights import (
    _match_items as _match_items,
)
from modules.dashboard._highlights import (
    _rule_topic_terms as _rule_topic_terms,
)
from modules.dashboard._highlights import (
    _snapshot_highlight_scan as _snapshot_highlight_scan,
)
from modules.dashboard._highlights import (
    definition_usage as definition_usage,
)
from modules.dashboard._highlights import (
    evaluate_gadget_highlights as evaluate_gadget_highlights,
)
from modules.dashboard._highlights import (
    preview_highlights as preview_highlights,
)
from modules.dashboard._highlights import (
    validate_highlight_rules as validate_highlight_rules,
)
from modules.dashboard._presets import (
    _RENDERER_DISPLAY_NAMES as _RENDERER_DISPLAY_NAMES,
)
from modules.dashboard._presets import (
    _place_applied_instances as _place_applied_instances,
)
from modules.dashboard._presets import (
    _prepare_apply_target as _prepare_apply_target,
)
from modules.dashboard._presets import (
    _preview_preset_slot as _preview_preset_slot,
)
from modules.dashboard._presets import (
    apply_preset as apply_preset,
)
from modules.dashboard._presets import (
    compatible_source_counts as compatible_source_counts,
)
from modules.dashboard._presets import (
    list_gadget_sources as list_gadget_sources,
)
from modules.dashboard._presets import (
    list_templates as list_templates,
)
from modules.dashboard._presets import (
    metric_catalog_read as metric_catalog_read,
)
from modules.dashboard._presets import (
    preset_catalog as preset_catalog,
)
from modules.dashboard._presets import (
    preset_definition_name as preset_definition_name,
)
from modules.dashboard._presets import (
    preview_preset as preview_preset,
)
from modules.dashboard._presets import (
    renderer_reads as renderer_reads,
)
from modules.dashboard._reads import (
    _dashboard_read as _dashboard_read,
)
from modules.dashboard._reads import (
    _definition_read as _definition_read,
)
from modules.dashboard._reads import (
    _definition_source_states as _definition_source_states,
)
from modules.dashboard._reads import (
    _definition_warnings as _definition_warnings,
)
from modules.dashboard._reads import (
    _definitions_for_instances as _definitions_for_instances,
)
from modules.dashboard._reads import (
    _instance_minima as _instance_minima,
)
from modules.dashboard._reads import (
    _lock_selected_sources as _lock_selected_sources,
)
from modules.dashboard._reads import (
    _renderer_warnings as _renderer_warnings,
)
from modules.dashboard._reads import (
    _source_states as _source_states,
)
from modules.dashboard._reads import (
    _validate_selected_sources as _validate_selected_sources,
)
from modules.dashboard._reads import (
    count_source_gadgets as count_source_gadgets,
)
from modules.dashboard._reads import (
    get_definition as get_definition,
)
from modules.dashboard._reads import (
    list_dashboards as list_dashboards,
)
from modules.dashboard._reads import (
    list_definitions as list_definitions,
)

# Stable seams for P10 automation: daily context and briefs are consumed through this module only.
from modules.dashboard.brief_sharing import (  # deliberate late import
    SharedBrief,
    check_brief_shareable,
    list_shared_briefs,
    read_brief_access_projection,
    read_brief_translation_input,
    read_shared_brief,
)
from modules.dashboard.briefs import (  # deliberate late import
    BriefEmpty,
    BriefEvidenceRevoked,
    BriefSlotOwned,
    BriefUnavailable,
    claim_brief_slot,
    clean_document_brief_evidence,
    generate_brief,
    latest_brief,
    legacy_brief_coverage,
    list_briefs,
    read_schedule,
    read_slot_owner,
    release_brief_slot,
)
from modules.dashboard.context import build_daily_context  # deliberate late import
from modules.dashboard.daily_schemas import (  # deliberate late import
    BriefRead,
    DailyContext,
)
from modules.settings import public as settings_public  # noqa: F401
from modules.sources import public as sources  # noqa: F401

__all__ = [
    "BriefEmpty",
    "BriefEvidenceRevoked",
    "BriefRead",
    "BriefSlotOwned",
    "BriefUnavailable",
    "DailyContext",
    "SharedBrief",
    "build_daily_context",
    "check_brief_shareable",
    "claim_brief_slot",
    "clean_document_brief_evidence",
    "count_source_gadgets",
    "generate_brief",
    "latest_brief",
    "legacy_brief_coverage",
    "list_briefs",
    "list_shared_briefs",
    "read_brief_access_projection",
    "read_brief_translation_input",
    "read_schedule",
    "read_shared_brief",
    "read_slot_owner",
    "release_brief_slot",
]
