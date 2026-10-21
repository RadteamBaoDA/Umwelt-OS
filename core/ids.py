"""Time-ordered identifier generation (UUID version 7, RFC 9562).

Every stored row id, lease/fencing token and request id is a UUIDv7 so primary-key indexes stay
append-mostly and ids sort by creation time. The 48-bit millisecond timestamp is visible inside each id;
that disclosure is accepted for this single-user, self-hosted system. Deterministic ``uuid5`` ids
(seeds, idempotency keys) are intentionally not routed through this module.
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0
_COUNTER_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7 that is strictly increasing within this process.

    Layout (RFC 9562 section 5.7, counter method of section 6.2): 48-bit unix milliseconds, version 7,
    a 12-bit ``rand_a`` counter, variant ``10`` and a 62-bit random tail from ``os.urandom``. Calls inside
    one millisecond increment the counter; on counter overflow the millisecond is advanced by one so
    ordering is never lost. A clock that moves backwards is clamped to the last issued timestamp. A
    process-wide lock makes the call thread-safe; ordering across processes is only millisecond-accurate.
    """
    global _last_ms, _counter
    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms, _counter = now_ms, int.from_bytes(os.urandom(2), "big") & 0x7FF  # headroom before overflow
        else:
            # Same millisecond or a backwards clock: keep the clamped timestamp and bump the counter.
            _counter += 1
            if _counter > _COUNTER_MAX:
                _last_ms, _counter = _last_ms + 1, 0
        ms, counter = _last_ms, _counter
    tail = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | tail
    return uuid.UUID(int=value)
