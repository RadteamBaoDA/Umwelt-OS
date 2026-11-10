"""Chat export paging, memory export origin and export fence revalidation behind the chat public contract."""

# Cross-module contract re-exports (importers use this public module, never private submodules).

# isort: split
import binascii as _binascii
import hashlib as _hashlib
import json as _json
from collections.abc import Sequence
from datetime import UTC as _UTC
from datetime import datetime as _datetime
from typing import Any as _Any
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import ColumnElement
from sqlalchemy import func as _func
from sqlalchemy import or_ as _or
from sqlalchemy import select as _select
from sqlalchemy import tuple_ as _tuple
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner as _Owner
from core.errors import (
    DomainError,
)
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.schemas import WorkspaceContext
from modules.chat.models import (
    Conversation,
    Message,
)
from modules.chat.schemas import (
    ChatExportCitation,
    ChatExportCitationFence,
    ChatExportConversationRead,
    ChatExportFence,
    ChatExportFenceValidation,
    ChatExportMessageRead,
    ChatExportPage,
    ChatExportWebCitation,
    ChatMemoryExportOrigin,
    Citation,
    WebCitation,
)
from modules.knowledge.documents.public import EvidenceReferenceRead

CHAT_EXPORT_PAGE_MAX_BYTES = 16_777_216
CHAT_EXPORT_MAX_CITATIONS_PER_PAGE = 100


def _encode_chat_export_cursor(
    owner_id: int,
    record_kind: str,
    snapshot_at: _datetime,
    position_at: _datetime,
    position_id: UUID,
) -> str:
    """Encode a canonical owner/kind/cutoff-bound chat keyset cursor."""
    payload = {
        "v": 1,
        "owner": owner_id,
        "kind": record_kind,
        "snapshot": snapshot_at.astimezone(_UTC).isoformat(),
        "at": position_at.astimezone(_UTC).isoformat(),
        "id": str(position_id),
    }
    raw = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url_encode(raw)


def _decode_chat_export_cursor(
    cursor: str,
    owner_id: int,
    record_kind: str,
) -> tuple[_datetime, _datetime, UUID]:
    """Decode a strict canonical cursor and reject owner or record-kind substitution."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid chat export cursor")
        raw = b64url_decode(cursor)
        payload = _json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "owner",
            "kind",
            "snapshot",
            "at",
            "id",
        }:
            raise ValueError("Invalid chat export cursor")
        if payload["v"] != 1 or payload["owner"] != owner_id or payload["kind"] != record_kind:
            raise ValueError("Chat export cursor belongs to another owner or record kind")
        snapshot_at = _datetime.fromisoformat(payload["snapshot"])
        position_at = _datetime.fromisoformat(payload["at"])
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (snapshot_at, position_at)
        ):
            raise ValueError("Chat export cursor timestamps must be timezone-aware")
        snapshot_at, position_at = snapshot_at.astimezone(_UTC), position_at.astimezone(_UTC)
        if snapshot_at > _datetime.now(_UTC):
            raise ValueError("Chat export cursor cutoff cannot be in the future")
        position_id = UUID(payload["id"])
        if (
            _encode_chat_export_cursor(owner_id, record_kind, snapshot_at, position_at, position_id)
            != cursor
        ):
            raise ValueError("Chat export cursor is not canonical")
        return snapshot_at, position_at, position_id
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        _binascii.Error,
        _json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid chat export cursor") from exc


def _chat_export_payload_bytes(items: Sequence[BaseModel]) -> int:
    """Measure serialized item-array bytes for the aggregate export-page budget."""
    return len(
        _json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _chat_export_item_bytes(item: BaseModel) -> int:
    """Measure one serialized chat record so page admission avoids repeated full-array encoding."""
    return len(
        _json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _safe_chat_export_url(value: str | None) -> str | None:
    """Remove credentials, query values, fragments, and non-web URLs from citations."""
    if not value:
        return None
    try:
        parsed = _urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return _urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


def _export_owner_allowed(owner_id: int, scope: _Any, multi_workspace_enabled: bool) -> bool:
    """Bootstrap owner always; any other account only when the rollout is on and the scope is theirs."""
    return (owner_id == 1 or multi_workspace_enabled) and (
        scope is None or (scope.user_id == owner_id and scope.role == "owner")
    )


async def _require_chat_export_owner(
    session: AsyncSession,
    owner_id: int,
    *,
    scope: _Any = None,
    multi_workspace_enabled: bool = False,
) -> None:
    """Require the live workspace owner before reading owner-scoped chat history."""
    if (
        not _export_owner_allowed(owner_id, scope, multi_workspace_enabled)
        or await session.scalar(_select(_Owner.id).where(_Owner.id == owner_id)) is None
    ):
        raise PermissionError("Chat export requires the current owner")


async def read_memory_export_origin(
    session: AsyncSession,
    *,
    owner_id: int,
    conversation_id: UUID,
    message_id: UUID,
) -> ChatMemoryExportOrigin | None:
    """Fence an exact retained transcript message for Memory without exposing its text.

    The read observes current history consent and the live non-ephemeral owner conversation.
    Its digest binds content, citations, and both lifecycle rows so Memory can recheck this
    evidence immediately before its download is published.
    """
    from modules.chat.scope import multi_workspace_enabled, owner_default_scope
    from modules.memory.public import lock_export_privacy

    flag = multi_workspace_enabled()
    try:
        scope = await owner_default_scope(session, owner_id)
    except (PermissionError, HTTPException, DomainError):
        return None
    # Reader: only needs a consistent consent read; the workspace is known before the lock.
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    try:
        await _require_chat_export_owner(
            session, owner_id, scope=scope, multi_workspace_enabled=flag
        )
    except (PermissionError, HTTPException, DomainError):
        return None
    history_enabled, persisted, privacy_updated_at = await _chat_export_privacy(
        session, scope=scope, multi_workspace_enabled=flag
    )
    if not history_enabled:
        return None
    now = _datetime.now(_UTC)
    conversation_scope = _chat_export_scope(now, now, scope.workspace_id)
    row = (
        await session.execute(
            _select(
                Conversation.id.label("conversation_id"),
                Conversation.created_at.label("conversation_created_at"),
                Conversation.updated_at.label("conversation_updated_at"),
                Message.id.label("message_id"),
                Message.created_at.label("message_created_at"),
                Message.updated_at.label("message_updated_at"),
                Message.content.label("content"),
                Message.citations.label("citations"),
            )
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.id == conversation_id,
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                *conversation_scope,
                Message.created_at <= now,
                Message.updated_at <= now,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    digest_input = _json.dumps(
        {"content": row.content, "citations": row.citations},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ChatMemoryExportOrigin(
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        conversation_created_at=row.conversation_created_at,
        conversation_updated_at=row.conversation_updated_at,
        message_created_at=row.message_created_at,
        message_updated_at=row.message_updated_at,
        content_citation_digest=_hashlib.sha256(digest_input).hexdigest(),
        privacy_persisted=persisted,
        privacy_updated_at=privacy_updated_at,
    )


def _chat_export_privacy_marker(
    persisted: bool, updated_at: _datetime | None
) -> tuple[bool, _datetime | None]:
    """Validate the compact Memory-owned history-privacy persistence and timestamp fence."""
    if type(persisted) is not bool or persisted != (updated_at is not None):
        raise ValueError("Chat export privacy marker is inconsistent")
    if updated_at is not None and (
        not isinstance(updated_at, _datetime)
        or updated_at.tzinfo is None
        or updated_at.utcoffset() is None
    ):
        raise ValueError("Chat export privacy timestamp must be timezone-aware")
    return persisted, updated_at


async def _chat_export_privacy(
    session: AsyncSession,
    *,
    scope: _Any = None,
    multi_workspace_enabled: bool | None = None,
) -> tuple[bool, bool, _datetime | None]:
    """Read Memory's current history-storage grant and its minimal persisted-row fence."""
    from modules.chat.scope import read_owner_export_privacy

    if scope is not None and multi_workspace_enabled is not None:
        from modules.memory.public import read_export_privacy

        privacy = await read_export_privacy(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    else:
        privacy = await read_owner_export_privacy(
            session
        )  # default owner scope; callers should pass scope
    persisted, updated_at = _chat_export_privacy_marker(privacy.persisted, privacy.updated_at)
    return privacy.store_conversation_history, persisted, updated_at


async def search_conversations(
    session: AsyncSession,
    q: str,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    limit: int,
    offset: int,
    archived: bool,
    include_messages: bool = False,
) -> list[Conversation]:
    """Search retained conversation titles (optionally message text) by literal substring.

    Results are pinned first, then newest first. With ``include_messages`` a conversation also
    matches when any of its messages contains the text; an EXISTS subquery lists each
    conversation once and inherits every owner and retention filter of the outer query.

    Search shows nothing when history storage is off, and never ephemeral,
    expired or automation threads. Deleted rows are gone, so they never match.
    """
    # Lock-free consent read (the export path's); is_history_storage_enabled would hold Memory's advisory lock.
    try:
        if not (
            await _chat_export_privacy(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        )[0]:
            return []
    except ValueError:  # inconsistent privacy marker: fail closed rather than 500
        return []
    escaped = q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    pattern = f"%{escaped}%"
    matches: ColumnElement[bool] = Conversation.title.ilike(pattern, escape="\\")
    if include_messages:
        matches = _or(
            matches,
            _select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                Message.content.ilike(pattern, escape="\\"),
            )
            .exists(),
        )
    # ponytail: ILIKE scan over the owner's own titles/messages; add a pg_trgm index if volume grows.
    statement = (
        _select(Conversation)
        .where(
            Conversation.workspace_id == scope.workspace_id,
            Conversation.actor_user_id == scope.user_id,
            Conversation.archived == archived,
            matches,
        )
        .where(*_chat_export_scope_retained(_datetime.now(_UTC)))
        .order_by(
            Conversation.pinned.desc(), Conversation.updated_at.desc(), Conversation.id.desc()
        )
        .offset(offset)
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


def _chat_export_scope_retained(now: _datetime) -> tuple[ColumnElement[bool], ...]:
    """Retention predicates shared by export scope and search (no snapshot cutoff)."""
    return (
        Conversation.ephemeral.is_(False),
        _or(Conversation.expires_at.is_(None), Conversation.expires_at > now),
        _or(Conversation.context_kind.is_(None), Conversation.context_kind != "automation"),
    )


def _chat_export_scope(
    snapshot_at: _datetime,
    now: _datetime,
    workspace_id: UUID,
) -> tuple[ColumnElement[bool], ...]:
    """Filter to the workspace's retained, non-automation conversation history unchanged at the cutoff."""
    return (
        Conversation.workspace_id == workspace_id,
        Conversation.created_at <= snapshot_at,
        Conversation.updated_at <= snapshot_at,
        *_chat_export_scope_retained(now),
    )


async def _chat_export_count(
    session: AsyncSession,
    record_kind: str,
    snapshot_at: _datetime,
    workspace_id: UUID,
) -> int:
    """Count current owner-visible rows of one workspace at a fixed export cutoff."""
    now = _datetime.now(_UTC)
    scope = _chat_export_scope(snapshot_at, now, workspace_id)
    if record_kind == "conversations":
        statement = _select(_func.count()).select_from(Conversation).where(*scope)
    else:
        statement = (
            _select(_func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(*scope, Message.created_at <= snapshot_at, Message.updated_at <= snapshot_at)
        )
    return int(await session.scalar(statement) or 0)


async def _chat_export_evidence_fences(
    session: AsyncSession,
    refs: Sequence[tuple[UUID, UUID]],
    *,
    scope: _Any,
    multi_workspace_enabled: bool,
) -> dict[tuple[UUID, UUID], tuple[EvidenceReferenceRead, int]]:
    """Resolve exact retained chunks and live source generations for at most one bounded batch."""
    from modules.knowledge.documents import public as documents_public

    unique_refs = list(dict.fromkeys(refs))
    if len(unique_refs) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise ValueError("Chat export page exceeds its exact citation-reference budget")
    if not unique_refs:
        return {}
    try:
        evidence_rows = await documents_public.read_evidence_refs(
            session, unique_refs, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    except ValueError:
        # A deletion can race a page; retry individually to omit only copied citation fields
        # whose exact document/version/chunk fence is no longer owner-visible.
        evidence_rows = []
        for ref in unique_refs:
            try:
                evidence_rows.extend(
                    await documents_public.read_evidence_refs(
                        session, [ref], scope=scope, multi_workspace_enabled=multi_workspace_enabled
                    )
                )
            except ValueError:
                continue
    versions = list(dict.fromkeys(item.document_version_id for item in evidence_rows))
    source_fences = await documents_public.review_version_fences(
        session, versions, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    from modules.sources import public as sources_public
    from modules.sources.public import SourceExportFence

    source_generations: dict[UUID, int] = {}
    conflicting_sources: set[UUID] = set()
    for source_fence in source_fences.values():
        previous = source_generations.setdefault(
            source_fence.source_id, source_fence.current_source_generation
        )
        if previous != source_fence.current_source_generation:
            conflicting_sources.add(source_fence.source_id)
    source_export_fences = [
        SourceExportFence(
            source_id=source_id, workspace_id=scope.workspace_id, generation=generation
        )
        for source_id, generation in source_generations.items()
        if source_id not in conflicting_sources
    ]
    eligible_sources = set(
        await sources_public.filter_export_eligible_sources(
            session,
            source_export_fences,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    resolved: dict[tuple[UUID, UUID], tuple[EvidenceReferenceRead, int]] = {}
    for item in evidence_rows:
        fence = source_fences.get(item.document_version_id)
        if (
            fence is not None
            and fence.document_id == item.document_id
            and fence.source_id == item.source_id
            and fence.source_id in eligible_sources
            and fence.source_id not in conflicting_sources
        ):
            resolved[(item.document_version_id, item.chunk_id)] = (
                item,
                fence.current_source_generation,
            )
    return resolved


async def _export_conversation_records(
    session: AsyncSession,
    *,
    limit: int,
    row_scope: tuple[ColumnElement[bool], ...],
    position: tuple[_datetime, UUID] | None,
) -> tuple[list[ChatExportConversationRead | ChatExportMessageRead], list[ChatExportFence], bool]:
    """Stream one keyset page of retained conversations into byte-budgeted export records.

    Rows are ordered by ``(created_at, id)`` after ``position``; at most ``limit`` records are
    returned and ``has_more`` is set when a further row or the page byte budget stops the scan.
    The server-side stream is always closed. Read-only.

    Raises:
        ValueError: when a single record alone exceeds the page byte budget.
    """
    items: list[ChatExportConversationRead | ChatExportMessageRead] = []
    fences: list[ChatExportFence] = []
    has_more = False
    payload_bytes = 2
    statement = _select(
        Conversation.id.label("conversation_id"),
        Conversation.title.label("title"),
        Conversation.context_kind.label("context_kind"),
        Conversation.context_resource_id.label("context_resource_id"),
        Conversation.pinned.label("pinned"),
        Conversation.archived.label("archived"),
        Conversation.created_at.label("created_at"),
        Conversation.updated_at.label("updated_at"),
    ).where(*row_scope)
    if position is not None:
        statement = statement.where(_tuple(Conversation.created_at, Conversation.id) > position)
    result = await session.stream(
        statement.order_by(Conversation.created_at, Conversation.id)
        .limit(limit + 1)
        .execution_options(yield_per=10)
    )
    try:
        async for row in result.mappings():
            if len(items) == limit:
                has_more = True
                break
            item = ChatExportConversationRead(
                id=row["conversation_id"],
                title=row["title"],
                context_kind=row["context_kind"],
                context_resource_id=row["context_resource_id"],
                pinned=row["pinned"],
                archived=row["archived"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            item_bytes = _chat_export_item_bytes(item)
            proposed_bytes = payload_bytes + item_bytes + (1 if items else 0)
            if proposed_bytes > CHAT_EXPORT_PAGE_MAX_BYTES:
                if not items:
                    raise ValueError("A conversation export record exceeds the page byte budget")
                has_more = True
                break
            items.append(item)
            payload_bytes = proposed_bytes
            fences.append(
                ChatExportFence(
                    conversation_id=row["conversation_id"],
                    conversation_created_at=row["created_at"],
                    conversation_updated_at=row["updated_at"],
                )
            )
    finally:
        await result.close()
    return items, fences, has_more


def _parse_export_citations(
    message_row: dict[str, _Any],
) -> tuple[list[Citation | WebCitation], int]:
    """Parse a retained message's stored citations, counting malformed or non-list entries.

    Returns ``(parsed, omitted)``. A non-list ``citations`` column counts as one omission.

    Raises:
        ValueError: when the content exceeds 1 MiB or more than 100 citations are stored.
    """
    content = message_row["content"]
    if len(content.encode("utf-8")) > 1_048_576:
        raise ValueError("A retained chat message exceeds the export content bound")
    raw_citations = message_row["citations"] if isinstance(message_row["citations"], list) else []
    if len(raw_citations) > 100:
        raise ValueError("A retained message exceeds the citation export bound")
    parsed: list[Citation | WebCitation] = []
    omitted = 0 if isinstance(message_row["citations"], list) else 1
    for raw in raw_citations:
        try:
            is_web = isinstance(raw, dict) and raw.get("sourceType") == "web"
            parsed.append(
                WebCitation.model_validate(raw) if is_web else Citation.model_validate(raw)
            )
        except ValueError:
            omitted += 1
    return parsed, omitted


async def _stream_export_message_candidates(
    session: AsyncSession,
    *,
    limit: int,
    row_scope: tuple[ColumnElement[bool], ...],
    snapshot_at: _datetime,
    position: tuple[_datetime, UUID] | None,
) -> tuple[
    list[tuple[dict[str, _Any], list[Citation | WebCitation], int]], set[tuple[UUID, UUID]], bool
]:
    """Stream message rows into candidates while predicting citation and byte budgets.

    Stops (``has_more``) when the limit, the per-page distinct-evidence/citation count or the
    predicted byte budget would be exceeded. Returns ``(candidates, page_ref_set, has_more)``.
    The server-side stream is always closed. Read-only.
    """
    message_statement = (
        _select(
            Message.id.label("message_id"),
            Message.conversation_id.label("conversation_id"),
            Message.role.label("role"),
            Message.content.label("content"),
            Message.citations.label("citations"),
            Message.response_id.label("response_id"),
            Message.revision_of_message_id.label("revision_of_message_id"),
            Message.created_at.label("message_created_at"),
            Message.updated_at.label("message_updated_at"),
            Conversation.created_at.label("conversation_created_at"),
            Conversation.updated_at.label("conversation_updated_at"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(*row_scope, Message.created_at <= snapshot_at, Message.updated_at <= snapshot_at)
    )
    if position is not None:
        message_statement = message_statement.where(
            _tuple(Message.created_at, Message.id) > position
        )
    result = await session.stream(
        message_statement.order_by(Message.created_at, Message.id)
        .limit(limit + 1)
        .execution_options(yield_per=10)
    )
    candidates: list[tuple[dict[str, _Any], list[Citation | WebCitation], int]] = []
    page_ref_set: set[tuple[UUID, UUID]] = set()
    has_more = False
    page_citation_count = 0
    predicted_candidate_bytes = 0
    try:
        async for raw_row in result.mappings():
            if len(candidates) == limit:
                has_more = True
                break
            message_row = dict(raw_row)
            parsed, omitted = _parse_export_citations(message_row)
            refs_for_message = {
                (message_item.documentVersionId, message_item.chunkId)
                for message_item in parsed
                if isinstance(message_item, Citation)
            }
            predicted_record_bytes = (
                len(_json.dumps(message_row["content"], ensure_ascii=False).encode("utf-8"))
                + len(parsed) * 32_768
                + 1024
            )
            if (
                len(page_ref_set | refs_for_message) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE
                or page_citation_count + len(parsed) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE
                or predicted_candidate_bytes + predicted_record_bytes > CHAT_EXPORT_PAGE_MAX_BYTES
            ):
                has_more = True
                break
            candidates.append((message_row, parsed, omitted))
            page_ref_set.update(refs_for_message)
            page_citation_count += len(parsed)
            predicted_candidate_bytes += predicted_record_bytes
    finally:
        await result.close()
    return candidates, page_ref_set, has_more


def _rebuild_export_citations(
    parsed: list[Citation | WebCitation],
    evidence: dict[tuple[UUID, UUID], tuple[EvidenceReferenceRead, int]],
    omitted: int,
) -> tuple[list[ChatExportCitation | ChatExportWebCitation], list[ChatExportCitationFence], int]:
    """Rebuild export citations from live evidence, counting every omitted citation.

    Document citations are dropped (omitted) when their exact evidence fence is no longer
    owner-visible or its document/source ids disagree with the stored copy; web citations are
    dropped only when their URL cannot be sanitized. Labels are never copied from storage.
    """
    citations: list[ChatExportCitation | ChatExportWebCitation] = []
    citation_fences: list[ChatExportCitationFence] = []
    for citation in parsed:
        if isinstance(citation, WebCitation):
            # No Source, evidence fence or omission: the URL is query-stripped like document URLs.
            safe_url = _safe_chat_export_url(citation.url)
            if safe_url is None:
                omitted += 1
                continue
            citations.append(
                ChatExportWebCitation(
                    url=safe_url,
                    title=citation.title,
                    quote=citation.quote,
                    provider=citation.provider,
                    retrieved_at=citation.retrievedAt,
                )
            )
            continue
        resolved = evidence.get((citation.documentVersionId, citation.chunkId))
        if resolved is None:
            omitted += 1
            continue
        reference, current_generation = resolved
        if reference.document_id != citation.documentId or reference.source_id != citation.sourceId:
            omitted += 1
            continue
        citations.append(
            ChatExportCitation(
                source_id=reference.source_id,
                document_id=reference.document_id,
                document_version_id=reference.document_version_id,
                chunk_id=reference.chunk_id,
                title=reference.title,
                url=_safe_chat_export_url(reference.canonical_url),
                observed_at=reference.observed_at,
                quote=citation.quote,
                current_source_generation=current_generation,
            )
        )
        citation_fences.append(
            ChatExportCitationFence(
                source_id=reference.source_id,
                document_id=reference.document_id,
                document_version_id=reference.document_version_id,
                chunk_id=reference.chunk_id,
                current_source_generation=current_generation,
            )
        )
    return citations, citation_fences, omitted


async def _export_message_records(
    session: AsyncSession,
    *,
    limit: int,
    row_scope: tuple[ColumnElement[bool], ...],
    snapshot_at: _datetime,
    position: tuple[_datetime, UUID] | None,
    scope: _Any,
    multi_workspace_enabled: bool,
) -> tuple[list[ChatExportConversationRead | ChatExportMessageRead], list[ChatExportFence], bool]:
    """Build one keyset page of retained message revisions with live-evidence citations.

    Order: stream candidate rows (stream closed), resolve the page's exact evidence fences in
    one owner call, then build records under the page byte budget. ``has_more`` also holds when
    the byte budget left candidates unserved. Read-only.

    Raises:
        ValueError: when a single record alone exceeds the page byte budget.
    """
    items: list[ChatExportConversationRead | ChatExportMessageRead] = []
    fences: list[ChatExportFence] = []
    payload_bytes = 2
    candidates, page_ref_set, has_more = await _stream_export_message_candidates(
        session,
        limit=limit,
        row_scope=row_scope,
        snapshot_at=snapshot_at,
        position=position,
    )
    evidence = await _chat_export_evidence_fences(
        session, list(page_ref_set), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    for message_row, parsed, initially_omitted in candidates:
        citations, citation_fences, omitted = _rebuild_export_citations(
            parsed, evidence, initially_omitted
        )
        message_item = ChatExportMessageRead(
            id=message_row["message_id"],
            conversation_id=message_row["conversation_id"],
            role=message_row["role"],
            content=message_row["content"],
            response_id=message_row["response_id"],
            revision_of_message_id=message_row["revision_of_message_id"],
            citations=citations,
            omitted_citation_count=omitted,
            created_at=message_row["message_created_at"],
            updated_at=message_row["message_updated_at"],
        )
        item_bytes = _chat_export_item_bytes(message_item)
        proposed_bytes = payload_bytes + item_bytes + (1 if items else 0)
        if proposed_bytes > CHAT_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("A chat message export record exceeds the page byte budget")
            has_more = True
            break
        items.append(message_item)
        payload_bytes = proposed_bytes
        fences.append(
            ChatExportFence(
                conversation_id=message_row["conversation_id"],
                conversation_created_at=message_row["conversation_created_at"],
                conversation_updated_at=message_row["conversation_updated_at"],
                message_id=message_row["message_id"],
                message_created_at=message_row["message_created_at"],
                message_updated_at=message_row["message_updated_at"],
                citations=citation_fences,
            )
        )
    if len(candidates) > len(items):
        has_more = True
    return items, fences, has_more


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: _Any,
    multi_workspace_enabled: bool,
) -> ChatExportPage:
    """Return a bounded owner-authorized page of conversations or retained message revisions.

    The calling export route authenticates the owner and this query rechecks the singleton owner.
    Current privacy settings, ephemeral expiry, deletion, and conversation visibility are applied
    on every page. The cutoff-bound cursor and repeated counts expose page drift; final row/evidence
    fences must still be revalidated immediately before artifact publication. Message JSON internals
    and copied citation labels are never serialized; citation labels are rebuilt from live evidence.
    """
    if record_kind not in {"conversations", "messages"} or not 1 <= limit <= 100:
        raise ValueError("Chat export kind or page limit is invalid")
    await _require_chat_export_owner(
        session, owner_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if cursor is None:
        snapshot_at = _datetime.now(_UTC)
        position = None
    else:
        snapshot_at, position_at, position_id = _decode_chat_export_cursor(
            cursor, owner_id, record_kind
        )
        position = (position_at, position_id)
    history_enabled, privacy_persisted, privacy_updated_at = await _chat_export_privacy(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not history_enabled:
        return ChatExportPage(
            owner_id=owner_id,
            record_kind=record_kind,
            snapshot_at=snapshot_at,
            snapshot_count=0,
            items=[],
            fences=[],
            payload_bytes=2,
            max_payload_bytes=CHAT_EXPORT_PAGE_MAX_BYTES,
            next_cursor=None,
            available=False,
            omission_reason="conversation_history_disabled",
            privacy_persisted=privacy_persisted,
            privacy_updated_at=privacy_updated_at,
            history_enabled=False,
        )
    snapshot_count = await _chat_export_count(session, record_kind, snapshot_at, scope.workspace_id)
    now = _datetime.now(_UTC)
    row_scope = _chat_export_scope(snapshot_at, now, scope.workspace_id)

    if record_kind == "conversations":
        items, fences, has_more = await _export_conversation_records(
            session, limit=limit, row_scope=row_scope, position=position
        )
    else:
        items, fences, has_more = await _export_message_records(
            session,
            limit=limit,
            row_scope=row_scope,
            snapshot_at=snapshot_at,
            position=position,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    next_cursor = (
        _encode_chat_export_cursor(
            owner_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id
        )
        if has_more and items
        else None
    )

    if len(items) != len(fences):
        raise RuntimeError("Chat export page lost a record fence")
    if sum(len(fence.citations) for fence in fences) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise RuntimeError("Chat export page exceeded its citation fence budget")
    return ChatExportPage(
        owner_id=owner_id,
        record_kind=record_kind,
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        items=items,
        fences=fences,
        payload_bytes=_chat_export_payload_bytes(items),
        max_payload_bytes=CHAT_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor,
        available=True,
        omission_reason=None,
        privacy_persisted=privacy_persisted,
        privacy_updated_at=privacy_updated_at,
        history_enabled=True,
    )


async def _revalidate_export_rows(
    session: AsyncSession,
    *,
    record_kind: str,
    fences: Sequence[ChatExportFence],
    scope: _Any,
) -> tuple[str | None, list[ChatExportCitationFence]]:
    """Recheck every fenced conversation (and message) row against its recorded revision.

    Returns ``("record_changed", [])`` for the first row that vanished, changed, expired,
    became ephemeral or automation-owned; otherwise ``(None, citation_fences)`` with the
    message fences' citation fences gathered for evidence revalidation. Read-only; fresh
    time is taken per fence.

    Raises:
        ValueError: when a fence carries an identity inconsistent with ``record_kind``.
    """
    citation_fences: list[ChatExportCitationFence] = []
    for fence in fences:
        now = _datetime.now(_UTC)
        conversation = (
            await session.execute(
                _select(
                    Conversation.created_at,
                    Conversation.updated_at,
                    Conversation.ephemeral,
                    Conversation.expires_at,
                    Conversation.context_kind,
                ).where(
                    Conversation.id == fence.conversation_id,
                    Conversation.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if conversation is None or (
            conversation.created_at != fence.conversation_created_at
            or conversation.updated_at != fence.conversation_updated_at
            or conversation.ephemeral
            or conversation.expires_at is not None
            and conversation.expires_at <= now
            or conversation.context_kind == "automation"
        ):
            return "record_changed", []
        if record_kind == "conversations":
            if fence.message_id is not None:
                raise ValueError("Conversation export fence cannot contain a message identity")
        else:
            if (
                fence.message_id is None
                or fence.message_created_at is None
                or fence.message_updated_at is None
            ):
                raise ValueError("Message export fence is missing its transcript revision identity")
            message = (
                await session.execute(
                    _select(Message.created_at, Message.updated_at).where(
                        Message.id == fence.message_id,
                        Message.conversation_id == fence.conversation_id,
                    )
                )
            ).one_or_none()
            if message is None or (
                message.created_at != fence.message_created_at
                or message.updated_at != fence.message_updated_at
            ):
                return "record_changed", []
            citation_fences.extend(fence.citations)
    return None, citation_fences


async def _revalidate_export_citations(
    session: AsyncSession,
    citation_fences: list[ChatExportCitationFence],
    *,
    scope: _Any,
    multi_workspace_enabled: bool,
) -> str | None:
    """Recheck the exact evidence, source eligibility and generation behind every citation fence.

    Returns ``None`` when all citations are still valid, else the failure reason
    (``citation_unavailable`` or ``source_generation_changed``). Read-only.

    Raises:
        ValueError: when the distinct reference count exceeds the revalidation budget.
    """
    from modules.knowledge.documents import public as documents_public

    refs = list(
        dict.fromkeys((item.document_version_id, item.chunk_id) for item in citation_fences)
    )
    if len(refs) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise ValueError("Chat citation fences exceed the revalidation reference budget")
    evidence: dict[tuple[UUID, UUID], EvidenceReferenceRead] = {}
    for start in range(0, len(refs), CHAT_EXPORT_MAX_CITATIONS_PER_PAGE):
        batch = refs[start : start + CHAT_EXPORT_MAX_CITATIONS_PER_PAGE]
        try:
            evidence.update(
                {
                    (row.document_version_id, row.chunk_id): row
                    for row in await documents_public.read_evidence_refs(
                        session, batch, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                    )
                }
            )
        except ValueError:
            return "citation_unavailable"
    version_ids = list(dict.fromkeys(version_id for version_id, _chunk_id in refs))
    current_sources = await documents_public.review_version_fences(
        session, version_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    from modules.sources import public as sources_public
    from modules.sources.public import SourceExportFence

    current_generations: dict[UUID, int] = {}
    conflicting_sources: set[UUID] = set()
    cited_source_ids = {citation.source_id for citation in citation_fences}
    for current in current_sources.values():
        if current.source_id not in cited_source_ids:
            continue
        previous = current_generations.setdefault(
            current.source_id, current.current_source_generation
        )
        if previous != current.current_source_generation:
            conflicting_sources.add(current.source_id)
    eligible_source_fences = [
        SourceExportFence(
            source_id=source_id, workspace_id=scope.workspace_id, generation=generation
        )
        for source_id, generation in current_generations.items()
        if source_id not in conflicting_sources
    ]
    eligible_sources = set(
        await sources_public.filter_export_eligible_sources(
            session,
            eligible_source_fences,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    for citation in citation_fences:
        ref = evidence.get((citation.document_version_id, citation.chunk_id))
        current_fence = current_sources.get(citation.document_version_id)
        if (
            ref is None
            or current_fence is None
            or ref.document_id != citation.document_id
            or ref.source_id != citation.source_id
            or current_fence.document_id != citation.document_id
            or current_fence.source_id != citation.source_id
            or citation.source_id not in eligible_sources
            or citation.source_id in conflicting_sources
        ):
            return "citation_unavailable"
        if current_fence.current_source_generation != citation.current_source_generation:
            return "source_generation_changed"
    return None


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: _datetime,
    expected_snapshot_count: int,
    privacy_persisted: bool,
    privacy_updated_at: _datetime | None,
    fences: Sequence[ChatExportFence],
    scope: _Any,
    multi_workspace_enabled: bool,
) -> ChatExportFenceValidation:
    """Revalidate bounded chat rows, privacy, deletion, and exact citation evidence before publication.

    Check order (first failure wins): owner, history setting, privacy marker, snapshot count,
    per-row fences, then citation evidence. Read-only; invalid input raises ``ValueError``.
    """
    if (
        record_kind not in {"conversations", "messages"}
        or not 0 <= expected_snapshot_count <= 2**63 - 1
    ):
        raise ValueError("Chat export revalidation input is invalid")
    privacy_persisted, privacy_updated_at = _chat_export_privacy_marker(
        privacy_persisted, privacy_updated_at
    )
    if (
        len(fences) > 100
        or sum(len(fence.citations) for fence in fences) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE
    ):
        raise ValueError("Chat export revalidation exceeds its bounded page contract")
    if (
        not _export_owner_allowed(owner_id, scope, multi_workspace_enabled)
        or await session.scalar(_select(_Owner.id).where(_Owner.id == owner_id)) is None
    ):
        return ChatExportFenceValidation(
            valid=False,
            reason="owner_unavailable",
            observed_snapshot_count=0,
            privacy_persisted=privacy_persisted,
            privacy_updated_at=privacy_updated_at,
        )
    (
        history_enabled,
        current_privacy_persisted,
        current_privacy_updated_at,
    ) = await _chat_export_privacy(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not history_enabled:
        return ChatExportFenceValidation(
            valid=False,
            reason="conversation_history_disabled",
            observed_snapshot_count=0,
            privacy_persisted=current_privacy_persisted,
            privacy_updated_at=current_privacy_updated_at,
        )
    if (current_privacy_persisted, current_privacy_updated_at) != (
        privacy_persisted,
        privacy_updated_at,
    ):
        return ChatExportFenceValidation(
            valid=False,
            reason="privacy_changed",
            observed_snapshot_count=0,
            privacy_persisted=current_privacy_persisted,
            privacy_updated_at=current_privacy_updated_at,
        )
    observed_count = await _chat_export_count(session, record_kind, snapshot_at, scope.workspace_id)

    def verdict(valid: bool, reason: str) -> ChatExportFenceValidation:
        """Build the result sharing the observed count and current privacy marker."""
        return ChatExportFenceValidation(
            valid=valid,
            reason=reason,
            observed_snapshot_count=observed_count,
            privacy_persisted=current_privacy_persisted,
            privacy_updated_at=current_privacy_updated_at,
        )

    if observed_count != expected_snapshot_count:
        return verdict(False, "snapshot_count_changed")
    row_failure, citation_fences = await _revalidate_export_rows(
        session, record_kind=record_kind, fences=fences, scope=scope
    )
    if row_failure is not None:
        return verdict(False, row_failure)
    if record_kind == "messages" and citation_fences:
        citation_failure = await _revalidate_export_citations(
            session, citation_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if citation_failure is not None:
            return verdict(False, citation_failure)
    return verdict(True, "valid")
