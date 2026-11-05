"""Redis pub/sub wake-up signal between the chat worker's committed events and the SSE handler.

The database stays the single source of truth: the signal carries no payload that a client could
observe, it only says "stream events of this response were committed, read them now". Publishing is
best-effort and happens strictly after the commit; the SSE handler keeps a slow poll as a fallback,
so a lost or never-sent message delays delivery by at most ``STREAM_SIGNAL_FALLBACK_INTERVAL`` and can
never change event order, dedup (``seq`` cursor) or resume semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session as SyncSession

from modules.chat.models import StreamEvent

logger = logging.getLogger(__name__)

RESPONSE_CHANNEL_PREFIX = "chat:response:"
# Slow safety-net poll used while a pub/sub subscription is active (the legacy 0.1-0.5 s backoff is used
# only when no subscription could be established).
STREAM_SIGNAL_FALLBACK_INTERVAL = 2.0
_PENDING_KEY = "chat_stream_signal_ids"
_RUN_REDIS: dict[UUID, Any] = {}
_TASKS: set[asyncio.Task[None]] = set()


def response_channel(response_id: UUID | str) -> str:
    """Return the pub/sub channel name carrying wake-ups for one response run."""
    return f"{RESPONSE_CHANNEL_PREFIX}{response_id}"


async def publish_response_signal(redis: Any, response_id: UUID | str) -> None:
    """Publish one wake-up for ``response_id``; failures are logged and swallowed (poll fallback covers them).

    Must only be called after the transaction that inserted the events has committed.
    """
    try:
        await redis.publish(response_channel(response_id), b"1")
    except Exception as exc:  # noqa: BLE001  # boundary: signalling is best-effort, polling still delivers
        logger.warning("Chat stream signal publish failed (%s)", type(exc).__name__)


@contextlib.contextmanager
def signal_run_commits(response_id: UUID, redis: Any) -> Any:
    """Publish a wake-up after every commit that inserted ``StreamEvent`` rows for this run.

    The worker wraps a whole run in this context. The registration is process-local; sessions of other
    runs and API-side commits are unaffected (the cancel route signals explicitly).
    """
    _RUN_REDIS[response_id] = redis
    try:
        yield
    finally:
        _RUN_REDIS.pop(response_id, None)


def _note_inserted(_mapper: object, _connection: object, target: StreamEvent) -> None:
    """Remember (per session) which responses received events in the current transaction."""
    from sqlalchemy.orm import object_session

    session = object_session(target)
    if session is not None and target.response_id in _RUN_REDIS:
        session.info.setdefault(_PENDING_KEY, set()).add(target.response_id)


def _signal_after_commit(session: SyncSession) -> None:
    """After a successful commit, schedule one publish per registered response that got events."""
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for response_id in pending:
        redis = _RUN_REDIS.get(response_id)
        if redis is None:
            continue
        task = loop.create_task(publish_response_signal(redis, response_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)


def _forget_after_rollback(session: SyncSession, *_args: object) -> None:
    """Drop remembered responses when the transaction that inserted the events rolled back."""
    session.info.pop(_PENDING_KEY, None)


event.listen(StreamEvent, "after_insert", _note_inserted)
event.listen(SyncSession, "after_commit", _signal_after_commit)
event.listen(SyncSession, "after_rollback", _forget_after_rollback)


class ResponseSignalSubscription:
    """One SSE connection's subscription to ``chat:response:<id>`` with a bounded wait.

    ``subscribe`` must complete before the first database poll so a commit landing between the poll
    and the wait is buffered on the connection instead of being missed. Every Redis error disables the
    subscription (``active`` becomes False) and the caller falls back to plain polling.
    """

    def __init__(self, redis: Any, response_id: UUID) -> None:
        self._redis = redis
        self._channel = response_channel(response_id)
        self._pubsub: Any = None

    @property
    def active(self) -> bool:
        """Whether a live subscription exists (False before ``subscribe`` and after any Redis error)."""
        return self._pubsub is not None

    async def subscribe(self) -> bool:
        """Open the subscription; return False (and stay inactive) when Redis is missing or fails."""
        if self._redis is None:
            return False
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(self._channel)
        except Exception as exc:  # noqa: BLE001  # boundary: no Redis -> legacy polling
            logger.warning("Chat stream subscribe failed (%s)", type(exc).__name__)
            return False
        self._pubsub = pubsub
        return True

    async def wait(self, timeout: float) -> bool:  # noqa: ASYNC109  # bounded wait primitive; callers pass the poll fallback interval
        """Wait up to ``timeout`` seconds for a wake-up; True when one arrived, False on timeout/failure.

        Drains every message already buffered so one poll covers a burst of flushes.
        """
        if self._pubsub is None:
            await asyncio.sleep(timeout)
            return False
        try:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if message is None:
                return False
            while await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=0) is not None:
                pass
            return True
        except Exception as exc:  # noqa: BLE001  # boundary: degrade to polling
            logger.warning("Chat stream wait failed (%s)", type(exc).__name__)
            await self.close()
            return False

    async def close(self) -> None:
        """Unsubscribe and release the pub/sub connection; safe to call repeatedly."""
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is None:
            return
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(self._channel)
        with contextlib.suppress(Exception):
            await pubsub.aclose()


@contextlib.asynccontextmanager
async def subscribed(redis: Any, response_id: UUID) -> AsyncIterator[ResponseSignalSubscription]:
    """Yield a subscription (possibly inactive) and always close it, including on cancellation."""
    subscription = ResponseSignalSubscription(redis, response_id)
    await subscription.subscribe()
    try:
        yield subscription
    finally:
        await asyncio.shield(subscription.close())
