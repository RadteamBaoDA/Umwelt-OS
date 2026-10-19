"""Effective transcript projection fed to the model as prior conversation history (Q2).

Storage stays append-only (edits/regenerations add rows); this projection decides what the model sees:
completed turns only, newest revision of each branch, the latest ``HISTORY_MAX_TURNS`` logical turns in
chronological order, bounded by ``HISTORY_MAX_CHARS``, minus any turn whose assistant text cites evidence
that is no longer present, active or remote-eligible.
"""

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.chat.models import Conversation, Message, ResponseRun
from modules.chat.schemas import Citation, parse_generative_ui, parse_generative_ui_parts
from modules.chat.scope import multi_workspace_enabled

HISTORY_MAX_TURNS = 20
# A quarter of the 64_000-byte retrieval evidence budget (MAX_CONTEXT_BUDGET_BYTES): history never outweighs evidence.
HISTORY_MAX_CHARS = 16_000
# Rows scanned (newest first). Superseders are always newer than their targets, so a window this size
# only loses turns older than it. ponytail: fixed scan window, keyset-page if conversations need deeper history.
HISTORY_SCAN_MESSAGES = 400
_EVIDENCE_BATCH = 100
# Cap of the widget summary appended to one assistant turn, and of any single markup string inside it.
WIDGET_SUMMARY_MAX_CHARS = 4096
_WIDGET_TEXT_MAX_CHARS = 600
_TRUNCATED = "[truncated]"


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
        turn_run = run_for_user.get(user.id)
        # failed / cancelled / pending / answer-less runs never become model history
        if turn_run is None or turn_run.status != "completed" or turn_run.assistant_message_id is None:
            continue
        assistant = by_id.get(turn_run.assistant_message_id)
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


def widget_summary(raw: Any, *, max_chars: int = WIDGET_SUMMARY_MAX_CHARS) -> str:
    """Bounded text describing a stored assistant widget so later turns can refer to it ("") when none).

    Only completed, schema-valid widgets count. The summary is the kind, title and compact JSON of the
    widget payload without evidence ids (``citations``) or the ``records`` duplicate of table rows;
    long markup strings and row/data/step lists are cut with an explicit ``[truncated]`` marker so the
    whole summary never exceeds ``max_chars`` (default ``WIDGET_SUMMARY_MAX_CHARS``).
    """
    widget = parse_generative_ui(raw)
    if widget is None or widget.status != "completed":
        return ""
    slot = next((v for v in (widget.table, widget.chart, widget.plan, widget.form, widget.sandbox) if v), None)
    if slot is None:
        return ""
    body = {k: v for k, v in slot.items() if k not in ("records", "citations")}
    for key, value in body.items():
        if isinstance(value, str) and len(value) > _WIDGET_TEXT_MAX_CHARS:
            body[key] = value[:_WIDGET_TEXT_MAX_CHARS] + _TRUNCATED
    head = f"\n\n[Widget shown to the user: {widget.kind}, title {json.dumps(widget.title, ensure_ascii=False)}]\n"
    budget = max_chars - len(head)

    def dump() -> str:
        """Compact JSON of the current (possibly shortened) body."""
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)

    text = dump()
    # Halve the longest list until it fits (keeps the leading rows, which carry the headline values).
    while len(text) > budget:
        lists = [k for k, v in body.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        longest = max(lists, key=lambda k: len(body[k]))
        body[longest] = body[longest][: len(body[longest]) // 2]
        body["note"] = _TRUNCATED
        text = dump()
    if len(text) > budget:
        text = text[: budget - len(_TRUNCATED)] + _TRUNCATED
    return head + text


def widget_summaries(metadata: Any) -> str:
    """Concatenated bounded summaries of every completed widget part of an assistant message ("" when none).

    The total stays within ``WIDGET_SUMMARY_MAX_CHARS``: the budget is split evenly across the parts, so a
    turn with several widgets costs the same history space as one.
    """
    parts = parse_generative_ui_parts(metadata)
    if not parts:
        return ""
    share = WIDGET_SUMMARY_MAX_CHARS // len(parts)
    return "".join(widget_summary(part.model_dump(mode="json"), max_chars=share) for part in parts)


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
            cur = current.get((version_id, chunk_id))
            if cur is None or cur.local_only or cur.source_id != source_id or cur.document_id != document_id:
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
        # Revoked turns were already removed above, and cleanup drops a wiped widget from metadata.
        widget = widget_summaries(getattr(turn.assistant, "metadata_json", None))
        out.append({"role": "assistant", "content": turn.assistant.content + widget})
    return out
