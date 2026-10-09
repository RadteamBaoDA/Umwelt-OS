"""Effective transcript projection fed to the model as prior conversation history (Q2).

Storage stays append-only (edits/regenerations add rows); this projection decides what the model sees:
completed turns only, newest revision of each branch, the latest ``HISTORY_MAX_TURNS`` logical turns in
chronological order, bounded by ``HISTORY_MAX_CHARS``, minus any turn whose assistant text cites evidence
that is no longer present, active or remote-eligible.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.chat.models import Conversation, Message, ResponseRun
from modules.chat.schemas import Citation
from modules.chat.scope import multi_workspace_enabled

HISTORY_MAX_TURNS = 20
# A quarter of the 64_000-byte retrieval evidence budget (MAX_CONTEXT_BUDGET_BYTES): history never outweighs evidence.
HISTORY_MAX_CHARS = 16_000
# Rows scanned (newest first). Superseders are always newer than their targets, so a window this size
# only loses turns older than it. ponytail: fixed scan window, keyset-page if conversations need deeper history.
HISTORY_SCAN_MESSAGES = 400
_EVIDENCE_BATCH = 100


class Turn(NamedTuple):
    user: Message
    assistant: Message


def select_turns(messages: Sequence[Message], runs: Iterable[ResponseRun], limit: int = HISTORY_MAX_TURNS) -> list[Turn]:
    """Complete, non-superseded turns, newest ``limit`` of them, oldest first."""
    by_id = {m.id: m for m in messages}
    run_for_user: dict[UUID, ResponseRun] = {}
    for run in runs:
        prev = run_for_user.get(run.user_message_id)
        if prev is None or (run.created_at, str(run.id)) > (prev.created_at, str(prev.id)):
            run_for_user[run.user_message_id] = run
    complete: list[Turn] = []
    for user in messages:
        if user.role != "user":
            continue
        run = run_for_user.get(user.id)
        # failed / cancelled / pending / answer-less runs never become model history
        if run is None or run.status != "completed" or run.assistant_message_id is None:
            continue
        assistant = by_id.get(run.assistant_message_id)
        if assistant is not None and assistant.role == "assistant":
            complete.append(Turn(user, assistant))
    superseded = {
        target for t in complete for target in (t.user.revision_of_message_id, t.assistant.revision_of_message_id)
        if target is not None
    }
    live = [t for t in complete if t.user.id not in superseded and t.assistant.id not in superseded]
    live.sort(key=lambda t: (t.user.created_at, str(t.user.id)))
    return live[-limit:] if limit > 0 else []


def cap_turns(turns: Sequence[Turn], max_chars: int = HISTORY_MAX_CHARS) -> list[Turn]:
    """Keep the newest whole turns that fit ``max_chars`` (user + assistant text)."""
    kept: list[Turn] = []
    total = 0
    for turn in reversed(turns):
        total += len(turn.user.content) + len(turn.assistant.content)
        if total > max_chars:
            break
        kept.append(turn)
    kept.reverse()
    return kept


def _citation_refs(citations: Any) -> list[tuple[UUID, UUID, UUID, UUID]] | None:
    """(source, document, version, chunk) per citation, or None when any entry is unreadable."""
    refs = []
    for raw in citations if isinstance(citations, list) else []:
        try:
            c = Citation.model_validate(raw)
        except Exception:  # noqa: BLE001  # boundary: unparseable copied evidence is treated as unsafe
            return None
        refs.append((c.sourceId, c.documentId, c.documentVersionId, c.chunkId))
    return refs


async def revoked_assistant_ids(session: AsyncSession, turns: Sequence[Turn], scope: Scope) -> set[UUID]:
    """Assistant ids whose cited evidence is gone, inactive, local-only or outside the job scope.

    Reuses the Documents evidence lock/read used for publication; key-share locks last until the caller
    commits, which the worker does before any retrieval or gateway I/O.
    """
    from modules.knowledge.documents import public as documents_public

    parsed = {t.assistant.id: _citation_refs(t.assistant.citations) for t in turns if t.assistant.citations}
    unique = sorted({(v, c) for refs in parsed.values() for _s, _d, v, c in refs or []}, key=str)
    if not unique:
        return {aid for aid, refs in parsed.items() if refs is None}
    current: dict[tuple[UUID, UUID], Any] = {}
    try:
        for start in range(0, len(unique), _EVIDENCE_BATCH):
            for item in await documents_public.lock_chat_evidence_chunks(
                session, unique[start:start + _EVIDENCE_BATCH],
                scope=scope, multi_workspace_enabled=multi_workspace_enabled(),
            ):
                current[(item.document_version_id, item.chunk_id)] = item
    except ValueError:
        return set(parsed)  # evidence set changed under us: drop every cited answer rather than guess
    revoked: set[UUID] = set()
    for aid, refs in parsed.items():
        if refs is None:
            revoked.add(aid)
            continue
        for source_id, document_id, version_id, chunk_id in refs:
            item = current.get((version_id, chunk_id))
            if item is None or item.local_only or item.source_id != source_id or item.document_id != document_id:
                revoked.add(aid)
                break
    return revoked


async def load_effective_history(
    session: AsyncSession, conversation_id: UUID, cutoff: datetime, scope: Scope,
) -> list[dict[str, str]]:
    """Model-facing prior messages for a run whose branch starts at ``cutoff`` (exclusive)."""
    messages = list((await session.scalars(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conversation_id,
            Conversation.workspace_id == scope.workspace_id,
            Message.created_at < cutoff,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_SCAN_MESSAGES)
    )).all())
    user_ids = [m.id for m in messages if m.role == "user"]
    runs = list((await session.scalars(
        select(ResponseRun).where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.workspace_id == scope.workspace_id,
            ResponseRun.user_message_id.in_(user_ids),
        )
    )).all()) if user_ids else []
    turns = select_turns(messages, runs)
    if turns:
        revoked = await revoked_assistant_ids(session, turns, scope)
        turns = [t for t in turns if t.assistant.id not in revoked]
    out: list[dict[str, str]] = []
    for turn in cap_turns(turns):
        out.append({"role": "user", "content": turn.user.content})
        out.append({"role": "assistant", "content": turn.assistant.content})
    return out
