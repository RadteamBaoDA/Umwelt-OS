"""Arq queue routing for jobs that run on the dedicated model worker."""

from datetime import UTC, datetime, timedelta

from arq.connections import ArqRedis
from arq.constants import default_queue_name

MODEL_QUEUE = "arq:model"
# Jobs dominated by remote model I/O (already bounded by ModelGateway slots); keeping them off the
# main worker stops them from starving heavy-slot jobs such as chunk embedding.
MODEL_JOBS = frozenset(
    {
        "process_entity_extraction_work",
        "process_timeline_extraction_work",
        "translate_content",
    }
)


def queue_for(job_name: str) -> str | None:
    """Return the arq queue for a job name: MODEL_QUEUE for model jobs, else None (arq's default queue).

    Pass the result as ``_queue_name`` on every ``enqueue_job`` of these jobs; ``None`` leaves
    arq's own default queue in place.
    """
    return MODEL_QUEUE if job_name in MODEL_JOBS else None


def ahead_of_backlog() -> datetime:
    """Return a past ``_defer_until`` for interactive jobs on a shared queue (one hour ago, tz-aware UTC).

    arq runs queued jobs in score (enqueue-time) order and ``_defer_until`` sets that score. An hour
    in the past puts the job ahead of queued bulk work, such as extraction requeued after an AI
    settings change, so it runs as soon as one model-worker slot frees. It does not preempt running
    jobs. Keep it well under arq's 1-day ``expires_extra_ms``: the job key TTL is
    ``score - now + 1 day``, so a larger jump would shorten the job's lifetime.
    """
    # ponytail: score-jump priority on the shared arq:model queue; add a dedicated translation worker
    # if clicks still wait on 4 busy extraction slots.
    return datetime.now(UTC) - timedelta(hours=1)


def pin_default_queue(redis: ArqRedis) -> None:
    """Make bare ``enqueue_job`` calls on a worker's own pool land on arq's default queue.

    arq builds each worker's pool with ``default_queue_name = <that worker's queue_name>``, so a bare
    enqueue (no ``_queue_name``) from the cron/model/chat workers, such as the outbox dispatcher's
    ``process_*`` jobs, would land on ``arq:cron``/``arq:model``/``arq:chat``, where no worker
    registers it, and stall. Pinning the attribute restores API-equivalent routing. Workers poll,
    retry and health-check via ``self.queue_name`` and retry with an explicit ``_queue_name``, so
    nothing else reads the pool default. Explicit ``_queue_name`` still wins. Idempotent.
    """
    redis.default_queue_name = default_queue_name
