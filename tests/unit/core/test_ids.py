import threading
from itertools import pairwise
from unittest import mock

from core import ids
from core.ids import uuid7


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"  # RFC 9562 shares the 10xx variant


def test_strictly_monotonic_over_many_calls() -> None:
    values = [uuid7() for _ in range(100_000)]
    assert all(a < b for a, b in pairwise(values))


def test_ordering_across_millisecond_boundaries() -> None:
    clock = iter([1_000_000_000, 1_000_000_000, 1_001_000_000, 1_002_000_000])
    with mock.patch.object(ids.time, "time_ns", lambda: next(clock)):
        ids._last_ms = 0
        first, second, third, fourth = (uuid7() for _ in range(4))
    assert first < second < third < fourth
    assert third.int >> 80 == 1001 and fourth.int >> 80 == 1002


def test_backwards_clock_is_clamped() -> None:
    with mock.patch.object(ids.time, "time_ns", lambda: 5_000_000_000):
        high = uuid7()
    with mock.patch.object(ids.time, "time_ns", lambda: 1_000_000_000):
        later = uuid7()
    assert later > high
    assert later.int >> 80 == high.int >> 80


def test_counter_overflow_advances_millisecond() -> None:
    with mock.patch.object(ids.time, "time_ns", lambda: 9_000_000_000):
        ids._last_ms, ids._counter = 9000, ids._COUNTER_MAX
        value = uuid7()
    assert value.int >> 80 == 9001


def test_unique_across_threads() -> None:
    seen: list[list[object]] = [[] for _ in range(8)]

    def work(bucket: list[object]) -> None:
        bucket.extend(uuid7() for _ in range(5_000))

    threads = [threading.Thread(target=work, args=(bucket,)) for bucket in seen]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    flat = [value for bucket in seen for value in bucket]
    assert len(set(flat)) == len(flat) == 40_000
