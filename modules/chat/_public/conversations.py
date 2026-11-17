"""Conversation deletion, gadget selection context and answer-context helpers behind the chat public contract."""

# Cross-module contract re-exports (importers use this public module, never private submodules).

# isort: split
from typing import Any as _Any
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis as _Redis
from sqlalchemy import delete as _delete
from sqlalchemy import func as _func
from sqlalchemy import or_ as _or
from sqlalchemy import select as _select
from sqlalchemy import union as _union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker as _async_sessionmaker

from core.config import Settings as _Settings
from core.errors import (
    ConflictError,
    DomainError,
    ValidationFailedError,
)
from core.workspaces.schemas import Scope, WorkspaceContext
from modules.chat.models import (
    ChatTemplate,
    Conversation,
    Message,
    ResponseRun,
)
from modules.chat.schemas import (
    AnswerContext,
    AnswerContextRequest,
    SelectedDocumentVersion,
)


async def delete_conversation(
    session: AsyncSession,
    conversation_id: UUID,
    owner_id: int,
    *,
    skip_pinned: bool = False,
) -> bool:
    """Delete one conversation inside the caller's transaction.

    Lock order is Memory privacy fence, Chat conversation parent, then Agent run/action rows.
    Owner and any requested pin filter are checked after the parent lock is acquired. Model-saved
    templates whose provenance run belongs to this conversation are deleted with it. This function
    never commits; the route or owning service commits after its unit of work.
    """

    from modules.agents.public import purge_conversation_actions
    from modules.chat.scope import multi_workspace_enabled, owner_default_scope
    from modules.memory.public import lock_export_privacy

    try:
        scope = await owner_default_scope(session, owner_id)
    except (HTTPException, DomainError):
        return False
    await lock_export_privacy(session, scope.workspace_id)
    conversation = await session.scalar(
        _select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.workspace_id == scope.workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None or (skip_pinned and conversation.pinned):
        return False

    await purge_conversation_actions(
        session,
        conversation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled(),
    )
    # Todos cascade with the conversation; templates are owner-scoped, so delete the ones the model saved in it.
    await session.execute(
        _delete(ChatTemplate).where(
            ChatTemplate.source_response_id.in_(
                _select(ResponseRun.id).where(ResponseRun.conversation_id == conversation_id)
            )
        )
    )
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
        conversation_ids = list(
            (await session.scalars(stmt.order_by(Conversation.id).limit(100))).all()
        )
        if not conversation_ids:
            return deleted
        for conversation_id in conversation_ids:
            if await delete_conversation(
                session,
                conversation_id,
                owner_id,
                skip_pinned=True,
            ):
                deleted += 1
        cursor = conversation_ids[-1]


def _selection_detail(code: str, message: str) -> dict[str, _Any]:
    """Build the machine-coded ``HTTPException`` detail (``core/errors.py`` envelope) for selection/context failures."""
    return {"code": code, "message": message, "details": {}}


MAX_SCOPE_VALUE_CHARS = 200
MAX_SCOPE_LIST_ITEMS = 20


def bounded_scope_value(value: _Any) -> _Any:
    """Bound one client-supplied gadget scope value before it is stored and echoed into the system prompt.

    Strings are cut to ``MAX_SCOPE_VALUE_CHARS``; lists keep at most ``MAX_SCOPE_LIST_ITEMS`` scalar items
    (nested containers are dropped), each bounded the same way. Pure; scalars other than ``str`` pass through.
    """
    if isinstance(value, str):
        return value[:MAX_SCOPE_VALUE_CHARS]
    if isinstance(value, list):
        return [
            item[:MAX_SCOPE_VALUE_CHARS] if isinstance(item, str) else item
            for item in value[:MAX_SCOPE_LIST_ITEMS]
            if isinstance(item, (str, int, float, bool))
        ]
    return value


async def _resolve_gadget_instance_context(
    session: AsyncSession,
    context: dict[str, _Any],
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> dict[str, _Any]:
    """Validate a dashboard gadget context and bind its authorized active source IDs to ``source_scope``.

    Args:
        session: Active asynchronous database session.
        context: Client-supplied gadget context dict (``kind == 'gadget'``).
        scope: Admitted workspace context.
        multi_workspace_enabled: Rollout flag for multi-workspace checks.

    Returns:
        Normalized retrieval context carrying ``kind``, ``gadget_id``, ``title``, ``renderer``,
        ``source_scope`` (always present, possibly empty), and ``gadget_scope``.
    """
    from modules.sources import public as sources_public

    raw_sources = (
        context.get("sourceIds") or context.get("source_ids") or context.get("source_scope") or []
    )
    valid_sources: list[str] = []
    if isinstance(raw_sources, list):
        seen: set[UUID] = set()
        for item in raw_sources[:32]:
            try:
                sid = UUID(str(item))
            except (ValueError, TypeError):
                continue
            if sid in seen:
                continue
            seen.add(sid)
            src = await sources_public.get_source(
                session,
                sid,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if src is not None and src.status == "active" and not src.local_only:
                valid_sources.append(str(sid))

    raw_scope_obj = context.get("scope")
    raw_scope: dict[_Any, _Any] = raw_scope_obj if isinstance(raw_scope_obj, dict) else {}
    safe_scope = {
        str(k)[:64]: bounded_scope_value(v)
        for k, v in list(raw_scope.items())[:16]
        if isinstance(v, (str, int, float, bool, list))
    }
    out: dict[str, _Any] = {
        "kind": "gadget",
        "gadget_id": str(context.get("gadgetId") or context.get("gadget_id") or "")[:128],
        "title": str(context.get("title") or "")[:200],
        "renderer": str(context.get("renderer") or "")[:64],
        "gadget_scope": safe_scope,
    }
    # Always stored, even empty: an absent scope would widen the data tool to the whole workspace, so an
    # empty list (every gadget source paused, local-only or deleted) must stay an explicit "no sources".
    out["source_scope"] = valid_sources
    return out


def _parse_selection_items(raw_items: _Any) -> list[SelectedDocumentVersion]:
    """Validate the raw ``items`` list of a selection context into typed, unique selections.

    Raises:
        ValidationFailedError: (422, ``selection_invalid``) for a missing/oversized list,
            malformed references, or duplicate documents.
    """
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 32:
        raise ValidationFailedError(
            _selection_detail(
                "selection_invalid", "Selection must contain 1 to 32 document versions"
            )
        )
    selections: list[SelectedDocumentVersion] = []
    try:
        selections = [SelectedDocumentVersion.model_validate(item) for item in raw_items]
    except Exception as exc:
        raise ValidationFailedError(
            _selection_detail("selection_invalid", "Selection references are invalid")
        ) from exc
    if len({item.document_id for item in selections}) != len(selections):
        raise ValidationFailedError(
            _selection_detail("selection_invalid", "Selection contains duplicate documents")
        )
    return selections


async def _resolve_selected_chunk_ids(
    session: AsyncSession,
    item: SelectedDocumentVersion,
    projection: _Any,
    fence: _Any,
    scope_kw: dict[str, _Any],
) -> list[UUID]:
    """Return the evidence chunk ids a selection item contributes (an exact chunk or all chunks).

    An exact chunk is re-read under the captured fence; without one the whole document must fit
    the bounded projection slice.

    Raises:
        ConflictError: (409) when the chunk is unavailable or the document has no chunks.
        ValidationFailedError: (422, ``selection_too_large``) when the projection was truncated.
    """
    from modules.knowledge.documents import public as documents_public

    if item.chunk_id is not None:
        chunks = await documents_public.read_chat_evidence_chunks(
            session,
            [(item.document_version_id, item.chunk_id)],
            require_current_version=True,
            selection_fences=(fence,),
            **scope_kw,
        )
        if (
            not chunks
            or chunks[0].document_id != item.document_id
            or chunks[0].source_id != item.source_id
        ):
            raise ConflictError(
                _selection_detail(
                    "selection_unavailable", "A selected evidence chunk is unavailable"
                )
            )
        # The exact chunk may be beyond the bounded projection slice; preserve its validated ID.
        chunk_ids = [item.chunk_id]
    else:
        if projection.chunks_truncated:
            raise ValidationFailedError(
                _selection_detail(
                    "selection_too_large",
                    "Select a specific evidence chunk for documents above the context limit",
                ),
            )
        chunk_ids = [chunk.id for chunk in projection.chunks]
    if not chunk_ids:
        raise ConflictError(
            _selection_detail(
                "selection_unavailable", "A selected document has no available evidence chunks"
            )
        )
    return chunk_ids


async def resolve_gadget_context(
    session: AsyncSession,
    context: dict[str, _Any] | None,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> dict[str, _Any]:
    """Normalize exact gadget selections to JSON-safe refs and owner-derived source fences.

    Every item is re-resolved through Documents' active current-version and provider-scope policy at
    send time. Persisted IDs use JSON strings; worker intake reparses typed refs and revalidates each
    captured generation/scope before reading, reranking, remote egress, or streaming callbacks.
    Read-only; raises ``DomainError`` subclasses (422 invalid/too large, 409 stale/unavailable).
    """
    if not context:
        return {}
    if context.get("kind") == "gadget":
        return await _resolve_gadget_instance_context(
            session,
            context,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if context.get("kind") != "selection":
        # This server-derived flag is reserved for validated exact gadget selections.
        # Strip server-reserved keys: "selected_only" and every "_"-prefixed private key.
        return {
            key: value
            for key, value in context.items()
            if key != "selected_only" and not key.startswith("_")
        }
    from modules.knowledge.documents import public as documents_public
    from modules.knowledge.documents.public import GadgetDocumentSelectionFence

    selections = _parse_selection_items(context.get("items"))

    scope_kw: dict[str, _Any] = {"scope": scope, "multi_workspace_enabled": multi_workspace_enabled}
    sources: list[UUID] = []
    refs: list[dict[str, str]] = []
    selection_fences: list[GadgetDocumentSelectionFence] = []
    for item in selections:
        projection = await documents_public.get_news_document_projection(
            session,
            item.document_id,
            **scope_kw,
        )
        if (
            projection is None
            or projection.source_id != item.source_id
            or projection.document_version_id != item.document_version_id
        ):
            raise ConflictError(
                _selection_detail(
                    "selection_unavailable", "A selected document version is stale or unavailable"
                )
            )
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
        if not await documents_public.validate_gadget_document_selection_fences(
            session,
            (fence,),
            **scope_kw,
        ):
            raise ConflictError("A selected document source scope is stale")
        selection_fences.append(fence)
        chunk_ids = await _resolve_selected_chunk_ids(session, item, projection, fence, scope_kw)
        for chunk_id in chunk_ids:
            refs.append(
                {
                    "document_version_id": str(projection.document_version_id),
                    "chunk_id": str(chunk_id),
                    "document_id": str(projection.document_id),
                    "source_id": str(projection.source_id),
                }
            )
        if item.source_id not in sources:
            sources.append(item.source_id)
        if len(refs) > 100:
            raise ValidationFailedError(
                _selection_detail("selection_too_large", "Selection exceeds the evidence limit")
            )

    return {
        "source_scope": [str(source_id) for source_id in sources],
        "selected_refs": refs,
        "selection_fences": [fence.model_dump(mode="json") for fence in selection_fences],
        "selected_only": True,
    }


MAX_CHAT_ATTACHMENTS_PER_MESSAGE = 5


CHAT_SELECTION_LOCAL_ONLY = "selection_local_only"


async def reject_unsendable_selection(
    session: AsyncSession,
    resolved: dict[str, _Any] | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Refuse, before any run exists, a selection that chat could never answer from.

    Chat always streams through the remote model gateway, and the worker privacy-cancels any run
    whose evidence is ``local_only`` before a request opens. Refusing here makes that outcome
    visible (409, machine code ``selection_local_only``) instead of a silently cancelled answer.
    ``local_only`` is re-read from each fence's current source, so edit/regenerate (which reuse a
    stored fence snapshot) see the source's present state. Chat attachments are also capped per
    message at the boundary (422).
    """

    from modules.sources import public as sources_public

    fences = (resolved or {}).get("selection_fences") or []
    attachments = 0
    local_only = False
    for fence in fences:
        source = await sources_public.get_source(
            session,
            UUID(str(fence["source_id"])),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            source is not None
            and (source.configuration or {}).get(sources_public.CHAT_ATTACHMENTS_MARKER) is True
        ):
            attachments += 1
        local_only = (
            local_only
            or bool(fence.get("local_only"))
            or (source is not None and source.local_only)
        )
    if attachments > MAX_CHAT_ATTACHMENTS_PER_MESSAGE:
        raise ValidationFailedError(
            _selection_detail(
                "selection_too_large", "Selection exceeds the per-message attachment limit"
            )
        )
    if local_only:
        raise ConflictError(
            {
                "code": CHAT_SELECTION_LOCAL_ONLY,
                "message": "Selection includes local-only documents, which are never sent to the cloud chat model",
                "details": {},
            }
        )


async def count_source_conversations(
    session: AsyncSession, source_id: UUID, *, scope: Scope, cap: int = 1000
) -> int:
    """Return an owner-UI-only count of conversations citing a source, saturating at ``cap``."""
    key = str(source_id)
    # ponytail: seq scan over chat_messages; add GIN(citations jsonb_path_ops) if dialog open gets slow
    cites = _or(
        Message.citations.contains([{"source_id": key}]),  # worker writes the alias key
        Message.citations.contains([{"sourceId": key}]),  # older rows
    )
    ids = _union(
        _select(Message.conversation_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.workspace_id == scope.workspace_id, cites),
        _select(ResponseRun.conversation_id).where(
            ResponseRun.workspace_id == scope.workspace_id,
            ResponseRun.retrieval_context["source_scope"].contains([key]),
        ),
    ).subquery()
    capped = _select(ids.c.conversation_id).limit(cap).subquery()
    return int(await session.scalar(_select(_func.count()).select_from(capped)) or 0)


async def build_answer_context(
    session: AsyncSession,
    session_factory: _async_sessionmaker[AsyncSession],
    redis: _Redis,
    settings: _Settings,
    request: AnswerContextRequest,
    *,
    scope: Scope,
) -> AnswerContext:
    """Compose grounded answer context for Knowledge's service facade (delegates to ``chat.retrieval.build_context``).

    Thin contract wrapper so Knowledge does not import Chat's private retrieval module. Arguments, transaction use
    (the caller's ``session`` plus short isolated ``session_factory`` sessions), authorization (the caller's scope) and
    errors are exactly those of ``build_context``; nothing is committed here.
    """
    from modules.chat import retrieval

    return await retrieval.build_context(
        session, session_factory, redis, settings, request, scope=scope
    )
