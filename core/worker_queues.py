"""Arq queue routing for jobs that run on the dedicated model worker."""

MODEL_QUEUE = "arq:model"
# Jobs dominated by remote model I/O (already bounded by ModelGateway slots); keeping them off the
# main worker stops them from starving heavy-slot jobs such as chunk embedding.
MODEL_JOBS = frozenset({
    "process_entity_extraction_work", "process_timeline_extraction_work", "translate_content",
})


def queue_for(job_name: str) -> str | None:
    """Return the arq queue for a job name: MODEL_QUEUE for model jobs, else None (arq's default queue).

    Pass the result as ``_queue_name`` on every ``enqueue_job`` of these jobs; ``None`` leaves
    arq's own default queue in place.
    """
    return MODEL_QUEUE if job_name in MODEL_JOBS else None
