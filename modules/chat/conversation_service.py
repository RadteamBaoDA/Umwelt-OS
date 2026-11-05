"""Chat-owned read/query helpers used by the conversation routes.

These functions hold the persistence queries that used to live inline in ``routes.py`` so the
route handlers only parse input, authorize and translate results. Every function takes the
caller's ``AsyncSession`` and never opens another session, commits, or rolls back; locking and
transaction boundaries stay with the route that owns the unit of work. They raise no HTTP
errors: "not found" style outcomes are returned as ``None`` for the route to translate.
"""

from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat.models import Message, MessageMutationReceipt, ResponseRun
from modules.chat.schemas import WEB_SEARCH_OUTCOME_KEY, WebSearchOutcomeRead

# Runs that count as active: they block a second turn (409), keep the SSE stream open and need the privacy fence.
# `awaiting_input` is a run parked on `request_user_input` (modules/chat/user_input.py).
ACTIVE_RUN_STATUSES = ("pending", "streaming", "awaiting_input")


async def _load_ordered_messages(session: AsyncSession, conversation_id: UUID) -> list[Message]:
    """Read every message of the conversation in creation order (one SELECT)."""
    return list(
        (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc())
            )
        ).all()
    )


async def load_transcript(
    session: AsyncSession,
    conversation_id: UUID,
) -> tuple[list[Message], UUID | None]:
    """Load the ordered transcript and the id of a still-active response, if any.

    Ordering matters: messages are read first, then the active-run probe. If completion raced
    with the first read, the active-run query sees no run and the terminal-run check reloads the
    transcript (so the completed answer is included). If completion happens after the active-run
    query, returning its id is still safe: the replayed SSE delivers completion and invalidates
    the caller's detail query. Read-only.

    Returns:
        ``(messages, active_response_id)``; ``active_response_id`` is ``None`` when no run is active.
    """
    messages_rows = await _load_ordered_messages(session, conversation_id)
    active_response_id = await session.scalar(
        select(ResponseRun.id)
        .where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.status.in_(ACTIVE_RUN_STATUSES),
            ResponseRun.assistant_message_id.is_(None),
        )
        .order_by(desc(ResponseRun.created_at), desc(ResponseRun.id))
        .limit(1)
    )
    if active_response_id is None:
        latest_run = await session.scalar(
            select(ResponseRun)
            .where(ResponseRun.conversation_id == conversation_id)
            .order_by(desc(ResponseRun.created_at), desc(ResponseRun.id))
            .limit(1)
        )
        if (
            latest_run is not None
            and latest_run.status == "completed"
            and latest_run.assistant_message_id is not None
            and all(message.id != latest_run.assistant_message_id for message in messages_rows)
        ):
            messages_rows = await _load_ordered_messages(session, conversation_id)
    return messages_rows, active_response_id


async def load_run_links(
    session: AsyncSession,
    conversation_id: UUID,
    messages_rows: list[Message],
) -> tuple[dict[UUID, UUID], dict[UUID, WebSearchOutcomeRead]]:
    """Map user messages to their response ids and answers to their web-search outcomes.

    Issues no SQL for an empty transcript. Only the outcome sub-key of the retrieval context
    is read (never the whole context), and only for assistant answers. A malformed stored
    outcome is skipped rather than guessed. Read-only.

    Returns:
        ``(response_ids_by_user_message, web_search_by_assistant)``.
    """
    response_ids_by_user_message: dict[UUID, UUID] = {}
    web_search_by_assistant: dict[UUID, WebSearchOutcomeRead] = {}
    if not messages_rows:
        return response_ids_by_user_message, web_search_by_assistant
    response_rows = await session.execute(
        select(ResponseRun.user_message_id, ResponseRun.id).where(
            # conversation_id lets the planner use ix_chat_response_runs_conversation_id.
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.user_message_id.in_([message.id for message in messages_rows]),
        )
    )
    response_ids_by_user_message = {
        user_message_id: response_id for user_message_id, response_id in response_rows.all()
    }
    outcome_rows = await session.execute(
        select(
            ResponseRun.assistant_message_id,
            ResponseRun.retrieval_context[WEB_SEARCH_OUTCOME_KEY].as_json(),
        ).where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.assistant_message_id.in_(
                [m.id for m in messages_rows if m.role == "assistant"]
            ),
        )
    )
    for assistant_id, raw_outcome in outcome_rows.all():
        if assistant_id is not None and isinstance(raw_outcome, dict):
            try:
                web_search_by_assistant[UUID(str(assistant_id))] = (
                    WebSearchOutcomeRead.model_validate(raw_outcome)
                )
            except ValueError:
                pass  # malformed outcome: report nothing rather than guess
    return response_ids_by_user_message, web_search_by_assistant


async def find_run_by_client_request(
    session: AsyncSession,
    conversation_id: UUID,
    client_request_id: str | None,
) -> ResponseRun | None:
    """Return the conversation's run created for ``client_request_id`` (idempotent replay lookup)."""
    return await session.scalar(
        select(ResponseRun).where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.client_request_id == client_request_id,
        )
    )


async def find_mutation_receipt(
    session: AsyncSession,
    conversation_id: UUID,
    client_request_id: str,
) -> MessageMutationReceipt | None:
    """Return the mutation receipt stored under ``client_request_id`` for the conversation."""
    return await session.scalar(
        select(MessageMutationReceipt).where(
            MessageMutationReceipt.conversation_id == conversation_id,
            MessageMutationReceipt.client_request_id == client_request_id,
        )
    )


async def find_run_by_id(session: AsyncSession, run_id: UUID) -> ResponseRun | None:
    """Return the response run with ``run_id`` (no workspace filter; callers hold the parent lock)."""
    return await session.scalar(select(ResponseRun).where(ResponseRun.id == run_id))


async def request_id_in_use_by_run(
    session: AsyncSession,
    conversation_id: UUID,
    client_request_id: str,
) -> bool:
    """Whether a normal send already used ``client_request_id`` (mutation and send keys are disjoint)."""
    collision = await session.scalar(
        select(ResponseRun.id)
        .where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.client_request_id == client_request_id,
        )
        .limit(1)
    )
    return collision is not None


async def lock_mutation_target(
    session: AsyncSession,
    conversation_id: UUID,
    message_id: UUID,
) -> Message | None:
    """Row-lock (``FOR UPDATE``, refreshing identity-map state) the message a mutation targets."""
    return await session.scalar(
        select(Message)
        .where(Message.id == message_id, Message.conversation_id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def find_original_run(
    session: AsyncSession,
    conversation_id: UUID,
    target: Message,
    action: str,
) -> ResponseRun | None:
    """Return the newest run an edit/regenerate derives from.

    ``edit`` matches the run that consumed the target prompt; ``regenerate`` matches the run
    that produced the target answer. Newest first by creation time.
    """
    statement = select(ResponseRun).where(
        ResponseRun.conversation_id == conversation_id,
        (ResponseRun.user_message_id == target.id)
        if action == "edit"
        else (ResponseRun.id == target.response_id),
    )
    if action == "regenerate":
        statement = statement.where(ResponseRun.assistant_message_id == target.id)
    return await session.scalar(statement.order_by(desc(ResponseRun.created_at)).limit(1))


async def find_original_prompt(
    session: AsyncSession,
    conversation_id: UUID,
    original_run: ResponseRun,
) -> Message | None:
    """Return the user prompt message the original run answered, scoped to the conversation."""
    return await session.scalar(
        select(Message).where(
            Message.id == original_run.user_message_id,
            Message.conversation_id == conversation_id,
            Message.role == "user",
        )
    )
