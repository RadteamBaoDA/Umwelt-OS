"""Public contract and service interface for selective memory, candidates, and privacy management."""

# ruff: noqa: F401

import binascii
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

if TYPE_CHECKING:
    from modules.knowledge.documents.public import (
        DocumentCleanupEvidenceScope,
        EvidenceReferenceRead,
    )
    from modules.sources.public import SourceFence

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, Select, delete, desc, func, or_, select, text, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    DomainError,
    PayloadTooLargeError,
    UnavailableError,
    ValidationFailedError,
)
from core.export_pages import content_digest, dto_digest
from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.admission import actor_id as _actor
from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.memory.schemas import (
    MemoryCandidateExportRead,
    MemoryCandidatePage,
    MemoryCandidateRead,
    MemoryCreate,
    MemoryExportFence,
    MemoryExportFenceValidation,
    MemoryExportPage,
    MemoryExportPrivacy,
    MemoryExportProvenance,
    MemoryExportRead,
    MemoryPage,
    MemoryPrivacyConfig,
    MemoryPrivacyUpdate,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRead,
    MemorySupersedeRequest,
    MemoryUpdate,
)
from modules.memory.selection import (
    evaluate_candidate,
    extract_candidate_proposals,
)

# Schema/DTO classes re-exported so other modules can import them from the public contract
# instead of the private ``schemas`` module.
__all__ = [
    "MemoryCandidateExportRead",
    "MemoryCandidatePage",
    "MemoryCandidateRead",
    "MemoryCreate",
    "MemoryExportFence",
    "MemoryExportFenceValidation",
    "MemoryExportPage",
    "MemoryExportPrivacy",
    "MemoryExportProvenance",
    "MemoryExportRead",
    "MemoryPage",
    "MemoryPrivacyConfig",
    "MemoryPrivacyUpdate",
    "MemoryPurgeRequest",
    "MemoryPurgeResponse",
    "MemoryRead",
    "MemorySupersedeRequest",
    "MemoryUpdate",
]


from modules.memory._common import (
    _DOCUMENT_MEMORY_CLEANUP_REASON as _DOCUMENT_MEMORY_CLEANUP_REASON,
)
from modules.memory._common import (
    _DOCUMENT_MEMORY_UNRESOLVED_REASON as _DOCUMENT_MEMORY_UNRESOLVED_REASON,
)
from modules.memory._common import (
    CACHE_KEY_MEMORIES_ACTIVE as CACHE_KEY_MEMORIES_ACTIVE,
)
from modules.memory._common import (
    MEMORY_EXPORT_PAGE_MAX_BYTES as MEMORY_EXPORT_PAGE_MAX_BYTES,
)
from modules.memory._common import (
    DocumentMemoryCleanupProgress as DocumentMemoryCleanupProgress,
)
from modules.memory._common import (
    SourceCopiedEvidenceScope as SourceCopiedEvidenceScope,
)
from modules.memory._common import (
    SourceMemoryCleanupProgress as SourceMemoryCleanupProgress,
)
from modules.memory._common import (
    _admit as _admit,
)
from modules.memory._common import (
    _cache_key as _cache_key,
)
from modules.memory._common import (
    _owned as _owned,
)
from modules.memory._common import (
    _privacy_select as _privacy_select,
)
from modules.memory._common import (
    bind_page_cursor as bind_page_cursor,
)
from modules.memory._common import (
    page_cursor_binding as page_cursor_binding,
)
from modules.memory._common import (
    unbind_page_cursor as unbind_page_cursor,
)
from modules.memory._export import (
    _EXPORT_PROVENANCE_KEYS as _EXPORT_PROVENANCE_KEYS,
)
from modules.memory._export import (
    _candidate_export_read as _candidate_export_read,
)
from modules.memory._export import (
    _decode_memory_export_cursor as _decode_memory_export_cursor,
)
from modules.memory._export import (
    _encode_memory_export_cursor as _encode_memory_export_cursor,
)
from modules.memory._export import (
    _ExportProvenance as _ExportProvenance,
)
from modules.memory._export import (
    _fence_export_source as _fence_export_source,
)
from modules.memory._export import (
    _memory_export_count as _memory_export_count,
)
from modules.memory._export import (
    _memory_export_provenance as _memory_export_provenance,
)
from modules.memory._export import (
    _memory_export_read as _memory_export_read,
)
from modules.memory._export import (
    _memory_export_scope as _memory_export_scope,
)
from modules.memory._export import (
    _memory_export_source_fence as _memory_export_source_fence,
)
from modules.memory._export import (
    _parse_export_provenance as _parse_export_provenance,
)
from modules.memory._export import (
    _verify_export_chat_evidence as _verify_export_chat_evidence,
)
from modules.memory._export import (
    _verify_export_document_evidence as _verify_export_document_evidence,
)
from modules.memory._export import (
    export_page as export_page,
)
from modules.memory._export import (
    validate_export_fences as validate_export_fences,
)
from modules.memory._privacy_cleanup import (
    _PRIVACY_LOCK_NAMESPACE as _PRIVACY_LOCK_NAMESPACE,
)
from modules.memory._privacy_cleanup import (
    CLEANUP_LOCK_TIMEOUT_MS as CLEANUP_LOCK_TIMEOUT_MS,
)
from modules.memory._privacy_cleanup import (
    _decode_document_memory_cleanup_cursor as _decode_document_memory_cleanup_cursor,
)
from modules.memory._privacy_cleanup import (
    _decode_source_memory_cursor as _decode_source_memory_cursor,
)
from modules.memory._privacy_cleanup import (
    _document_cleanup_scope_fingerprint as _document_cleanup_scope_fingerprint,
)
from modules.memory._privacy_cleanup import (
    _document_provenance_match as _document_provenance_match,
)
from modules.memory._privacy_cleanup import (
    _DocumentSweepTally as _DocumentSweepTally,
)
from modules.memory._privacy_cleanup import (
    _encode_document_memory_cleanup_cursor as _encode_document_memory_cleanup_cursor,
)
from modules.memory._privacy_cleanup import (
    _lock_linked_candidate as _lock_linked_candidate,
)
from modules.memory._privacy_cleanup import (
    _lock_live_provenance_evidence as _lock_live_provenance_evidence,
)
from modules.memory._privacy_cleanup import (
    _note_privacy_lock as _note_privacy_lock,
)
from modules.memory._privacy_cleanup import (
    _provenance_uuid as _provenance_uuid,
)
from modules.memory._privacy_cleanup import (
    _scrub_candidate as _scrub_candidate,
)
from modules.memory._privacy_cleanup import (
    _scrub_derived_memory as _scrub_derived_memory,
)
from modules.memory._privacy_cleanup import (
    _scrub_manual_document_provenance as _scrub_manual_document_provenance,
)
from modules.memory._privacy_cleanup import (
    _scrub_manual_source_provenance as _scrub_manual_source_provenance,
)
from modules.memory._privacy_cleanup import (
    _source_memory_cursor as _source_memory_cursor,
)
from modules.memory._privacy_cleanup import (
    _source_progress as _source_progress,
)
from modules.memory._privacy_cleanup import (
    _sweep_document_candidate_page as _sweep_document_candidate_page,
)
from modules.memory._privacy_cleanup import (
    _sweep_document_memory_page as _sweep_document_memory_page,
)
from modules.memory._privacy_cleanup import (
    _sweep_source_memory_page as _sweep_source_memory_page,
)
from modules.memory._privacy_cleanup import (
    bound_cleanup_lock_waits as bound_cleanup_lock_waits,
)
from modules.memory._privacy_cleanup import (
    lock_export_privacy as lock_export_privacy,
)
from modules.memory._privacy_cleanup import (
    lock_export_privacy_in_uow as lock_export_privacy_in_uow,
)
from modules.memory._privacy_cleanup import (
    lock_export_privacy_workspaces as lock_export_privacy_workspaces,
)
from modules.memory._privacy_cleanup import (
    privacy_lock_key as privacy_lock_key,
)
from modules.memory._privacy_cleanup import (
    purge_document_copied_evidence_page as purge_document_copied_evidence_page,
)
from modules.memory._privacy_cleanup import (
    purge_source_copied_evidence_page_in_uow as purge_source_copied_evidence_page_in_uow,
)
from modules.memory._privacy_cleanup import (
    read_export_privacy as read_export_privacy,
)
from modules.memory._reads import (
    _COUNT_BATCH as _COUNT_BATCH,
)
from modules.memory._reads import (
    COUNT_SCAN_CAP as COUNT_SCAN_CAP,
)
from modules.memory._reads import (
    COUNT_VERIFIED_CAP as COUNT_VERIFIED_CAP,
)
from modules.memory._reads import (
    _list_filters as _list_filters,
)
from modules.memory._reads import (
    _to_candidate_read as _to_candidate_read,
)
from modules.memory._reads import (
    _to_memory_read as _to_memory_read,
)
from modules.memory._reads import (
    _verified_candidate_read as _verified_candidate_read,
)
from modules.memory._reads import (
    _verified_memory_read as _verified_memory_read,
)
from modules.memory._reads import (
    _visible_kind_counts as _visible_kind_counts,
)
from modules.memory._reads import (
    invalidate_memory_cache as invalidate_memory_cache,
)
from modules.memory._service import (
    MemoryService as MemoryService,
)

logger = logging.getLogger(__name__)
