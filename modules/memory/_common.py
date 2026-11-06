"""Shared scope, admission, cursor-binding and cleanup-progress primitives for the memory owner module."""

import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ValidationFailedError
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.memory.models import MemoryPrivacyRecord

CACHE_KEY_MEMORIES_ACTIVE = "cache:memory:active"


def _cache_key(scope: Scope) -> str:
    """Return the per-workspace active-Memory cache key; the legacy global key is never reused."""
    return f"{CACHE_KEY_MEMORIES_ACTIVE}:{scope.workspace_id}"


def _privacy_select(scope: Scope) -> Select[MemoryPrivacyRecord]:
    """Select the single privacy row of this workspace actor."""
    return select(MemoryPrivacyRecord).where(
        MemoryPrivacyRecord.workspace_id == scope.workspace_id,
        MemoryPrivacyRecord.owner_id == _actor(scope),
    )


def _owned(model: Any, scope: Scope) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Workspace and actor predicates for a Memory/Candidate statement (never read unscoped)."""
    return model.workspace_id == scope.workspace_id, model.actor_user_id == _actor(scope)


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before any Memory query or lock.

    Thin wrapper over ``core.workspaces.admission.admit_scope`` (Workspace/Job scopes only, labelled ``Memory workspace``;
    no admin viewers). Members stay denied (403) before any statement runs. ``lock=True`` takes the fence ahead of
    the privacy, Source and Document locks; otherwise a non-locking snapshot is returned. No commit.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        allowed_scopes=(WorkspaceContext, InternalJobScope),
        scope_label="Memory workspace",
    )


MEMORY_EXPORT_PAGE_MAX_BYTES = 16_777_216
_DOCUMENT_MEMORY_CLEANUP_REASON = "Source document evidence removed"
_DOCUMENT_MEMORY_UNRESOLVED_REASON = "legacy_provenance_unresolved"


@dataclass(frozen=True)
class DocumentMemoryCleanupProgress:
    """Describe one bounded Memory cleanup page without exposing copied content."""

    complete: bool
    next_cursor: str | None
    changed: bool
    scrubbed_memories: int
    scrubbed_candidates: int
    unresolved_count: int
    unresolved_reason: str | None


@dataclass(frozen=True)
class SourceCopiedEvidenceScope:
    """Detached whole-Source purge scope; carries no Document identity or Source row."""

    operation_id: UUID
    source_id: UUID
    generation: int


@dataclass(frozen=True)
class SourceMemoryCleanupProgress:
    """Describe one bounded Source-local Memory sweep page without exposing copied content."""

    complete: bool
    next_cursor: str | None
    processed: int
    unresolved_count: int
    reason: str | None
    changed: bool


def page_cursor_binding(scope: Scope, **filters: object) -> str:
    """Fingerprint workspace, actor and list filters that a page cursor is only valid for."""
    raw = json.dumps(
        {"w": str(scope.workspace_id), "a": _actor(scope), "f": filters},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def bind_page_cursor(position: str, binding: str) -> str:
    """Wrap a timestamp/UUID cursor in a canonical envelope carrying its workspace binding."""
    raw = json.dumps(
        {"v": 2, "binding": binding, "position": position}, separators=(",", ":")
    ).encode()
    return b64url_encode(raw)


def unbind_page_cursor(cursor: str, binding: str) -> str:
    """Return the inner position cursor, or 422 for a foreign-workspace, stale or malformed cursor."""
    try:
        if not 1 <= len(cursor) <= 2048 or "=" in cursor:
            raise ValueError("Invalid cursor encoding")
        raw = b64url_decode(cursor)
        envelope = json.loads(raw)
        if (
            b64url_encode(raw) != cursor
            or not isinstance(envelope, dict)
            or set(envelope) != {"v", "binding", "position"}
            or envelope["v"] != 2
            or envelope["binding"] != binding
            or not isinstance(envelope["position"], str)
        ):
            raise ValueError("Cursor binding does not match")
        return envelope["position"]
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationFailedError("Invalid cursor") from exc
