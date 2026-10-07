"""Public module contracts and API boundary for the chat capability."""

import base64 as _base64
import binascii as _binascii
import hashlib as _hashlib
import json as _json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass as _dataclass
from datetime import UTC as _UTC
from datetime import datetime as _datetime
from typing import Any as _Any
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import ColumnElement
from sqlalchemy import func as _func
from sqlalchemy import or_ as _or
from sqlalchemy import select as _select
from sqlalchemy import tuple_ as _tuple
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner as _Owner
from core.telemetry import RunMeta as _RunMeta
from modules.chat.citations import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    ensure_grounded_answer,
    validate_answer_citations,
    validate_citations,
)
from modules.chat.models import (
    AgentActivityLink,
    Conversation,
    Message,
    ResponseRun,
    StreamEvent,
)
from modules.chat.retrieval import (
    build_context,
    format_grounded_context,
    revalidate_context_fence,
)
from modules.chat.schemas import (
    AgentActivityRead,
    AnswerContext,
    AnswerContextRequest,
    CancelResponse,
    ChatExportCitation,
    ChatExportCitationFence,
    ChatExportConversationRead,
    ChatExportFence,
    ChatExportFenceValidation,
    ChatExportMessageRead,
    ChatExportPage,
    ChatMemoryExportOrigin,
    Citation,
    CitationValidationResult,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    EntityContextItem,
    EvidenceItem,
    MessageRead,
    ResponseRunRead,
    SelectedDocumentVersion,
    SelectedEvidenceRef,
    SendMessageRequest,
    SendMessageResponse,
    TemporalContextItem,
    ValidatedAnswer,
)
from modules.chat.seed import ensure_demo_conversation
from modules.chat.stream import (
    StreamBuffer,
    format_sse_event,
    make_event_id,
    parse_event_id,
)
from modules.chat.worker import (
    _privacy_cancel_locked,
    is_history_storage_enabled,
    process_chat_response,
    purge_expired_chat_runs,
    run_response_generation,
)
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope, EvidenceReferenceRead


@_dataclass(frozen=True)
class CopiedEvidenceCleanupProgress:
    """Describe one bounded Chat cleanup page without changing stream identities."""

    next_cursor: str | None
    complete: bool
    rows_examined: int
    rows_changed: int


_REMOVE_EVIDENCE = object()
_EVIDENCE_FIELDS = {
    "sourcetype", "source_type", "sourceid", "source_id", "documentid", "document_id",
    "documentversionid", "document_version_id", "chunkid", "chunk_id", "title", "url",
    "canonical_url", "observedat", "observed_at", "quote", "content", "excerpt",
    "content_hash", "chunk_index", "version_number", "score",
}

__all__ = [
    "INSUFFICIENT_EVIDENCE_MESSAGE",
    "AgentActivityLink",
    "AgentActivityRead",
    "AnswerContext",
    "AnswerContextRequest",
    "CancelResponse",
    "ChatExportCitation",
    "ChatExportCitationFence",
    "ChatExportConversationRead",
    "ChatExportFence",
    "ChatExportFenceValidation",
    "ChatExportMessageRead",
    "ChatExportPage",
    "ChatMemoryExportOrigin",
    "Citation",
    "CitationValidationResult",
    "Conversation",
    "ConversationCreate",
    "ConversationDetailRead",
    "ConversationPatch",
    "ConversationRead",
    "CopiedEvidenceCleanupProgress",
    "EntityContextItem",
    "EvidenceItem",
    "Message",
    "MessageRead",
    "ResponseRun",
    "ResponseRunRead",
    "SelectedEvidenceRef",
    "SendMessageRequest",
    "SendMessageResponse",
    "StreamBuffer",
    "StreamEvent",
    "TemporalContextItem",
    "ValidatedAnswer",
    "authorize_agent_run_access",
    "build_context",
    "delete_conversation",
    "ensure_demo_conversation",
    "ensure_grounded_answer",
    "export_page",
    "filter_current_citations",
    "filter_live_agent_run_ids",
    "format_grounded_context",
    "format_sse_event",
    "get_agent_activity",
    "has_live_agent_run_link",
    "is_history_storage_enabled",
    "link_agent_run",
    "list_agent_run_ids_for_delete",
    "list_agent_run_ids_for_owner",
    "live_agent_conversation_id",
    "make_event_id",
    "parse_event_id",
    "process_chat_response",
    "publish_agent_activity",
    "purge_document_copied_evidence_page",
    "purge_expired_chat_runs",
    "purge_unpinned_conversations",
    "read_memory_export_origin",
    "revalidate_context_fence",
    "run_response_generation",
    "validate_answer_citations",
    "validate_citations",
    "validate_export_fences",
]


async def delete_conversation(
    session: AsyncSession, conversation_id: UUID, owner_id: int, *, skip_pinned: bool = False,
) -> bool:
    """Delete one conversation inside the caller's transaction.

    Lock order is Memory privacy fence, Chat conversation parent, then Agent run/action rows.
    Owner and any requested pin filter are checked after the parent lock is acquired. This function
    never commits; the route or owning service commits after its unit of work.
    """
    from modules.memory.public import lock_export_privacy

    await lock_export_privacy(session)
    conversation = await session.scalar(
        _select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        return False
    owner = await session.scalar(_select(_Owner.id).where(_Owner.id == owner_id))
    if owner is None or (skip_pinned and conversation.pinned):
        return False

    from modules.agents.public import purge_conversation_actions

    await purge_conversation_actions(session, conversation_id, owner_id)
    await session.delete(conversation)
    return True


async def purge_unpinned_conversations(session: AsyncSession, owner_id: int) -> int:
    """Delete unpinned Chat history in bounded UUID keyset pages, without committing."""
    deleted = 0
    cursor: UUID | None = None
    while True:
        stmt = _select(Conversation.id).where(Conversation.pinned.is_(False))
        if cursor is not None:
            stmt = stmt.where(Conversation.id > cursor)
        conversation_ids = list((await session.scalars(
            stmt.order_by(Conversation.id).limit(100)
        )).all())
        if not conversation_ids:
            return deleted
        for conversation_id in conversation_ids:
            if await delete_conversation(
                session, conversation_id, owner_id, skip_pinned=True,
            ):
                deleted += 1
        cursor = conversation_ids[-1]


async def resolve_gadget_context(session: AsyncSession, context: dict[str, _Any] | None) -> dict[str, _Any]:
    """Normalize exact gadget selections to JSON-safe refs and owner-derived source fences.

    Every item is re-resolved through Documents' active current-version and provider-scope policy at
    send time. Persisted IDs use JSON strings; worker intake reparses typed refs and revalidates each
    captured generation/scope before reading, reranking, remote egress, or streaming callbacks.
    """
    if not context:
        return {}
    if context.get("kind") != "selection":
        # This server-derived flag is reserved for validated exact gadget selections.
        # Strip server-reserved keys: "selected_only" and every "_"-prefixed private key.
        return {key: value for key, value in context.items() if key != "selected_only" and not key.startswith("_")}
    from fastapi import HTTPException

    from modules.knowledge.documents import public as documents_public
    from modules.knowledge.documents.schemas import GadgetDocumentSelectionFence

    raw_items = context.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 32:
        raise HTTPException(status_code=422, detail="Selection must contain 1 to 32 document versions")
    selections: list[SelectedDocumentVersion] = []
    try:
        selections = [SelectedDocumentVersion.model_validate(item) for item in raw_items]
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Selection references are invalid") from exc
    if len({item.document_id for item in selections}) != len(selections):
        raise HTTPException(status_code=422, detail="Selection contains duplicate documents")

    sources: list[UUID] = []
    refs: list[dict[str, str]] = []
    selection_fences: list[GadgetDocumentSelectionFence] = []
    for item in selections:
        projection = await documents_public.get_news_document_projection(
            session, item.document_id,
        )
        if (
            projection is None or projection.source_id != item.source_id
            or projection.document_version_id != item.document_version_id
        ):
            raise HTTPException(status_code=409, detail="A selected document version is stale or unavailable")
        fence = GadgetDocumentSelectionFence(
            document_id=projection.document_id,
            document_version_id=projection.document_version_id,
            source_id=projection.source_id,
            source_generation=projection.current_source_generation,
            source_type=projection.source_type,
            provider=projection.provider,
            local_only=projection.local_only,
            scope_discriminator=projection.scope_discriminator,
        )
        if not await documents_public.validate_gadget_document_selection_fences(session, (fence,)):
            raise HTTPException(status_code=409, detail="A selected document source scope is stale")
        selection_fences.append(fence)
        if item.chunk_id is not None:
            chunks = await documents_public.read_chat_evidence_chunks(
                session, [(item.document_version_id, item.chunk_id)],
                require_current_version=True, selection_fences=(fence,),
            )
            if not chunks or chunks[0].document_id != item.document_id or chunks[0].source_id != item.source_id:
                raise HTTPException(status_code=409, detail="A selected evidence chunk is unavailable")
            # The exact chunk may be beyond the bounded projection slice; preserve its validated ID.
            chunk_ids = [item.chunk_id]
        else:
            if projection.chunks_truncated:
                raise HTTPException(
                    status_code=422,
                    detail="Select a specific evidence chunk for documents above the context limit",
                )
            chunk_ids = [chunk.id for chunk in projection.chunks]
        if not chunk_ids:
            raise HTTPException(status_code=409, detail="A selected document has no available evidence chunks")
        for chunk_id in chunk_ids:
            refs.append({
                "document_version_id": str(projection.document_version_id),
                "chunk_id": str(chunk_id),
                "document_id": str(projection.document_id),
                "source_id": str(projection.source_id),
            })
        if item.source_id not in sources:
            sources.append(item.source_id)
        if len(refs) > 100:
            raise HTTPException(status_code=422, detail="Selection exceeds the evidence limit")

    return {
        "source_scope": [str(source_id) for source_id in sources],
        "selected_refs": refs,
        "selection_fences": [fence.model_dump(mode="json") for fence in selection_fences],
        "selected_only": True,
    }


async def link_agent_run(
    session: AsyncSession,
    conversation_id: UUID,
    agent_run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
) -> None:
    """Create a live conversation-owned activity link after owner-session and expiry checks.

    Ephemeral links inherit the conversation deadline or receive a 24-hour retention deadline;
    already expired conversations cannot acquire new activity rows.
    """
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException
    from sqlalchemy import select

    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conversation is None or owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    now = datetime.now(UTC)
    if conversation.expires_at is not None and conversation.expires_at <= now:
        raise HTTPException(status_code=404, detail="Conversation not found")
    ephemeral = conversation.ephemeral or not await is_history_storage_enabled(session)
    expires_at = conversation.expires_at or (now + timedelta(hours=24) if ephemeral else None)
    session.add(AgentActivityLink(
        conversation_id=conversation_id, agent_run_id=agent_run_id, owner_id=owner_id,
        auth_session_hash=auth_session_hash, ephemeral=ephemeral,
        expires_at=expires_at,
        activities=[{"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}],
    ))


async def publish_agent_activity(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    status: str,
    tool_name: str | None = None,
) -> None:
    """Append bounded identifiers only while the linked conversation and owner session are live.

    A current durable run status is replayable by the worker reconciler; duplicate consecutive
    status publications are ignored to keep that bounded retry path from filling activity history.
    The conversation is locked before the refreshed link row; expiry is checked with fresh time
    after both lifecycle locks and owner-session revalidation, immediately before append.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from core.auth.public import revalidate_owner_session

    if status not in {"queued", "running", "waiting_approval", "succeeded", "failed", "cancelled", "started", "denied"}:
        return
    async with session_factory() as session:
        candidate_conversation_id = await session.scalar(
            select(AgentActivityLink.conversation_id).where(AgentActivityLink.agent_run_id == run_id)
        )
        if candidate_conversation_id is None:
            return
        # Match conversation deletion's parent-before-child lock order to avoid cascade deadlocks.
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == candidate_conversation_id).with_for_update()
        )
        link = await session.scalar(
            select(AgentActivityLink).where(AgentActivityLink.agent_run_id == run_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if (link is None or link.owner_id != owner_id or link.auth_session_hash != auth_session_hash
                or conversation is None or link.conversation_id != conversation.id
                or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
            return
        # Take fresh time after waiting on lifecycle locks and revalidating the owner session.
        now = datetime.now(UTC)
        if (link.expires_at is not None and link.expires_at <= now
                or conversation.expires_at is not None and conversation.expires_at <= now):
            return
        latest_status = next((
            item.get("status") for item in reversed(link.activities)
            if isinstance(item, dict) and item.get("kind") == "status"
        ), None)
        if tool_name is None and latest_status == status:
            return
        event: dict[str, object] = {"kind": "tool" if tool_name else "status", "status": status}
        if tool_name:
            event["tool_name"] = tool_name[:160]
        event["created_at"] = now.isoformat()
        link.activities = [*link.activities[-63:], event]
        await session.commit()


async def get_agent_activity(
    session: AsyncSession, conversation_id: UUID, run_id: UUID, owner_id: int,
    auth_session_hash: str,
) -> AgentActivityRead:
    """Read safe activity only through the requested live conversation and original owner session."""
    from fastapi import HTTPException
    from sqlalchemy import select

    link = await session.scalar(select(AgentActivityLink).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
    ))
    from datetime import UTC, datetime

    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if (link is None or link.auth_session_hash != auth_session_hash
            or link.expires_at is not None and link.expires_at <= datetime.now(UTC)
            or conversation is None
            or conversation.expires_at is not None and conversation.expires_at <= datetime.now(UTC)
            or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
        raise HTTPException(status_code=404, detail="Agent activity not found")
    return AgentActivityRead(
        conversation_id=link.conversation_id, agent_run_id=link.agent_run_id,
        activities=link.activities, updated_at=link.updated_at,
    )


async def list_agent_run_ids_for_owner(
    session: AsyncSession, conversation_id: UUID, owner_id: int, auth_session_hash: str,
) -> list[UUID]:
    """Return at most 50 live run links from the authenticated conversation without exposing chat storage."""
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from sqlalchemy import select

    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if (conversation is None or owner_id != 1
            or conversation.expires_at is not None and conversation.expires_at <= datetime.now(UTC)
            or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
        raise HTTPException(status_code=404, detail="Conversation not found")
    statement = select(AgentActivityLink.agent_run_id).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
        AgentActivityLink.auth_session_hash == auth_session_hash,
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > datetime.now(UTC))),
    ).order_by(AgentActivityLink.updated_at.desc(), AgentActivityLink.agent_run_id).limit(50)
    return list((await session.scalars(statement)).all())


async def filter_live_agent_run_ids(
    session: AsyncSession,
    run_ids: list[UUID],
    owner_id: int,
    auth_session_hash: str,
    *,
    conversation_id: UUID | None = None,
) -> frozenset[UUID]:
    """Return bounded run links visible to the current owner within Chat's live retention window.

    This query stays inside Chat because it owns conversations and activity links. Callers may
    pass candidate run IDs only; Chat revalidates the current owner session and retention window.
    Ephemeral links remain bound to their creating session, while retained persistent history is
    available to the authenticated owner after a session rotation.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from core.auth.public import revalidate_owner_session

    if not run_ids or len(run_ids) > 100 or len(set(run_ids)) != len(run_ids):
        return frozenset()
    if owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return frozenset()
    now = datetime.now(UTC)
    statement = select(AgentActivityLink.agent_run_id).join(
        Conversation, Conversation.id == AgentActivityLink.conversation_id,
    ).where(
        AgentActivityLink.agent_run_id.in_(run_ids),
        AgentActivityLink.owner_id == owner_id,
        (~AgentActivityLink.ephemeral | (AgentActivityLink.auth_session_hash == auth_session_hash)),
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > now)),
        (Conversation.expires_at.is_(None) | (Conversation.expires_at > now)),
    )
    if conversation_id is not None:
        statement = statement.where(AgentActivityLink.conversation_id == conversation_id)
    return frozenset((await session.scalars(statement)).all())


async def authorize_agent_run_access(
    session: AsyncSession,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    *,
    require_original_session: bool = False,
    lock_conversation: bool = False,
) -> bool:
    """Authorize a linked run read or action through its live Chat conversation.

    Current owner sessions may read retained persistent history. Ephemeral reads and every
    action require the link's original session digest. Cancellation may lock the Conversation
    before the agent run row so it follows Chat deletion's parent-before-child lock order.
    Deleted, expired, unlinked, or unauthorized conversations fail closed.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from core.auth.public import revalidate_owner_session

    if owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return False
    conversation_id = await session.scalar(select(AgentActivityLink.conversation_id).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
    ))
    if conversation_id is None:
        return False

    conversation = None
    if lock_conversation:
        # Chat deletion locks the parent conversation before linked agent state; keep that order.
        conversation = await session.scalar(select(Conversation).where(
            Conversation.id == conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        if conversation is None:
            return False

    link = await session.scalar(select(AgentActivityLink).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
    ).execution_options(populate_existing=True))
    if link is None:
        return False

    if conversation is None:
        conversation = await session.scalar(select(Conversation).where(
            Conversation.id == conversation_id,
        ).execution_options(populate_existing=True))
    if conversation is None or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return False
    now = datetime.now(UTC)
    if ((link.expires_at is not None and link.expires_at <= now)
            or (conversation.expires_at is not None and conversation.expires_at <= now)):
        return False
    if (require_original_session or link.ephemeral) and link.auth_session_hash != auth_session_hash:  # noqa: SIM103  # style-only rewrite skipped to avoid touching control flow
        return False
    return True


async def has_live_agent_run_link(
    session: AsyncSession, run_id: UUID, owner_id: int, auth_session_hash: str,
) -> bool:
    """Authorize one action through a live link bound to the original owner session digest."""
    return await live_agent_conversation_id(session, run_id, owner_id, auth_session_hash) is not None


async def live_agent_conversation_id(
    session: AsyncSession, run_id: UUID, owner_id: int, auth_session_hash: str,
) -> UUID | None:
    """Return the linked conversation only while its owner and retention fences remain live."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    now = datetime.now(UTC)
    if owner_id != 1:
        return None
    statement = select(AgentActivityLink.conversation_id).join(
        Conversation, Conversation.id == AgentActivityLink.conversation_id,
    ).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
        AgentActivityLink.auth_session_hash == auth_session_hash,
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > now)),
        (Conversation.expires_at.is_(None) | (Conversation.expires_at > now)),
    )
    return await session.scalar(statement)


async def list_agent_run_ids_for_delete(
    session: AsyncSession, conversation_id: UUID, owner_id: int, after: UUID | None = None,
) -> list[UUID]:
    """Return one bounded ordered page of linked runs for Chat's deletion-time privacy cleanup contract."""
    from sqlalchemy import select

    statement = select(AgentActivityLink.agent_run_id).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
    )
    if after is not None:
        statement = statement.where(AgentActivityLink.agent_run_id > after)
    return list((await session.scalars(statement.order_by(AgentActivityLink.agent_run_id).limit(50))).all())


async def list_run_meta(session: AsyncSession, limit: int) -> list[_RunMeta]:
    """Return at most ``limit`` (<=100) newest chat response runs as metadata only: no messages, errors text or context."""
    rows = await session.scalars(_select(ResponseRun).order_by(ResponseRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="chat", id=str(r.id), status=r.status, error_code=r.error_code,
                     created_at=r.created_at, updated_at=r.updated_at, finished_at=r.completed_at,
                     model_identity=r.model_name, token_usage=r.token_usage or None)
            for r in rows]


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID) -> _RunMeta | None:
    """Return one metadata-only response-run projection by its indexed primary key."""
    row = await session.get(ResponseRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="chat", id=str(row.id), status=row.status, error_code=row.error_code,
                    created_at=row.created_at, updated_at=row.updated_at, finished_at=row.completed_at,
                    model_identity=row.model_name, token_usage=row.token_usage or None)


CHAT_EXPORT_PAGE_MAX_BYTES = 16_777_216
CHAT_EXPORT_MAX_CITATIONS_PER_PAGE = 100


def _encode_chat_export_cursor(
    owner_id: int, record_kind: str, snapshot_at: _datetime, position_at: _datetime, position_id: UUID,
) -> str:
    """Encode a canonical owner/kind/cutoff-bound chat keyset cursor."""
    payload = {
        "v": 1, "owner": owner_id, "kind": record_kind,
        "snapshot": snapshot_at.astimezone(_UTC).isoformat(),
        "at": position_at.astimezone(_UTC).isoformat(), "id": str(position_id),
    }
    raw = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_chat_export_cursor(
    cursor: str, owner_id: int, record_kind: str,
) -> tuple[_datetime, _datetime, UUID]:
    """Decode a strict canonical cursor and reject owner or record-kind substitution."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid chat export cursor")
        raw = _base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = _json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"v", "owner", "kind", "snapshot", "at", "id"}:
            raise ValueError("Invalid chat export cursor")
        if payload["v"] != 1 or payload["owner"] != owner_id or payload["kind"] != record_kind:
            raise ValueError("Chat export cursor belongs to another owner or record kind")
        snapshot_at = _datetime.fromisoformat(payload["snapshot"])
        position_at = _datetime.fromisoformat(payload["at"])
        if any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, position_at)):
            raise ValueError("Chat export cursor timestamps must be timezone-aware")
        snapshot_at, position_at = snapshot_at.astimezone(_UTC), position_at.astimezone(_UTC)
        if snapshot_at > _datetime.now(_UTC):
            raise ValueError("Chat export cursor cutoff cannot be in the future")
        position_id = UUID(payload["id"])
        if _encode_chat_export_cursor(owner_id, record_kind, snapshot_at, position_at, position_id) != cursor:
            raise ValueError("Chat export cursor is not canonical")
        return snapshot_at, position_at, position_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, _binascii.Error, _json.JSONDecodeError) as exc:
        raise ValueError("Invalid chat export cursor") from exc


def _chat_export_payload_bytes(items: Sequence[BaseModel]) -> int:
    """Measure serialized item-array bytes for the aggregate export-page budget."""
    return len(_json.dumps(
        [item.model_dump(mode="json") for item in items],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


def _chat_export_item_bytes(item: BaseModel) -> int:
    """Measure one serialized chat record so page admission avoids repeated full-array encoding."""
    return len(_json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _safe_chat_export_url(value: str | None) -> str | None:
    """Remove credentials, query values, fragments, and non-web URLs from citations."""
    if not value:
        return None
    try:
        parsed = _urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return _urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


async def _require_chat_export_owner(session: AsyncSession, owner_id: int) -> None:
    """Require the live singleton owner before reading owner-scoped chat history."""
    if owner_id != 1 or await session.scalar(_select(_Owner.id).where(_Owner.id == owner_id)) is None:
        raise PermissionError("Chat export requires the current owner")


async def read_memory_export_origin(
    session: AsyncSession, *, owner_id: int, conversation_id: UUID, message_id: UUID,
) -> ChatMemoryExportOrigin | None:
    """Fence an exact retained transcript message for Memory without exposing its text.

    The read observes current history consent and the live non-ephemeral owner conversation.
    Its digest binds content, citations, and both lifecycle rows so Memory can recheck this
    evidence immediately before its download is published.
    """
    from modules.memory.public import lock_export_privacy

    await lock_export_privacy(session)
    try:
        await _require_chat_export_owner(session, owner_id)
    except PermissionError:
        return None
    history_enabled, persisted, privacy_updated_at = await _chat_export_privacy(session)
    if not history_enabled:
        return None
    now = _datetime.now(_UTC)
    conversation_scope = _chat_export_scope(now, now)
    row = (await session.execute(
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
            Conversation.id == conversation_id, Message.id == message_id,
            Message.conversation_id == conversation_id, *conversation_scope,
            Message.created_at <= now, Message.updated_at <= now,
        )
    )).one_or_none()
    if row is None:
        return None
    digest_input = _json.dumps(
        {"content": row.content, "citations": row.citations},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return ChatMemoryExportOrigin(
        conversation_id=row.conversation_id, message_id=row.message_id,
        conversation_created_at=row.conversation_created_at,
        conversation_updated_at=row.conversation_updated_at,
        message_created_at=row.message_created_at, message_updated_at=row.message_updated_at,
        content_citation_digest=_hashlib.sha256(digest_input).hexdigest(),
        privacy_persisted=persisted, privacy_updated_at=privacy_updated_at,
    )


def _chat_export_privacy_marker(persisted: bool, updated_at: _datetime | None) -> tuple[bool, _datetime | None]:
    """Validate the compact Memory-owned history-privacy persistence and timestamp fence."""
    if type(persisted) is not bool or persisted != (updated_at is not None):
        raise ValueError("Chat export privacy marker is inconsistent")
    if (updated_at is not None
            and (not isinstance(updated_at, _datetime) or updated_at.tzinfo is None or updated_at.utcoffset() is None)):
        raise ValueError("Chat export privacy timestamp must be timezone-aware")
    return persisted, updated_at


async def _chat_export_privacy(session: AsyncSession) -> tuple[bool, bool, _datetime | None]:
    """Read Memory's current history-storage grant and its minimal persisted-row fence."""
    from modules.memory import public as memory_public

    privacy = await memory_public.read_export_privacy(session)
    persisted, updated_at = _chat_export_privacy_marker(privacy.persisted, privacy.updated_at)
    return privacy.store_conversation_history, persisted, updated_at


def _chat_export_scope(snapshot_at: _datetime, now: _datetime) -> tuple[ColumnElement[bool], ...]:
    """Filter to retained, non-automation conversation history unchanged at the cutoff."""
    return (
        Conversation.created_at <= snapshot_at,
        Conversation.updated_at <= snapshot_at,
        Conversation.ephemeral.is_(False),
        _or(Conversation.expires_at.is_(None), Conversation.expires_at > now),
        _or(Conversation.context_kind.is_(None), Conversation.context_kind != "automation"),
    )


async def _chat_export_count(session: AsyncSession, record_kind: str, snapshot_at: _datetime) -> int:
    """Count current owner-visible rows at a fixed export cutoff."""
    now = _datetime.now(_UTC)
    scope = _chat_export_scope(snapshot_at, now)
    if record_kind == "conversations":
        statement = _select(_func.count()).select_from(Conversation).where(*scope)
    else:
        statement = (
            _select(_func.count()).select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(*scope, Message.created_at <= snapshot_at, Message.updated_at <= snapshot_at)
        )
    return int(await session.scalar(statement) or 0)


async def _chat_export_evidence_fences(
    session: AsyncSession, refs: Sequence[tuple[UUID, UUID]],
) -> dict[tuple[UUID, UUID], tuple[EvidenceReferenceRead, int]]:
    """Resolve exact retained chunks and live source generations for at most one bounded batch."""
    from modules.knowledge.documents import public as documents_public

    unique_refs = list(dict.fromkeys(refs))
    if len(unique_refs) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise ValueError("Chat export page exceeds its exact citation-reference budget")
    if not unique_refs:
        return {}
    try:
        evidence_rows = await documents_public.read_evidence_refs(session, unique_refs)
    except ValueError:
        # A deletion can race a page; retry individually to omit only copied citation fields
        # whose exact document/version/chunk fence is no longer owner-visible.
        evidence_rows = []
        for ref in unique_refs:
            try:
                evidence_rows.extend(await documents_public.read_evidence_refs(session, [ref]))
            except ValueError:
                continue
    versions = list(dict.fromkeys(item.document_version_id for item in evidence_rows))
    source_fences = await documents_public.review_version_fences(session, versions)
    from modules.sources import public as sources_public
    from modules.sources.schemas import SourceExportFence

    source_generations: dict[UUID, int] = {}
    conflicting_sources: set[UUID] = set()
    for source_fence in source_fences.values():
        previous = source_generations.setdefault(source_fence.source_id, source_fence.current_source_generation)
        if previous != source_fence.current_source_generation:
            conflicting_sources.add(source_fence.source_id)
    source_export_fences = [
        SourceExportFence(source_id=source_id, generation=generation)
        for source_id, generation in source_generations.items()
        if source_id not in conflicting_sources
    ]
    eligible_sources = set(await sources_public.filter_export_eligible_sources(session, source_export_fences))
    resolved: dict[tuple[UUID, UUID], tuple[EvidenceReferenceRead, int]] = {}
    for item in evidence_rows:
        fence = source_fences.get(item.document_version_id)
        if (fence is not None and fence.document_id == item.document_id and fence.source_id == item.source_id
                and fence.source_id in eligible_sources and fence.source_id not in conflicting_sources):
            resolved[(item.document_version_id, item.chunk_id)] = (item, fence.current_source_generation)
    return resolved


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
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
    await _require_chat_export_owner(session, owner_id)
    if cursor is None:
        snapshot_at = _datetime.now(_UTC)
        position = None
    else:
        snapshot_at, position_at, position_id = _decode_chat_export_cursor(cursor, owner_id, record_kind)
        position = (position_at, position_id)
    history_enabled, privacy_persisted, privacy_updated_at = await _chat_export_privacy(session)
    if not history_enabled:
        return ChatExportPage(
            owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
            snapshot_count=0, items=[], fences=[], payload_bytes=2, max_payload_bytes=CHAT_EXPORT_PAGE_MAX_BYTES,
            next_cursor=None, available=False, omission_reason="conversation_history_disabled",
            privacy_persisted=privacy_persisted, privacy_updated_at=privacy_updated_at, history_enabled=False,
        )
    snapshot_count = await _chat_export_count(session, record_kind, snapshot_at)
    now = _datetime.now(_UTC)
    scope = _chat_export_scope(snapshot_at, now)
    items: list[ChatExportConversationRead | ChatExportMessageRead] = []
    fences: list[ChatExportFence] = []
    has_more = False
    payload_bytes = 2

    if record_kind == "conversations":
        statement = _select(
            Conversation.id.label("conversation_id"), Conversation.title.label("title"),
            Conversation.context_kind.label("context_kind"),
            Conversation.context_resource_id.label("context_resource_id"),
            Conversation.pinned.label("pinned"), Conversation.archived.label("archived"),
            Conversation.created_at.label("created_at"), Conversation.updated_at.label("updated_at"),
        ).where(*scope)
        if position is not None:
            statement = statement.where(_tuple(Conversation.created_at, Conversation.id) > position)
        result = await session.stream(
            statement.order_by(Conversation.created_at, Conversation.id)
            .limit(limit + 1).execution_options(yield_per=10)
        )
        try:
            async for row in result.mappings():
                if len(items) == limit:
                    has_more = True
                    break
                item = ChatExportConversationRead(
                    id=row["conversation_id"], title=row["title"], context_kind=row["context_kind"],
                    context_resource_id=row["context_resource_id"], pinned=row["pinned"],
                    archived=row["archived"], created_at=row["created_at"], updated_at=row["updated_at"],
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
                fences.append(ChatExportFence(
                    conversation_id=row["conversation_id"], conversation_created_at=row["created_at"],
                    conversation_updated_at=row["updated_at"],
                ))
        finally:
            await result.close()
        next_cursor = (
            _encode_chat_export_cursor(owner_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id)
            if has_more and items else None
        )
    else:
        message_statement = (
            _select(
                Message.id.label("message_id"), Message.conversation_id.label("conversation_id"),
                Message.role.label("role"), Message.content.label("content"),
                Message.citations.label("citations"), Message.response_id.label("response_id"),
                Message.revision_of_message_id.label("revision_of_message_id"),
                Message.created_at.label("message_created_at"), Message.updated_at.label("message_updated_at"),
                Conversation.created_at.label("conversation_created_at"),
                Conversation.updated_at.label("conversation_updated_at"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(*scope, Message.created_at <= snapshot_at, Message.updated_at <= snapshot_at)
        )
        if position is not None:
            message_statement = message_statement.where(_tuple(Message.created_at, Message.id) > position)
        result = await session.stream(
            message_statement.order_by(Message.created_at, Message.id)
            .limit(limit + 1).execution_options(yield_per=10)
        )
        candidates: list[tuple[dict[str, _Any], list[Citation], int]] = []
        page_ref_set: set[tuple[UUID, UUID]] = set()
        page_citation_count = 0
        predicted_candidate_bytes = 0
        try:
            async for raw_row in result.mappings():
                if len(candidates) == limit:
                    has_more = True
                    break
                message_row = dict(raw_row)
                content = message_row["content"]
                if len(content.encode("utf-8")) > 1_048_576:
                    raise ValueError("A retained chat message exceeds the export content bound")
                raw_citations = message_row["citations"] if isinstance(message_row["citations"], list) else []
                if len(raw_citations) > 100:
                    raise ValueError("A retained message exceeds the citation export bound")
                parsed: list[Citation] = []
                omitted = 0 if isinstance(message_row["citations"], list) else 1
                for raw in raw_citations:
                    try:
                        parsed.append(Citation.model_validate(raw))
                    except ValueError:
                        omitted += 1
                refs_for_message = {(message_item.documentVersionId, message_item.chunkId) for message_item in parsed}
                predicted_record_bytes = (
                    len(_json.dumps(content, ensure_ascii=False).encode("utf-8"))
                    + len(parsed) * 32_768 + 1024
                )
                if (len(page_ref_set | refs_for_message) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE
                        or page_citation_count + len(parsed) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE
                        or predicted_candidate_bytes + predicted_record_bytes > CHAT_EXPORT_PAGE_MAX_BYTES):
                    has_more = True
                    break
                candidates.append((message_row, parsed, omitted))
                page_ref_set.update(refs_for_message)
                page_citation_count += len(parsed)
                predicted_candidate_bytes += predicted_record_bytes
        finally:
            await result.close()

        evidence = await _chat_export_evidence_fences(session, list(page_ref_set))
        for message_row, parsed, initially_omitted in candidates:
            citations: list[ChatExportCitation] = []
            citation_fences: list[ChatExportCitationFence] = []
            omitted = initially_omitted
            for citation in parsed:
                resolved = evidence.get((citation.documentVersionId, citation.chunkId))
                if resolved is None:
                    omitted += 1
                    continue
                reference, current_generation = resolved
                if (reference.document_id != citation.documentId or reference.source_id != citation.sourceId):
                    omitted += 1
                    continue
                citations.append(ChatExportCitation(
                    source_id=reference.source_id, document_id=reference.document_id,
                    document_version_id=reference.document_version_id, chunk_id=reference.chunk_id,
                    title=reference.title, url=_safe_chat_export_url(reference.canonical_url),
                    observed_at=reference.observed_at, quote=citation.quote,
                    current_source_generation=current_generation,
                ))
                citation_fences.append(ChatExportCitationFence(
                    source_id=reference.source_id, document_id=reference.document_id,
                    document_version_id=reference.document_version_id, chunk_id=reference.chunk_id,
                    current_source_generation=current_generation,
                ))
            message_item = ChatExportMessageRead(
                id=message_row["message_id"], conversation_id=message_row["conversation_id"], role=message_row["role"],
                content=message_row["content"], response_id=message_row["response_id"],
                revision_of_message_id=message_row["revision_of_message_id"],
                citations=citations, omitted_citation_count=omitted,
                created_at=message_row["message_created_at"], updated_at=message_row["message_updated_at"],
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
            fences.append(ChatExportFence(
                conversation_id=message_row["conversation_id"],
                conversation_created_at=message_row["conversation_created_at"],
                conversation_updated_at=message_row["conversation_updated_at"], message_id=message_row["message_id"],
                message_created_at=message_row["message_created_at"], message_updated_at=message_row["message_updated_at"],
                citations=citation_fences,
            ))
        if len(candidates) > len(items):
            has_more = True
        next_cursor = (
            _encode_chat_export_cursor(owner_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id)
            if has_more and items else None
        )

    if len(items) != len(fences):
        raise RuntimeError("Chat export page lost a record fence")
    if sum(len(fence.citations) for fence in fences) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise RuntimeError("Chat export page exceeded its citation fence budget")
    return ChatExportPage(
        owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=_chat_export_payload_bytes(items), max_payload_bytes=CHAT_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor, available=True, omission_reason=None,
        privacy_persisted=privacy_persisted, privacy_updated_at=privacy_updated_at, history_enabled=True,
    )


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
) -> ChatExportFenceValidation:
    """Revalidate bounded chat rows, privacy, deletion, and exact citation evidence before publication."""
    if record_kind not in {"conversations", "messages"} or not 0 <= expected_snapshot_count <= 2**63 - 1:
        raise ValueError("Chat export revalidation input is invalid")
    privacy_persisted, privacy_updated_at = _chat_export_privacy_marker(privacy_persisted, privacy_updated_at)
    if len(fences) > 100 or sum(len(fence.citations) for fence in fences) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
        raise ValueError("Chat export revalidation exceeds its bounded page contract")
    if owner_id != 1 or await session.scalar(_select(_Owner.id).where(_Owner.id == owner_id)) is None:
        return ChatExportFenceValidation(
            valid=False, reason="owner_unavailable", observed_snapshot_count=0,
            privacy_persisted=privacy_persisted, privacy_updated_at=privacy_updated_at,
        )
    history_enabled, current_privacy_persisted, current_privacy_updated_at = await _chat_export_privacy(session)
    if not history_enabled:
        return ChatExportFenceValidation(
            valid=False, reason="conversation_history_disabled", observed_snapshot_count=0,
            privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
        )
    if (current_privacy_persisted, current_privacy_updated_at) != (privacy_persisted, privacy_updated_at):
        return ChatExportFenceValidation(
            valid=False, reason="privacy_changed", observed_snapshot_count=0,
            privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
        )
    observed_count = await _chat_export_count(session, record_kind, snapshot_at)
    if observed_count != expected_snapshot_count:
        return ChatExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed_count,
            privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
        )
    citation_fences: list[ChatExportCitationFence] = []
    for fence in fences:
        now = _datetime.now(_UTC)
        conversation = (await session.execute(
            _select(Conversation.created_at, Conversation.updated_at, Conversation.ephemeral,
                    Conversation.expires_at, Conversation.context_kind)
            .where(Conversation.id == fence.conversation_id)
        )).one_or_none()
        if conversation is None or (
            conversation.created_at != fence.conversation_created_at
            or conversation.updated_at != fence.conversation_updated_at
            or conversation.ephemeral
            or conversation.expires_at is not None and conversation.expires_at <= now
            or conversation.context_kind == "automation"
        ):
            return ChatExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed_count,
                privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
            )
        if record_kind == "conversations":
            if fence.message_id is not None:
                raise ValueError("Conversation export fence cannot contain a message identity")
        else:
            if fence.message_id is None or fence.message_created_at is None or fence.message_updated_at is None:
                raise ValueError("Message export fence is missing its transcript revision identity")
            message = (await session.execute(
                _select(Message.created_at, Message.updated_at)
                .where(Message.id == fence.message_id, Message.conversation_id == fence.conversation_id)
            )).one_or_none()
            if message is None or (
                message.created_at != fence.message_created_at or message.updated_at != fence.message_updated_at
            ):
                return ChatExportFenceValidation(
                    valid=False, reason="record_changed", observed_snapshot_count=observed_count,
                    privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
                )
            citation_fences.extend(fence.citations)
    if record_kind == "messages" and citation_fences:
        from modules.knowledge.documents import public as documents_public

        refs = list(dict.fromkeys((item.document_version_id, item.chunk_id) for item in citation_fences))
        if len(refs) > CHAT_EXPORT_MAX_CITATIONS_PER_PAGE:
            raise ValueError("Chat citation fences exceed the revalidation reference budget")
        evidence: dict[tuple[UUID, UUID], EvidenceReferenceRead] = {}
        for start in range(0, len(refs), CHAT_EXPORT_MAX_CITATIONS_PER_PAGE):
            batch = refs[start:start + CHAT_EXPORT_MAX_CITATIONS_PER_PAGE]
            try:
                evidence.update({(row.document_version_id, row.chunk_id): row
                                 for row in await documents_public.read_evidence_refs(session, batch)})
            except ValueError:
                return ChatExportFenceValidation(
                    valid=False, reason="citation_unavailable", observed_snapshot_count=observed_count,
                    privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
                )
        version_ids = list(dict.fromkeys(version_id for version_id, _chunk_id in refs))
        current_sources = await documents_public.review_version_fences(session, version_ids)
        from modules.sources import public as sources_public
        from modules.sources.schemas import SourceExportFence

        current_generations: dict[UUID, int] = {}
        conflicting_sources: set[UUID] = set()
        cited_source_ids = {citation.source_id for citation in citation_fences}
        for current in current_sources.values():
            if current.source_id not in cited_source_ids:
                continue
            previous = current_generations.setdefault(current.source_id, current.current_source_generation)
            if previous != current.current_source_generation:
                conflicting_sources.add(current.source_id)
        eligible_source_fences = [
            SourceExportFence(source_id=source_id, generation=generation)
            for source_id, generation in current_generations.items()
            if source_id not in conflicting_sources
        ]
        eligible_sources = set(await sources_public.filter_export_eligible_sources(session, eligible_source_fences))
        for citation in citation_fences:
            ref = evidence.get((citation.document_version_id, citation.chunk_id))
            current_fence = current_sources.get(citation.document_version_id)
            if (ref is None or current_fence is None or ref.document_id != citation.document_id
                    or ref.source_id != citation.source_id or current_fence.document_id != citation.document_id
                    or current_fence.source_id != citation.source_id or citation.source_id not in eligible_sources
                    or citation.source_id in conflicting_sources):
                return ChatExportFenceValidation(
                    valid=False, reason="citation_unavailable", observed_snapshot_count=observed_count,
                    privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
                )
            if current_fence.current_source_generation != citation.current_source_generation:
                return ChatExportFenceValidation(
                    valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count,
                    privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
                )
    return ChatExportFenceValidation(
        valid=True, reason="valid", observed_snapshot_count=observed_count,
        privacy_persisted=current_privacy_persisted, privacy_updated_at=current_privacy_updated_at,
    )


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
    version_ids = {item.document_version_id for item in scope.references if item.reference_kind == "version"}
    chunk_refs = {
        (item.document_version_id, item.chunk_id)
        for item in scope.references if item.reference_kind == "chunk" and item.chunk_id is not None
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
    citations: object, scope: DocumentCleanupEvidenceScope,
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
            [item.reference_kind, str(item.document_version_id), str(item.chunk_id) if item.chunk_id else None]
            for item in scope.references
        ],
    }
    return _hashlib.sha256(_json.dumps(material, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _encode_cleanup_cursor(
    scope: DocumentCleanupEvidenceScope, kind: str, after: UUID | None,
) -> str:
    """Encode a deterministic Chat-table keyset cursor bound to this evidence page."""
    payload = {"v": 1, "operation": str(scope.operation_id), "fingerprint": _cleanup_scope_fingerprint(scope),
               "kind": kind, "after": str(after) if after else None}
    raw = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cleanup_cursor(
    cursor: str, scope: DocumentCleanupEvidenceScope,
) -> tuple[str, UUID | None]:
    """Reject malformed, non-canonical, cross-operation, and cross-page cleanup cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid Chat copied-evidence cursor")
        raw = _base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = _json.loads(raw)
        if (not isinstance(payload, dict)
                or set(payload) != {"v", "operation", "fingerprint", "kind", "after"}
                or payload["v"] != 1 or payload["operation"] != str(scope.operation_id)
                or payload["fingerprint"] != _cleanup_scope_fingerprint(scope)
                or payload["kind"] not in {"messages", "runs", "events"}):
            raise ValueError("Chat copied-evidence cursor does not match this operation page")
        after = UUID(payload["after"]) if payload["after"] is not None else None
        if _encode_cleanup_cursor(scope, payload["kind"], after) != cursor:
            raise ValueError("Chat copied-evidence cursor is not canonical")
        return payload["kind"], after
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, _binascii.Error, _json.JSONDecodeError) as exc:
        raise ValueError("Invalid Chat copied-evidence cursor") from exc


async def filter_current_citations(
    session: AsyncSession,
    citations: object,
) -> list[dict[str, object]]:
    """Return only citations whose exact document, version, and chunk still exist.

    The Documents owner supplies detached current evidence; absent or mismatched references
    are omitted before Chat history or SSE replay can expose their copied title, URL, or quote.
    Evidence row locks remain held until the caller's short response transaction completes.
    """
    if not isinstance(citations, list) or not citations:
        return []
    refs: list[tuple[UUID, UUID]] = []
    parsed: list[tuple[dict[str, object], UUID, UUID, UUID, UUID]] = []
    for raw in citations:
        if not isinstance(raw, dict):
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
    if not unique_refs:
        return []
    from modules.knowledge.documents import public as documents_public

    current = {}
    for start in range(0, len(unique_refs), 100):
        evidence = await documents_public.lock_chat_evidence_chunks(
            session, unique_refs[start:start + 100], require_active_source=False,
        )
        current.update({(item.document_version_id, item.chunk_id): item for item in evidence})
    return [
        raw for raw, source_id, document_id, version_id, chunk_id in parsed
        if (item := current.get((version_id, chunk_id))) is not None
        and item.source_id == source_id and item.document_id == document_id
    ]


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    scope: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> CopiedEvidenceCleanupProgress:
    """Clean one bounded Chat table page using detached identities captured before hard deletion.

    Lock order is Memory privacy, Conversation parent, ResponseRun, then StreamEvent. This
    function mutates only Chat-owned rows; it preserves message text, unrelated citations,
    append-only mutation receipts, and every existing stream/event/run identity. The Documents
    caller persists the returned cursor and page mutations in the same transaction. Version-only
    and chunk identity records can fall on different scope pages; each exact chunk pair matches
    independently of the separate version-only record.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Chat copied-evidence page size must be between 1 and 100")
    if len(scope.references) > 100:
        raise ValueError("Chat copied-evidence identity page exceeds 100 references")
    if cursor is None:
        kind, after = "messages", None
    else:
        kind, after = _decode_cleanup_cursor(cursor, scope)
    from modules.memory.public import bound_cleanup_lock_waits, lock_export_privacy

    await lock_export_privacy(session)
    await bound_cleanup_lock_waits(session)
    kinds = ("messages", "runs", "events")
    index = kinds.index(kind)
    examined = 0
    changed = 0
    while index < len(kinds) and examined < limit:
        kind = kinds[index]
        remaining = limit - examined
        found: list[_Any]
        if kind == "messages":
            found = list((await session.execute(
                _select(Message.id, Message.conversation_id)
                .where(*([Message.id > after] if after else []))
                .order_by(Message.id).limit(remaining + 1)
            )).all())
        elif kind == "runs":
            found = list((await session.execute(
                _select(ResponseRun.id, ResponseRun.conversation_id)
                .where(*([ResponseRun.id > after] if after else []))
                .order_by(ResponseRun.id).limit(remaining + 1)
            )).all())
        else:
            found = list((await session.execute(
                _select(StreamEvent.id, StreamEvent.response_id, ResponseRun.conversation_id)
                .join(ResponseRun, ResponseRun.id == StreamEvent.response_id)
                .where(*([StreamEvent.id > after] if after else []))
                .order_by(StreamEvent.id).limit(remaining + 1)
            )).all())
        has_more = len(found) > remaining
        rows = found[:remaining]
        examined += len(rows)
        for candidate in rows:
            if kind == "messages":
                row_id, conversation_id = candidate
                parent = await session.scalar(_select(Conversation).where(
                    Conversation.id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if parent is None:
                    continue
                message = await session.scalar(_select(Message).where(
                    Message.id == row_id, Message.conversation_id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if message is None:
                    continue
                citations, citations_changed = _filter_citation_values(message.citations, scope)
                metadata, metadata_changed = _scrub_cleanup_payload(message.metadata_json or {}, scope)
                if citations_changed or metadata_changed:
                    message.citations = citations
                    message.metadata_json = metadata if isinstance(metadata, dict) else {}
                    changed += 1
            elif kind == "runs":
                row_id, conversation_id = candidate
                parent = await session.scalar(_select(Conversation).where(
                    Conversation.id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if parent is None:
                    continue
                run = await session.scalar(_select(ResponseRun).where(
                    ResponseRun.id == row_id, ResponseRun.conversation_id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if run is None:
                    continue
                citations, citations_changed = _filter_citation_values(run.citations, scope)
                context, context_changed = _scrub_cleanup_payload(run.retrieval_context or {}, scope)
                if citations_changed or context_changed:
                    run.citations = citations
                    run.retrieval_context = context if isinstance(context, dict) else {}
                    changed += 1
                    if run.status in {"pending", "streaming"}:
                        await _privacy_cancel_locked(session, run)
            else:
                row_id, response_id, conversation_id = candidate
                parent = await session.scalar(_select(Conversation).where(
                    Conversation.id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if parent is None:
                    continue
                run = await session.scalar(_select(ResponseRun).where(
                    ResponseRun.id == response_id, ResponseRun.conversation_id == conversation_id,
                ).with_for_update().execution_options(populate_existing=True))
                if run is None:
                    continue
                event = await session.scalar(_select(StreamEvent).where(
                    StreamEvent.id == row_id, StreamEvent.response_id == response_id,
                ).with_for_update().execution_options(populate_existing=True))
                if event is None:
                    continue
                payload, payload_changed = _scrub_cleanup_payload(event.data or {}, scope)
                if payload_changed:
                    event.data = payload if isinstance(payload, dict) else {}
                    changed += 1
                    if run.status in {"pending", "streaming"}:
                        await _privacy_cancel_locked(session, run, event.seq)
        if has_more:
            last_id = rows[-1][0]
            return CopiedEvidenceCleanupProgress(
                next_cursor=_encode_cleanup_cursor(scope, kind, last_id),
                complete=False, rows_examined=examined, rows_changed=changed,
            )
        after = None
        index += 1
        if examined >= limit and index < len(kinds):
            return CopiedEvidenceCleanupProgress(
                next_cursor=_encode_cleanup_cursor(scope, kinds[index], None),
                complete=False, rows_examined=examined, rows_changed=changed,
            )
    return CopiedEvidenceCleanupProgress(
        next_cursor=None, complete=True, rows_examined=examined, rows_changed=changed,
    )
