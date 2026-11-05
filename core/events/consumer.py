"""Consumer-side contracts for outbox events: ordering and idempotency.

Delivery contract (unchanged by the move to ``core.events``):

* **At-least-once.** A row is committed ``pending`` with the producer's transaction, claimed ``queued`` (with
  a ``dispatched_at`` fence) by the dispatcher and enqueued to ARQ under the job id
  ``ingestion:<event id>:<claimed_at>``. A lost enqueue reply, a crashed worker or a stale ``queued`` claim
  (older than ``DISPATCH_STALE_AFTER``) is re-claimed, so a consumer can see the same event more than once.
* **Per-aggregate ordering is not guaranteed.** The dispatcher scans in ``(created_at, id)`` order, but each
  event runs as its own concurrent ARQ job, retries re-enter later and quarantined events may be replayed
  hours afterwards. The ordering unit is the aggregate (``aggregate_key``: workspace plus Source, falling back
  to the workspace). Consumers must therefore be commutative or fenced: compare ``Source.generation`` /
  document version numbers / ``occurred_at`` against durable state and ignore stale effects, never assume a
  predecessor event was handled.
* **Idempotency.** There is no dedicated processed-events table (adding one needs a migration). The existing
  ``event_outbox.status`` is the processed marker: workers acknowledge with ``mark_event_delivered`` inside
  the transaction that applies the effect, so ``delivered`` means "effect committed". ``already_delivered``
  below is the guard a consumer calls before applying work; effects must still be individually idempotent
  because the acknowledgement and the effect are only atomic when the consumer commits them together.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.events.model import EventOutbox


def aggregate_key(event: EventOutbox | Any) -> tuple[str, str]:
    """Return the ordering/idempotency aggregate of an event as ``(workspace_id, source_id or "")``.

    Pure function over the stored envelope (no I/O). Events with equal keys are the only ones whose relative
    order a consumer might care about; even for those the delivery order is not guaranteed (module docstring).
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(event.workspace_id), str(payload.get("source_id") or "")


async def already_delivered(session: AsyncSession, event_id: UUID) -> bool:
    """Report whether the event's effect was already acknowledged (``status == "delivered"``).

    Read-only, takes no lock and never commits; a missing row counts as not delivered so the caller's own
    scope/envelope checks decide. Call it at the start of a consumer so a re-delivered event becomes a no-op.
    """
    status = await session.scalar(select(EventOutbox.status).where(EventOutbox.id == event_id))
    return status == "delivered"
