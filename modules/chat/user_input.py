"""Human-in-the-loop user input requests: park a chat run, accept one validated answer, resume it.

Lifecycle of one ``request_user_input`` tool call (worker side calls 1 and 3, the API route calls 2):

1. ``create_input_request``: run ``streaming`` -> ``awaiting_input``; ``input_request`` JSON is stored and a
   ``message.input.request`` event is appended. The caller commits, releasing every lock.
2. ``submit_input``: validates owner, request id and answer, stores the answer, flips the run back to
   ``streaming`` (so the 409 "run active" rule never has a gap) and appends ``message.input.resolved``.
3. ``await_user_input``: the worker polls the DB (no Redis, no open transaction) until answered, expired or
   cancelled, then appends the tool message and continues the same run.

Concurrency/ordering invariants:
- While parked NO transaction, row lock or Memory privacy advisory lock is held: ``await_user_input`` takes a
  session *factory* and uses one short transaction per poll. Locks are re-acquired (privacy -> conversation ->
  run, the order the rest of Chat uses) only for writes: create, submit (conversation -> run), expiry.
- ``awaiting_input`` counts as an active run everywhere ``pending``/``streaming`` does, so a second user turn
  still gets 409. Only the run row lock serializes submit vs. expire vs. cancel; each re-checks status under it.
- Event ``seq`` is ``max(persisted)+1`` under the run lock (same rule as the worker's ``_next_event_seq``).
- A parked run is not ``streaming`` so ``recover_chat_runs`` ignores it; the worker heartbeats ``updated_at``
  and ``expire_stale_requests`` fails parked runs whose worker vanished and cancels expired requests.
- Deletion/provenance: the request lives on the run row and dies with it (ON DELETE CASCADE of its events).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.ids import uuid7
from modules.chat.generative_ui.tool_specs import INPUT_DECLINE, RequestUserInputArgs
from modules.chat.models import Conversation, ResponseRun, StreamEvent
from modules.chat.stream import make_event_id
from modules.memory.public import lock_export_privacy

logger = logging.getLogger(__name__)

EVENT_INPUT_REQUEST = "message.input.request"
EVENT_INPUT_RESOLVED = "message.input.resolved"
MAX_TEXT_ANSWER_CHARS = 2000
# arq's chat job_timeout is 600 s (worker.CHAT_JOB_TIMEOUT) and the job includes parked time, so a longer park
# would be killed mid-wait. Raise that constant together with this one to allow the 15 min default.
MAX_PARK_SECONDS = 540
POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 10.0
# A parked run whose worker stopped heartbeating this long ago is failed by `expire_stale_requests`.
PARKED_STALE_AFTER = timedelta(seconds=120)

Outcome = Literal["answered", "declined", "expired", "cancelled"]


class InputRequestError(Exception):
    """A user-input operation was refused; ``status_code`` and ``code`` map directly onto the HTTP response."""

    def __init__(self, status_code: int, code: str) -> None:
        """Record the HTTP status and machine code of the refusal."""
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class InputOutcome:
    """Result of waiting on a request: ``tool_result`` is the string for the model's tool message."""

    outcome: Outcome
    tool_result: str


def resolve_ttl(configured_seconds: int) -> timedelta:
    """Return the park duration: the configured TTL clamped to the worker job budget (``MAX_PARK_SECONDS``)."""
    return timedelta(seconds=max(1, min(configured_seconds, MAX_PARK_SECONDS)))


def public_request(request: dict[str, Any]) -> dict[str, Any]:
    """Project a stored request onto its SSE payload: never the answer or internal state."""
    keys = ("request_id", "kind", "prompt", "options", "slots", "expires_at")
    return {k: request[k] for k in keys if k in request}


def build_tool_result(request: dict[str, Any]) -> str:
    """Render the tool-message content for an answered or declined request (JSON string)."""
    answer = request.get("answer") or {}
    if answer.get("value") == INPUT_DECLINE:
        return json.dumps({"status": "declined", "kind": request["kind"]})
    return json.dumps(
        {"status": "answered", "kind": request["kind"], "answer": answer.get("value"), "label": answer.get("label")},
        ensure_ascii=False,
    )


def validate_answer(request: dict[str, Any], answer: str) -> dict[str, str]:
    """Validate ``answer`` against the request schema and return ``{"value", "label"}``.

    Raises:
        InputRequestError: 422 ``invalid_answer`` for an unknown/unavailable slot or option or an empty/oversized text.
    """
    kind = request["kind"]
    if kind == "text":
        text = answer.strip()
        if not text or len(text) > MAX_TEXT_ANSWER_CHARS:
            raise InputRequestError(422, "invalid_answer")
        return {"value": text, "label": text}
    if answer == INPUT_DECLINE:
        return {"value": INPUT_DECLINE, "label": ""}
    if kind == "time_slots":
        for slot in request.get("slots", []):
            if slot["id"] == answer and slot.get("available", True):
                return {"value": answer, "label": f"{slot['label']} ({slot['date']} {slot['start_time']}-{slot['end_time']})"}
    else:
        for option in request.get("options", []):
            if option["id"] == answer:
                return {"value": answer, "label": option["label"]}
    raise InputRequestError(422, "invalid_answer")


def _expired(request: dict[str, Any], now: datetime) -> bool:
    """Whether the stored ``expires_at`` has passed."""
    return datetime.fromisoformat(request["expires_at"]) <= now


async def _append_event(session: AsyncSession, run_id: UUID, event_type: str, data: dict[str, Any]) -> None:
    """Append one event with ``seq = max(persisted)+1``; the caller holds the run row lock."""
    latest = await session.scalar(select(func.coalesce(func.max(StreamEvent.seq), 0)).where(StreamEvent.response_id == run_id))
    seq = (latest or 0) + 1
    session.add(StreamEvent(
        response_id=run_id, seq=seq, event_type=event_type, event_id=make_event_id(run_id, seq), data=data,
    ))


async def _lock_run(session: AsyncSession, response_id: UUID) -> ResponseRun | None:
    """Lock conversation then run FOR UPDATE (the Chat lock order) and return the fresh run, or None if gone."""
    hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
    if hint is None:
        return None
    await session.scalar(select(Conversation).where(
        Conversation.id == hint.conversation_id,
    ).with_for_update().execution_options(populate_existing=True))
    return await session.scalar(select(ResponseRun).where(
        ResponseRun.id == response_id,
    ).with_for_update().execution_options(populate_existing=True))


async def create_input_request(
    session: AsyncSession, run: ResponseRun, args: RequestUserInputArgs, *,
    ttl: timedelta, now: datetime | None = None,
) -> dict[str, Any]:
    """Park ``run`` on a new request and publish ``message.input.request``.

    The caller must hold the run row lock with the run ``streaming`` (the worker's ``_lock_live_response``)
    and must COMMIT right after, which releases all locks before ``await_user_input`` starts.

    Args:
        session: Transaction holding the locks.
        run: The locked, streaming run.
        args: Validated tool arguments (bounded by the pydantic model).
        ttl: How long the request stays answerable (see ``resolve_ttl``).
        now: Clock override for tests.

    Returns:
        The public request payload (also the SSE event data); its ``request_id`` is what to await.

    Raises:
        InputRequestError: 409 ``run_not_streaming`` when the run is not streaming.
    """
    if run.status != "streaming":
        raise InputRequestError(409, "run_not_streaming")
    request = {
        "request_id": uuid7().hex,
        "kind": args.kind,
        "prompt": args.prompt,
        "options": [o.model_dump() for o in args.options],
        "slots": [s.model_dump() for s in args.slots],
        "expires_at": ((now or datetime.now(UTC)) + ttl).isoformat(),
    }
    run.input_request = {**request, "state": "pending", "answer": None}
    run.status = "awaiting_input"
    await _append_event(session, run.id, EVENT_INPUT_REQUEST, public_request(request))
    return public_request(request)


async def submit_input(
    session: AsyncSession, *, response_id: UUID, request_id: str, answer: str,
    workspace_id: UUID, actor_user_id: int, now: datetime | None = None,
) -> str:
    """Record the owner's answer, resume the run and return the tool-result string.

    Locks conversation -> run, then re-checks everything under the run lock so a double submit, a concurrent
    cancel and an expiry race resolve to exactly one winner. The caller commits.

    Raises:
        InputRequestError: 404 ``not_found`` (no run, other owner/workspace, or request id mismatch);
            409 ``not_awaiting`` (already answered/cancelled/finished); 409 ``expired`` (the run is cancelled
            here and the expiry is committed by the caller); 422 ``invalid_answer``.
    """
    run = await _lock_run(session, response_id)
    request = run.input_request if run is not None else None
    if run is None or run.workspace_id != workspace_id or run.actor_user_id != actor_user_id:
        raise InputRequestError(404, "not_found")
    if not request or request.get("request_id") != request_id:
        raise InputRequestError(404, "not_found")
    if run.status != "awaiting_input" or request.get("state") != "pending":
        raise InputRequestError(409, "not_awaiting")
    if _expired(request, now or datetime.now(UTC)):
        await expire_locked(session, run)
        raise InputRequestError(409, "expired")
    resolved = validate_answer(request, answer)
    run.input_request = {**request, "state": "answered", "answer": resolved}
    run.status = "streaming"  # never leaves the active set, so 409 "run active" has no gap
    await _append_event(session, run.id, EVENT_INPUT_RESOLVED, {
        "request_id": request_id, "outcome": "declined" if resolved["value"] == INPUT_DECLINE else "answered",
        "label": resolved["label"],
    })
    return build_tool_result(run.input_request)


async def expire_locked(session: AsyncSession, run: ResponseRun) -> bool:
    """Cancel a parked run whose request expired (cancel semantics); the caller holds the run lock and commits.

    Returns:
        True when the run was cancelled here, False when it was no longer ``awaiting_input``.
    """
    if run.status != "awaiting_input":
        return False
    request = run.input_request or {}
    run.input_request = {**request, "state": "expired"}
    run.status = "cancelled"
    run.completed_at = func.now()
    await _append_event(session, run.id, EVENT_INPUT_RESOLVED, {"request_id": request.get("request_id"), "outcome": "expired"})
    await _append_event(session, run.id, "status", {"status": "cancelled"})
    return True


async def expire_stale_requests(session: AsyncSession, *, now: datetime | None = None, limit: int = 50) -> int:
    """Cancel expired requests and fail parked runs whose worker stopped heartbeating; returns runs changed.

    Lock order privacy -> conversation -> run. Meant for the periodic recovery job; safe to run concurrently
    with submit/cancel because each run is re-checked under its row lock.
    """
    now = now or datetime.now(UTC)
    ids = list(await session.scalars(
        select(ResponseRun.id).where(ResponseRun.status == "awaiting_input").order_by(ResponseRun.updated_at).limit(limit)
    ))
    if not ids:
        return 0
    await lock_export_privacy(session)
    changed = 0
    for run_id in ids:
        run = await _lock_run(session, run_id)
        if run is None or run.status != "awaiting_input":
            continue
        request = run.input_request or {}
        if request.get("expires_at") and _expired(request, now):
            changed += int(await expire_locked(session, run))
        elif run.updated_at is not None and run.updated_at < now - PARKED_STALE_AFTER:
            run.input_request = {**request, "state": "expired"}
            run.status = "failed"
            run.error_code = "WorkerLost"
            run.error_message = "worker stopped while waiting for user input"
            run.completed_at = func.now()
            await _append_event(session, run.id, "status", {"status": "failed", "error": run.error_message})
            changed += 1
    await session.commit()
    return changed


async def await_user_input(
    session_factory: async_sessionmaker[AsyncSession], response_id: UUID, request_id: str, *,
    poll_interval: float = POLL_INTERVAL, heartbeat_interval: float = HEARTBEAT_INTERVAL,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> InputOutcome:
    """Wait (DB polling, bounded by the request's ``expires_at``) until the request is resolved.

    Holds nothing between polls: each poll is its own short read transaction. Every ``heartbeat_interval``
    seconds it touches ``updated_at`` so ``expire_stale_requests`` can tell a live worker from a dead one.
    DB polling matches the SSE route and needs no Redis key (the cancel route changes the status the poll reads).

    Returns:
        ``answered``/``declined`` with the tool result, or ``expired``/``cancelled`` (run already terminal:
        the worker must stop without publishing anything further).
    """
    elapsed = 0.0
    while True:
        async with session_factory() as session:
            run = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
            request = run.input_request if run is not None else None
            if run is None or not request or request.get("request_id") != request_id:
                return InputOutcome("cancelled", json.dumps({"status": "cancelled"}))
            state = request.get("state")
            if state == "answered":
                outcome: Outcome = "declined" if (request.get("answer") or {}).get("value") == INPUT_DECLINE else "answered"
                return InputOutcome(outcome, build_tool_result(request))
            if state == "expired":
                return InputOutcome("expired", json.dumps({"status": "expired"}))
            if run.status != "awaiting_input":  # cancelled/failed/completed while parked
                return InputOutcome("cancelled", json.dumps({"status": "cancelled"}))
            if _expired(request, now()):
                await lock_export_privacy(session)
                locked = await _lock_run(session, response_id)
                if locked is not None and await expire_locked(session, locked):
                    await session.commit()
                    return InputOutcome("expired", json.dumps({"status": "expired"}))
                await session.rollback()
                continue  # lost a race to submit/cancel: re-read the new state
            if elapsed >= heartbeat_interval:
                await session.execute(update(ResponseRun).where(
                    ResponseRun.id == response_id, ResponseRun.status == "awaiting_input",
                ).values(updated_at=func.now()))
                await session.commit()
                elapsed = 0.0
        await sleep(poll_interval)
        elapsed += poll_interval


async def recover_parked_input_runs(ctx: dict[str, object]) -> dict[str, int]:
    """ARQ cron entry: expire timed-out input requests and fail parked runs of dead workers.

    Separate from ``recover_chat_runs`` (which only handles pending/streaming) so the two sweeps stay independent.
    """
    factory = ctx["session_factory"]
    async with factory() as session:  # type: ignore[operator]
        return {"changed": await expire_stale_requests(session)}
