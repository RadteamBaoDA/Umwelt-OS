"""Copied-evidence cleanup paging and citation filtering for document deletion behind the chat public contract."""

# Cross-module contract re-exports (importers use this public module, never private submodules).

# isort: split
import binascii as _binascii
import hashlib as _hashlib
import json as _json
from dataclasses import dataclass as _dataclass
from typing import Any as _Any
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import or_ as _or
from sqlalchemy import select as _select
from sqlalchemy import update as _update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    ForbiddenError,
)
from core.pagination import b64url_decode, b64url_encode
from core.workspaces import public as _workspaces
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.chat.models import (
    Conversation,
    Message,
    ResponseRun,
    StreamEvent,
)
from modules.chat.schemas import (
    WebCitation,
)
from modules.chat.worker import (
    _privacy_cancel_locked,
    neutralize_ui_events,
    purge_run_model_copies,
)
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope


@_dataclass(frozen=True)
class CopiedEvidenceCleanupProgress:
    """Describe one bounded Chat cleanup page without changing stream identities."""

    next_cursor: str | None
    complete: bool
    rows_examined: int
    rows_changed: int


_REMOVE_EVIDENCE = object()
_EVIDENCE_FIELDS = {
    "sourcetype",
    "source_type",
    "sourceid",
    "source_id",
    "documentid",
    "document_id",
    "documentversionid",
    "document_version_id",
    "chunkid",
    "chunk_id",
    "title",
    "url",
    "canonical_url",
    "observedat",
    "observed_at",
    "quote",
    "content",
    "excerpt",
    "content_hash",
    "chunk_index",
    "version_number",
    "score",
}


def _cleanup_uuid(value: object) -> UUID | None:
    """Parse one exact UUID field from a stored Chat evidence object without fuzzy matching."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _cleanup_scope_parts(
    scope: DocumentCleanupEvidenceScope,
) -> tuple[set[UUID], set[tuple[UUID, UUID]], set[UUID]]:
    """Separate exact version-only references from exact version/chunk identities."""
    version_ids = {
        item.document_version_id for item in scope.references if item.reference_kind == "version"
    }
    chunk_refs = {
        (item.document_version_id, item.chunk_id)
        for item in scope.references
        if item.reference_kind == "chunk" and item.chunk_id is not None
    }
    return version_ids, chunk_refs, {scope.document_id}


def _matches_cleanup_scope(value: object, scope: DocumentCleanupEvidenceScope) -> bool:
    """Match exact structured IDs without requiring chunk and version records in one page.

    Chunk identities and version-only identities have independent random cursors, so an exact
    version/chunk pair must match its chunk record even when the separate version record is on
    another bounded reference page. A version-only object still requires its own version record.
    """
    if not isinstance(value, dict):
        return False
    version_ids, chunk_refs, document_ids = _cleanup_scope_parts(scope)
    document_id = _cleanup_uuid(value.get("documentId") or value.get("document_id"))
    version_id = _cleanup_uuid(value.get("documentVersionId") or value.get("document_version_id"))
    chunk_id = _cleanup_uuid(value.get("chunkId") or value.get("chunk_id"))
    if version_id is not None:
        if chunk_id is not None:
            return (version_id, chunk_id) in chunk_refs
        return version_id in version_ids
    return document_id in document_ids


def _scrub_cleanup_payload(value: _Any, scope: DocumentCleanupEvidenceScope) -> tuple[_Any, bool]:
    """Remove exact copied evidence objects while retaining unrelated and owner-authored fields."""
    if isinstance(value, list):
        cleaned: list[_Any] = []
        changed = False
        for item in value:
            if _matches_cleanup_scope(item, scope):
                changed = True
                continue
            candidate, item_changed = _scrub_cleanup_payload(item, scope)
            changed = changed or item_changed
            if candidate is not _REMOVE_EVIDENCE:
                cleaned.append(candidate)
        return (cleaned if changed else value), changed
    if isinstance(value, dict):
        matched = _matches_cleanup_scope(value, scope)
        cleaned_map: dict[str, _Any] = {}
        changed = False
        for key, item in value.items():
            if matched and str(key).replace("-", "_").lower() in _EVIDENCE_FIELDS:
                changed = True
                continue
            candidate, item_changed = _scrub_cleanup_payload(item, scope)
            changed = changed or item_changed
            if candidate is not _REMOVE_EVIDENCE:
                cleaned_map[key] = candidate
        if matched and not cleaned_map:
            return _REMOVE_EVIDENCE, True
        return (cleaned_map if changed else value), changed
    return value, False


def _filter_citation_values(
    citations: object,
    scope: DocumentCleanupEvidenceScope,
) -> tuple[list[dict[str, object]], bool]:
    """Drop exact citations for a deleted document and preserve unrelated citation dictionaries."""
    if not isinstance(citations, list):
        return [], citations not in (None, [])
    kept: list[dict[str, object]] = []
    changed = False
    for item in citations:
        if _matches_cleanup_scope(item, scope):
            changed = True
        elif isinstance(item, dict):
            kept.append(item)
        else:
            changed = True
    return (kept if changed else citations), changed


def _cleanup_scope_fingerprint(scope: DocumentCleanupEvidenceScope) -> str:
    """Bind a continuation token to one operation and one bounded captured reference page."""
    material = {
        "operation_id": str(scope.operation_id),
        "document_id": str(scope.document_id),
        "references": [
            [
                item.reference_kind,
                str(item.document_version_id),
                str(item.chunk_id) if item.chunk_id else None,
            ]
            for item in scope.references
        ],
    }
    return _hashlib.sha256(
        _json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _encode_cleanup_cursor(
    scope: DocumentCleanupEvidenceScope,
    kind: str,
    after: UUID | None,
) -> str:
    """Encode a deterministic Chat-table keyset cursor bound to this evidence page."""
    payload = {
        "v": 1,
        "operation": str(scope.operation_id),
        "fingerprint": _cleanup_scope_fingerprint(scope),
        "kind": kind,
        "after": str(after) if after else None,
    }
    raw = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url_encode(raw)


def _decode_cleanup_cursor(
    cursor: str,
    scope: DocumentCleanupEvidenceScope,
) -> tuple[str, UUID | None]:
    """Reject malformed, non-canonical, cross-operation, and cross-page cleanup cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid Chat copied-evidence cursor")
        raw = b64url_decode(cursor)
        payload = _json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "operation", "fingerprint", "kind", "after"}
            or payload["v"] != 1
            or payload["operation"] != str(scope.operation_id)
            or payload["fingerprint"] != _cleanup_scope_fingerprint(scope)
            or payload["kind"] not in {"messages", "runs", "events"}
        ):
            raise ValueError("Chat copied-evidence cursor does not match this operation page")
        after = UUID(payload["after"]) if payload["after"] is not None else None
        if _encode_cleanup_cursor(scope, payload["kind"], after) != cursor:
            raise ValueError("Chat copied-evidence cursor is not canonical")
        return payload["kind"], after
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        _binascii.Error,
        _json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid Chat copied-evidence cursor") from exc


async def filter_current_citations(
    session: AsyncSession,
    citations: object,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> list[dict[str, object]]:
    """Return only citations whose exact document, version, and chunk still exist.

    The Documents owner supplies detached current evidence; absent or mismatched references
    are omitted before Chat history or SSE replay can expose their copied title, URL, or quote.
    Evidence row locks remain held until the caller's short response transaction completes.
    """
    if not isinstance(citations, list) or not citations:
        return []
    refs: list[tuple[UUID, UUID]] = []
    parsed: list[tuple[dict[str, object], UUID, UUID, UUID, UUID] | dict[str, object]] = []
    for raw in citations:
        if not isinstance(raw, dict):
            continue
        if raw.get("sourceType") == "web":
            # Web results reference no Source or evidence: keep valid ones in place (markers are positional).
            try:
                WebCitation.model_validate(raw)
            except ValueError:
                continue
            parsed.append(raw)
            continue
        source_id = _cleanup_uuid(raw.get("sourceId") or raw.get("source_id"))
        document_id = _cleanup_uuid(raw.get("documentId") or raw.get("document_id"))
        version_id = _cleanup_uuid(raw.get("documentVersionId") or raw.get("document_version_id"))
        chunk_id = _cleanup_uuid(raw.get("chunkId") or raw.get("chunk_id"))
        if None in (source_id, document_id, version_id, chunk_id):
            continue
        refs.append((version_id, chunk_id))  # type: ignore[arg-type]
        parsed.append((raw, source_id, document_id, version_id, chunk_id))  # type: ignore[arg-type]
    unique_refs = sorted(set(refs), key=lambda item: (str(item[0]), str(item[1])))
    current = {}
    if unique_refs:
        from modules.knowledge.documents import public as documents_public

        scope_kw: dict[str, _Any] = {
            "scope": scope,
            "multi_workspace_enabled": multi_workspace_enabled,
        }

        for start in range(0, len(unique_refs), 100):
            evidence = await documents_public.lock_chat_evidence_chunks(
                session,
                unique_refs[start : start + 100],
                require_active_source=False,
                **scope_kw,
            )
            current.update({(item.document_version_id, item.chunk_id): item for item in evidence})
    kept: list[dict[str, object]] = []
    for entry in parsed:
        if isinstance(entry, dict):
            kept.append(entry)
            continue
        raw, source_id, document_id, version_id, chunk_id = entry
        if (
            (item := current.get((version_id, chunk_id))) is not None
            and item.source_id == source_id
            and item.document_id == document_id
        ):
            kept.append(raw)
    return kept


async def _admit_cleanup_page(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    limit: int,
    scope: WorkspaceContext | InternalJobScope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[ColumnElement[bool], ...], tuple[ColumnElement[bool], ...]]:
    """Validate page bounds, admit the owner/job (non-locking) and bind the detached evidence page.

    Members are denied before any SQL. Returns the workspace/actor predicates that every
    later statement must carry, as ``(own_conversation, own_run)``.

    Raises:
        ValueError: when the page size or reference count is out of bounds.
        ForbiddenError: when a non-owner workspace context calls.
        TypeError: when the feature flag is not a real boolean.
        ConflictError: when the evidence page belongs to another workspace or actor.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Chat copied-evidence page size must be between 1 and 100")
    if len(evidence.references) > 100:
        raise ValueError("Chat copied-evidence identity page exceeds 100 references")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    await _workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    workspace_id = scope.workspace_id
    actor_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    if evidence.workspace_id != workspace_id or evidence.actor_user_id != actor_id:
        raise ConflictError("Document cleanup evidence belongs to another workspace")
    own_conversation = (
        Conversation.workspace_id == workspace_id,
        Conversation.actor_user_id == actor_id,
    )
    own_run = (ResponseRun.workspace_id == workspace_id, ResponseRun.actor_user_id == actor_id)
    return own_conversation, own_run


async def _select_cleanup_candidates(
    session: AsyncSession,
    kind: str,
    after: UUID | None,
    remaining: int,
    own_conversation: tuple[ColumnElement[bool], ...],
    own_run: tuple[ColumnElement[bool], ...],
) -> list[_Any]:
    """Select up to ``remaining + 1`` keyset-ordered candidate identities of one table kind.

    The extra row lets the caller tell whether more rows follow. Non-locking read bound to the
    caller's workspace and actor; ``kind`` is ``messages``, ``runs`` or ``events``.
    """
    if kind == "messages":
        return list(
            (
                await session.execute(
                    _select(Message.id, Message.conversation_id)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(*own_conversation, *([Message.id > after] if after else []))
                    .order_by(Message.id)
                    .limit(remaining + 1)
                )
            ).all()
        )
    if kind == "runs":
        return list(
            (
                await session.execute(
                    _select(ResponseRun.id, ResponseRun.conversation_id)
                    .where(*own_run, *([ResponseRun.id > after] if after else []))
                    .order_by(ResponseRun.id)
                    .limit(remaining + 1)
                )
            ).all()
        )
    return list(
        (
            await session.execute(
                _select(StreamEvent.id, StreamEvent.response_id, ResponseRun.conversation_id)
                .join(ResponseRun, ResponseRun.id == StreamEvent.response_id)
                .where(
                    *own_run,
                    *([StreamEvent.id > after] if after else []),
                    # Deltas of terminal runs are purged on a schedule and the final message/citations
                    # events are still scrubbed; scanning ~1M delta rows made a document delete take hours.
                    # Active runs keep the scan so an in-flight delta can still trigger privacy cancel.
                    _or(
                        StreamEvent.event_type.not_in(("message.delta", "message.reasoning.delta")),
                        ResponseRun.status.in_(("pending", "streaming", "awaiting_input")),
                    ),
                )
                .order_by(StreamEvent.id)
                .limit(remaining + 1)
            )
        ).all()
    )


async def _scrub_cleanup_message(
    session: AsyncSession,
    candidate: _Any,
    evidence: DocumentCleanupEvidenceScope,
    own_conversation: tuple[ColumnElement[bool], ...],
) -> int:
    """Lock one message (parent conversation first) and scrub withdrawn evidence from it.

    Returns 1 when the message was changed (citations/metadata scrubbed, widget and reasoning
    copies dropped, run copies purged), else 0. Row-locks the conversation then the message.
    """
    row_id, conversation_id = candidate
    parent = await session.scalar(
        _select(Conversation)
        .where(
            Conversation.id == conversation_id,
            *own_conversation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        return 0
    message = await session.scalar(
        _select(Message)
        .where(
            Message.id == row_id,
            Message.conversation_id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        return 0
    citations, citations_changed = _filter_citation_values(message.citations, evidence)
    metadata, metadata_changed = _scrub_cleanup_payload(message.metadata_json or {}, evidence)
    if not (citations_changed or metadata_changed):
        return 0
    message.citations = citations
    message.metadata_json = metadata if isinstance(metadata, dict) else {}
    # The widget copies evidence-derived rows/data that scrubbing cannot attribute: drop it whole.
    # Every part (and the first-part compatibility copy) is dropped together.
    # Both keys are popped unconditionally (any() would stop at the first hit).
    popped = [
        message.metadata_json.pop(key, None) for key in ("generative_ui", "generative_ui_parts")
    ]
    had_widget = any(value is not None for value in popped)
    # Free-text model reasoning may quote the withdrawn evidence and cannot be attributed: drop it too.
    dropped_reasoning = [
        message.metadata_json.pop(key, None) for key in ("reasoning", "reasoning_truncated")
    ]
    if any(value is not None for value in dropped_reasoning) and message.response_id is not None:
        await session.execute(
            _update(StreamEvent)
            .where(
                StreamEvent.response_id == message.response_id,
                StreamEvent.event_type == "message.reasoning.delta",
            )
            .values(data={"text": ""})
        )
    # Streaming deltas carry no document ids, so blank all widget events of the answer's run.
    if had_widget and message.response_id is not None:
        await neutralize_ui_events(session, message.response_id)
    # Input requests, todos and model-saved templates of that run were derived from the same evidence.
    if message.response_id is not None:
        await purge_run_model_copies(session, message.response_id)
    return 1


async def _scrub_cleanup_run(
    session: AsyncSession,
    candidate: _Any,
    evidence: DocumentCleanupEvidenceScope,
    own_conversation: tuple[ColumnElement[bool], ...],
    own_run: tuple[ColumnElement[bool], ...],
) -> int:
    """Lock one response run (parent conversation first) and scrub withdrawn evidence from it.

    Returns 1 when changed; active runs are privacy-cancelled under the held run lock.
    """
    row_id, conversation_id = candidate
    parent = await session.scalar(
        _select(Conversation)
        .where(
            Conversation.id == conversation_id,
            *own_conversation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        return 0
    run = await session.scalar(
        _select(ResponseRun)
        .where(
            ResponseRun.id == row_id,
            ResponseRun.conversation_id == conversation_id,
            *own_run,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        return 0
    citations, citations_changed = _filter_citation_values(run.citations, evidence)
    context, context_changed = _scrub_cleanup_payload(run.retrieval_context or {}, evidence)
    if not (citations_changed or context_changed):
        return 0
    run.citations = citations
    run.retrieval_context = context if isinstance(context, dict) else {}
    await purge_run_model_copies(
        session, run.id
    )  # completed runs too: input request, todos, templates
    if run.status in {"pending", "streaming", "awaiting_input"}:
        await _privacy_cancel_locked(session, run)
    return 1


async def _scrub_cleanup_event(
    session: AsyncSession,
    candidate: _Any,
    evidence: DocumentCleanupEvidenceScope,
    own_conversation: tuple[ColumnElement[bool], ...],
    own_run: tuple[ColumnElement[bool], ...],
) -> int:
    """Lock one stream event (conversation, run, then event) and scrub withdrawn evidence from it.

    Returns 1 when changed; widget events of the run are neutralized and active runs are
    privacy-cancelled at the event's sequence.
    """
    row_id, response_id, conversation_id = candidate
    parent = await session.scalar(
        _select(Conversation)
        .where(
            Conversation.id == conversation_id,
            *own_conversation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        return 0
    run = await session.scalar(
        _select(ResponseRun)
        .where(
            ResponseRun.id == response_id,
            ResponseRun.conversation_id == conversation_id,
            *own_run,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        return 0
    event = await session.scalar(
        _select(StreamEvent)
        .where(
            StreamEvent.id == row_id,
            StreamEvent.response_id == response_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        return 0
    payload, payload_changed = _scrub_cleanup_payload(event.data or {}, evidence)
    if not payload_changed:
        return 0
    event.data = payload if isinstance(payload, dict) else {}
    # Same rule for widget payloads carried by ui/done events.
    if event.event_type in {"message.ui.delta", "message.ui.done", "message.ui.discard"}:
        event.data = {}
    else:
        event.data.pop("generative_ui", None)
        event.data.pop("generative_ui_parts", None)
    # message.ui.delta rows hold partial widget data without identifiers (so they never match
    # the scope): neutralize every widget event of this run once any copy of it matched.
    await neutralize_ui_events(session, response_id)
    await purge_run_model_copies(session, response_id)
    if run.status in {"pending", "streaming", "awaiting_input"}:
        await _privacy_cancel_locked(session, run, event.seq)
    return 1


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = 100,
    scope: WorkspaceContext | InternalJobScope,
    multi_workspace_enabled: bool,
) -> CopiedEvidenceCleanupProgress:
    """Clean one bounded Chat table page using detached identities captured before hard deletion.

    Lock order is Memory privacy, Conversation parent, ResponseRun, then StreamEvent. This
    function mutates only Chat-owned rows; it preserves message text, unrelated citations,
    append-only mutation receipts, and every existing stream/event/run identity. The Documents
    caller persists the returned cursor and page mutations in the same transaction. Version-only
    and chunk identity records can fall on different scope pages; each exact chunk pair matches
    independently of the separate version-only record. The owner is admitted (non-locking, members
    denied before SQL), the detached page must belong to this workspace and actor, and every
    statement is bound to that workspace and actor. Never commits.
    """
    own_conversation, own_run = await _admit_cleanup_page(
        session,
        evidence,
        limit=limit,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if cursor is None:
        kind, after = "messages", None
    else:
        kind, after = _decode_cleanup_cursor(cursor, evidence)
    from modules.memory.public import bound_cleanup_lock_waits, lock_export_privacy

    await lock_export_privacy(session, scope.workspace_id)
    await bound_cleanup_lock_waits(session)
    kinds = ("messages", "runs", "events")
    index = kinds.index(kind)
    examined = 0
    changed = 0
    while index < len(kinds) and examined < limit:
        kind = kinds[index]
        remaining = limit - examined
        found = await _select_cleanup_candidates(
            session, kind, after, remaining, own_conversation, own_run
        )
        has_more = len(found) > remaining
        rows = found[:remaining]
        examined += len(rows)
        for candidate in rows:
            if kind == "messages":
                changed += await _scrub_cleanup_message(
                    session, candidate, evidence, own_conversation
                )
            elif kind == "runs":
                changed += await _scrub_cleanup_run(
                    session, candidate, evidence, own_conversation, own_run
                )
            else:
                changed += await _scrub_cleanup_event(
                    session, candidate, evidence, own_conversation, own_run
                )
        if has_more:
            last_id = rows[-1][0]
            return CopiedEvidenceCleanupProgress(
                next_cursor=_encode_cleanup_cursor(evidence, kind, last_id),
                complete=False,
                rows_examined=examined,
                rows_changed=changed,
            )
        after = None
        index += 1
        if examined >= limit and index < len(kinds):
            return CopiedEvidenceCleanupProgress(
                next_cursor=_encode_cleanup_cursor(evidence, kinds[index], None),
                complete=False,
                rows_examined=examined,
                rows_changed=changed,
            )
    return CopiedEvidenceCleanupProgress(
        next_cursor=None,
        complete=True,
        rows_examined=examined,
        rows_changed=changed,
    )
