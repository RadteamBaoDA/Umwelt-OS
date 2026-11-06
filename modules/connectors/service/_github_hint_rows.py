"""Row-level GitHub webhook hint helpers split from ``github_hints`` to keep that module bounded.

Each helper operates on already admitted rows inside the caller's transaction and imports nothing
from ``github_hints``, so the pause-state tuple is received from ``github_binding``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from modules.connectors.models import GithubSourceHint, GithubWebhookCapacity
from modules.connectors.service.github_binding import _GITHUB_PAUSE_STATES


async def lock_pause_hints(
    session: AsyncSession, binding: Any, target: Any
) -> list[GithubSourceHint]:
    """Lock, in id order, every pause-relevant hint of the binding's Source plus the target's own hint.

    Takes ``FOR UPDATE`` with ``populate_existing`` so the rows reflect the latest committed state.
    """
    pause_hints = list(
        await session.scalars(
            select(GithubSourceHint)
            .where(
                GithubSourceHint.source_id == binding.source_id,
                or_(
                    GithubSourceHint.state.in_(_GITHUB_PAUSE_STATES),
                    (
                        (GithubSourceHint.resource == target.resource)
                        & (GithubSourceHint.locator_kind == target.locator_kind)
                        & (GithubSourceHint.locator == target.locator)
                    ),
                ),
            )
            .order_by(GithubSourceHint.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return pause_hints


def rewrite_visibility_hint(
    hint: GithubSourceHint,
    source_generation: int,
    binding: Any,
    delivery_receipt_id: Any,
    capacity: GithubWebhookCapacity,
) -> None:
    """Re-point an existing hint at the paused Source as ``visibility_unverified`` and clear its claim.

    Bumps ``dirty_revision`` and releases the hint's reserved capacity slot exactly once.

    Raises:
        ConflictError: when the hint holds a slot but the capacity counter is already zero.
    """
    hint.dirty_revision += 1
    hint.source_generation = source_generation
    hint.connector_revision = binding.connector_revision
    hint.repository_id = binding.repository_id
    hint.binding_revision = binding.binding_revision
    hint.intent = "visibility_lost"
    hint.last_delivery_id = delivery_receipt_id
    hint.state = "visibility_unverified"
    hint.claim_token = None
    hint.claimed_revision = None
    hint.claim_expires_at = None
    if hint.capacity_reserved:
        if capacity.pending_count < 1:
            raise ConflictError("GitHub visibility capacity changed")
        capacity.pending_count -= 1
        hint.capacity_reserved = False


async def reserve_deferred_capacity(
    session: AsyncSession, hint: GithubSourceHint, fence: Any, now: datetime
) -> bool:
    """Try to give a ``capacity_deferred`` hint a webhook capacity slot so it can be claimed.

    A hint whose repository, binding revision or resource left the fence is downgraded to
    ``visibility_unverified``; when capacity is missing or full the hint is retried in 60 seconds.
    Both outcomes commit and return ``False``. On success the slot is reserved, the hint becomes
    ``pending`` (uncommitted) and ``True`` is returned.
    """
    if (
        hint.repository_id != fence.repository_id
        or hint.binding_revision != fence.binding_revision
        or hint.resource not in fence.resource_scope
    ):
        hint.state = "visibility_unverified"
        hint.next_attempt_at = now
        await session.commit()
        return False
    capacity = await session.scalar(
        select(GithubWebhookCapacity)
        .where(
            GithubWebhookCapacity.id == 1,
        )
        .with_for_update()
    )
    if capacity is None or capacity.pending_count >= 100_000:
        hint.next_attempt_at = now + timedelta(seconds=60)
        await session.commit()
        return False
    capacity.pending_count += 1
    hint.capacity_reserved = True
    hint.state = "pending"
    return True
