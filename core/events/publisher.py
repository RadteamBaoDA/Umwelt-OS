"""Transactional outbox publication: the single entry point that writes ``event_outbox`` rows."""

from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import NotFoundError
from core.events.domain import DomainEvent
from core.events.model import EventOutbox
from core.events.registry import PublishContext, get_event_registry
from core.workspaces.schemas import Scope


async def publish_event(
    session: AsyncSession, event: DomainEvent, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Add scoped durable identity to the caller's already-admitted transaction outbox.

    Caller holds admission and all required domain locks; only nonlocking owner reads occur here
    (admission, then the event type's handler proofs). Existing claimed scope fields must match, never be
    silently overwritten. No commit and no I/O beyond those reads; the row is only ``session.add``-ed, so
    it is delivered at-least-once after the caller's commit.

    Ordering contract: ``id``/``occurred_at`` come from the producer; the dispatcher claims by
    ``(created_at, id)`` and gives no cross-aggregate ordering (see ``core.events.consumer``).

    Raises:
        ValueError: If the type is not declared by any module descriptor (``event_consumers``) or a handler
            rejects the payload shape.
        NotFoundError: If a payload identity or a handler's retained-receipt proof does not match.
    """
    registry = get_event_registry()
    if event.type not in registry.routes:
        raise ValueError(f"Outbox event type {event.type!r} is not declared by any module descriptor")
    runtime = registry.runtime()
    await runtime.admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    payload = deepcopy(event.payload)
    identity = {"workspace_id": str(scope.workspace_id), "actor_user_id": runtime.actor_id(scope),
                "membership_revision": scope.membership_revision}
    for key, value in identity.items():
        if key in payload and (type(payload[key]) is not type(value) or payload[key] != value):
            raise NotFoundError(detail="Event identity not found")
    ctx = PublishContext(event=event, payload=payload, identity=identity, scope=scope,
                         multi_workspace_enabled=multi_workspace_enabled)
    handler = registry.handler(event.type)
    await handler.verify_publish(session, ctx)
    outbox = EventOutbox(
        workspace_id=scope.workspace_id, actor_user_id=runtime.actor_id(scope),
        membership_revision=scope.membership_revision,
        id=event.id,
        type=event.type,
        version=event.version,
        occurred_at=event.occurred_at,
        producer=event.producer,
        payload=ctx.payload,
        status="pending",
    )
    await handler.verify_outbox(session, ctx, outbox)
    session.add(outbox)
