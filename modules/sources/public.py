"""Public contract and service interface for the sources owner module (facade over private ``_*`` submodules)."""

# ruff: noqa: F401

# Approved read projection (AGENTS.md "Testing Guidelines" / docs/architecture/module-boundaries.md): sources owns
# Source; other modules may build SELECT/JOIN statements on these columns and must never INSERT, UPDATE, DELETE,
# instantiate or add them (guarded by tests/unit/architecture/test_module_boundaries.py). Permission and deletion
# checks stay with the caller's own admission fence plus the owner's workspace/source predicates; the projection adds
# no access of its own.
from modules.sources.models import Source as SourceReadProjection

# isort: split
import binascii
import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Integer,
    Select,
    and_,
    case,
    cast,
    desc,
    func,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from core.events import DomainEvent
from core.export_pages import content_digest, dto_digest, serialize_bounded_page
from core.ids import uuid7
from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_source_change
from core.tools.schemas import ToolDestination
from core.workspaces import public as workspaces
from core.workspaces.schemas import (
    AccessFence,
    AdminWorkspaceReadContext,
    InternalJobScope,
    Scope,
    WorkspaceContext,
)
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import (
    ConnectorSource,
    GadgetSourceSelection,
    GadgetSourceSelectionPage,
    OperationRead,
    SourceCreate,
    SourceExportFence,
    SourceFence,
    SourceFenceSet,
    SourceImpactRead,
    SourceList,
    SourceMetadataExportFence,
    SourceMetadataExportPage,
    SourceMetadataExportValidation,
    SourcePatch,
    SourcePurgeJobIdentity,
    SourceRead,
)

# DTOs other modules import from ``modules.sources.schemas`` are re-exported here so importers can switch to the
# public contract. Only the re-exported schema names are listed; every other public function remains importable.
__all__ = [
    "ConnectorSource",
    "GadgetSourceSelection",
    "GadgetSourceSelectionPage",
    "OperationRead",
    "SourceCreate",
    "SourceExportFence",
    "SourceFence",
    "SourceFenceSet",
    "SourceImpactRead",
    "SourceList",
    "SourceMetadataExportFence",
    "SourceMetadataExportPage",
    "SourceMetadataExportValidation",
    "SourcePatch",
    "SourcePurgeJobIdentity",
    "SourceRead",
    "SourceReadProjection",
]

from modules.sources._connectors import (
    ConnectorStatusRead as ConnectorStatusRead,
)
from modules.sources._connectors import (
    _select_provider_sources as _select_provider_sources,
)
from modules.sources._connectors import (
    get_connector_sources as get_connector_sources,
)
from modules.sources._connectors import (
    list_connector_status as list_connector_status,
)
from modules.sources._connectors import (
    list_provider_connector_sources as list_provider_connector_sources,
)
from modules.sources._connectors import (
    list_provider_sources as list_provider_sources,
)
from modules.sources._export import (
    _cursor_fingerprint as _cursor_fingerprint,
)
from modules.sources._export import (
    _decode_source_export_cursor as _decode_source_export_cursor,
)
from modules.sources._export import (
    _decode_source_page_cursor as _decode_source_page_cursor,
)
from modules.sources._export import (
    _encode_source_export_cursor as _encode_source_export_cursor,
)
from modules.sources._export import (
    _encode_source_page_cursor as _encode_source_page_cursor,
)
from modules.sources._export import (
    _source_export_columns as _source_export_columns,
)
from modules.sources._export import (
    _source_export_read as _source_export_read,
)
from modules.sources._export import (
    _source_export_scope as _source_export_scope,
)
from modules.sources._export import (
    export_eligible_source_ids as export_eligible_source_ids,
)
from modules.sources._export import (
    export_page as export_page,
)
from modules.sources._export import (
    filter_export_eligible_sources as filter_export_eligible_sources,
)
from modules.sources._export import (
    observability_quality_summary as observability_quality_summary,
)
from modules.sources._export import (
    validate_export_fences as validate_export_fences,
)
from modules.sources._fences import (
    _connector_source as _connector_source,
)
from modules.sources._fences import (
    _lock_source_fence_rows as _lock_source_fence_rows,
)
from modules.sources._fences import (
    _lock_source_row as _lock_source_row,
)
from modules.sources._fences import (
    _record_collection_result as _record_collection_result,
)
from modules.sources._fences import (
    _record_collection_started as _record_collection_started,
)
from modules.sources._fences import (
    _record_processing_result as _record_processing_result,
)
from modules.sources._fences import (
    _source_in_uow as _source_in_uow,
)
from modules.sources._fences import (
    filter_active_source_ids as filter_active_source_ids,
)
from modules.sources._fences import (
    get_connector_source as get_connector_source,
)
from modules.sources._fences import (
    get_source_fence as get_source_fence,
)
from modules.sources._fences import (
    get_source_fences as get_source_fences,
)
from modules.sources._fences import (
    github_retirement_peer_projection_in_uow as github_retirement_peer_projection_in_uow,
)
from modules.sources._fences import (
    ingestion_instance_lifecycle_projection as ingestion_instance_lifecycle_projection,
)
from modules.sources._fences import (
    ingestion_lifecycle_projection as ingestion_lifecycle_projection,
)
from modules.sources._fences import (
    lock_retained_evidence_source as lock_retained_evidence_source,
)
from modules.sources._fences import (
    lock_source as lock_source,
)
from modules.sources._fences import (
    lock_source_for_document as lock_source_for_document,
)
from modules.sources._fences import (
    lock_source_set as lock_source_set,
)
from modules.sources._fences import (
    record_collection_result as record_collection_result,
)
from modules.sources._fences import (
    record_collection_result_in_uow as record_collection_result_in_uow,
)
from modules.sources._fences import (
    record_collection_started as record_collection_started,
)
from modules.sources._fences import (
    record_collection_started_in_uow as record_collection_started_in_uow,
)
from modules.sources._fences import (
    record_processing_result as record_processing_result,
)
from modules.sources._fences import (
    record_processing_result_in_uow as record_processing_result_in_uow,
)
from modules.sources._fences import (
    set_connector_configuration as set_connector_configuration,
)
from modules.sources._fences import (
    source_fence_of as source_fence_of,
)
from modules.sources._lifecycle import (
    SOURCE_MEMORY_TERMINAL_CODES as SOURCE_MEMORY_TERMINAL_CODES,
)
from modules.sources._lifecycle import (
    _open_coverage_operations as _open_coverage_operations,
)
from modules.sources._lifecycle import (
    archive_source as archive_source,
)
from modules.sources._lifecycle import (
    coverage_settled_exclusion as coverage_settled_exclusion,
)
from modules.sources._lifecycle import (
    discover_source_purge_observer_ids as discover_source_purge_observer_ids,
)
from modules.sources._lifecycle import (
    list_source_purge_observer_ids as list_source_purge_observer_ids,
)
from modules.sources._lifecycle import (
    pause_source_for_connector as pause_source_for_connector,
)
from modules.sources._lifecycle import (
    pause_source_for_connector_in_uow as pause_source_for_connector_in_uow,
)
from modules.sources._lifecycle import (
    pending_source_coverage_ids as pending_source_coverage_ids,
)
from modules.sources._lifecycle import (
    prepare_source_pause_for_connector_in_uow as prepare_source_pause_for_connector_in_uow,
)
from modules.sources._lifecycle import (
    read_source_purge_operation as read_source_purge_operation,
)
from modules.sources._lifecycle import (
    source_data_purge_exists as source_data_purge_exists,
)
from modules.sources._lifecycle import (
    start_source_purge as start_source_purge,
)
from modules.sources._lifecycle import (
    update_source as update_source,
)
from modules.sources._listing import (
    get_gadget_sources as get_gadget_sources,
)
from modules.sources._listing import (
    list_active_gadget_sources as list_active_gadget_sources,
)
from modules.sources._listing import (
    list_active_source_kinds as list_active_source_kinds,
)
from modules.sources._listing import (
    list_gadget_sources as list_gadget_sources,
)
from modules.sources._listing import (
    list_sources as list_sources,
)
from modules.sources._purge_jobs import (
    RETENTION_EXEMPT_SOURCE_TYPES as RETENTION_EXEMPT_SOURCE_TYPES,
)
from modules.sources._purge_jobs import (
    SourceImpact as SourceImpact,
)
from modules.sources._purge_jobs import (
    _fence_connector_source as _fence_connector_source,
)
from modules.sources._purge_jobs import (
    _reconcile_github_hint_lifecycle as _reconcile_github_hint_lifecycle,
)
from modules.sources._purge_jobs import (
    _transitioned_source_scope as _transitioned_source_scope,
)
from modules.sources._purge_jobs import (
    discover_source_job_identity as discover_source_job_identity,
)
from modules.sources._purge_jobs import (
    get_connector_retained_effect_anchor_in_uow as get_connector_retained_effect_anchor_in_uow,
)
from modules.sources._purge_jobs import (
    get_source_impact as get_source_impact,
)
from modules.sources._purge_jobs import (
    list_retention_cutoffs as list_retention_cutoffs,
)
from modules.sources._purge_jobs import (
    lock_connector_retained_effect_anchor as lock_connector_retained_effect_anchor,
)
from modules.sources._purge_jobs import (
    read_source_purge_job_capture as read_source_purge_job_capture,
)
from modules.sources._purge_jobs import (
    read_source_purge_job_identity as read_source_purge_job_identity,
)
from modules.sources._purge_jobs import (
    resolve_source_job_scope as resolve_source_job_scope,
)
from modules.sources._purge_jobs import (
    resolve_source_purge_job_scope as resolve_source_purge_job_scope,
)
from modules.sources._scope import (
    _admit_source_scope as _admit_source_scope,
)
from modules.sources._scope import (
    _operation_scope as _operation_scope,
)
from modules.sources._scope import (
    _require_source_owner as _require_source_owner,
)
from modules.sources._scope import (
    _source_scope as _source_scope,
)
from modules.sources._tools import (
    CHAT_ATTACHMENTS_MARKER as CHAT_ATTACHMENTS_MARKER,
)
from modules.sources._tools import (
    CHAT_ATTACHMENTS_NAME as CHAT_ATTACHMENTS_NAME,
)
from modules.sources._tools import (
    CHAT_ATTACHMENTS_SHARED_NAME as CHAT_ATTACHMENTS_SHARED_NAME,
)
from modules.sources._tools import (
    DOCUMENT_UPLOADS_MARKER as DOCUMENT_UPLOADS_MARKER,
)
from modules.sources._tools import (
    DOCUMENT_UPLOADS_NAME as DOCUMENT_UPLOADS_NAME,
)
from modules.sources._tools import (
    ToolSourcePage as ToolSourcePage,
)
from modules.sources._tools import (
    ToolSourceRead as ToolSourceRead,
)
from modules.sources._tools import (
    _get_or_create_marked_manual_source as _get_or_create_marked_manual_source,
)
from modules.sources._tools import (
    create_source as create_source,
)
from modules.sources._tools import (
    ensure_demo_source as ensure_demo_source,
)
from modules.sources._tools import (
    get_or_create_chat_attachments_source as get_or_create_chat_attachments_source,
)
from modules.sources._tools import (
    get_or_create_uploads_source as get_or_create_uploads_source,
)
from modules.sources._tools import (
    get_source as get_source,
)
from modules.sources._tools import (
    get_tool_source as get_tool_source,
)
from modules.sources._tools import (
    is_chat_attachments_source as is_chat_attachments_source,
)
from modules.sources._tools import (
    list_tool_sources as list_tool_sources,
)
