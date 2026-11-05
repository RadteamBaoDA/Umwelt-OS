"""Heavy-work capacity slots and remote-heavy guard (compatibility facade).

Canonical code lives in the submodules; these re-exports keep ``from core.heavy_work import X`` working.
"""

from core.heavy_work.models import (
    RemoteHeavyGuard,
)
from core.heavy_work.remote import (
    RemoteCleanupProof,
    RemoteHeavyGuardRead,
    cleanup_proof_after_ack,
    clear_remote_heavy_in_uow,
    get_remote_heavy_guard,
    has_blocking_remote_heavy_work,
    mark_remote_heavy_uncertain_in_uow,
    register_remote_heavy_in_uow,
)
from core.heavy_work.slots import (
    HEAVY_JOB_MAX_TRIES,
    HEAVY_LOCK_KEY,
    HEAVY_RETRY_DEFER_SECONDS,
    MAX_OPERATION_SECONDS,
    HeavyLease,
    HeavyLeaseLost,
    HeavyWorkBusy,
    P,
    R,
    RemoteHeavyWorkBlocked,
    bounded_heavy_work,
    heavy_job_slot,
    to_thread_joined,
)

__all__ = [
    "HEAVY_JOB_MAX_TRIES",
    "HEAVY_LOCK_KEY",
    "HEAVY_RETRY_DEFER_SECONDS",
    "MAX_OPERATION_SECONDS",
    "HeavyLease",
    "HeavyLeaseLost",
    "HeavyWorkBusy",
    "P",
    "R",
    "RemoteCleanupProof",
    "RemoteHeavyGuard",
    "RemoteHeavyGuardRead",
    "RemoteHeavyWorkBlocked",
    "bounded_heavy_work",
    "cleanup_proof_after_ack",
    "clear_remote_heavy_in_uow",
    "get_remote_heavy_guard",
    "has_blocking_remote_heavy_work",
    "heavy_job_slot",
    "mark_remote_heavy_uncertain_in_uow",
    "register_remote_heavy_in_uow",
    "to_thread_joined",
]
