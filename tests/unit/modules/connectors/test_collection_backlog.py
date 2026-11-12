"""Collection-stage review backlog: redirects, retry cap, quota split, interval rounding, heartbeat, recheck."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from modules.connectors import quota, scheduler
from modules.connectors._collection import base
from modules.connectors.providers import feed_catalog, rest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _resolve(host: str, port: int) -> list[str]:
    """Resolve ``internal.test`` to a private address and everything else to a public one."""
    return ["10.0.0.5"] if host == "internal.test" else ["93.184.216.34"]


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Serve responses keyed by the original Host header."""
    return httpx.MockTransport(lambda req: routes[req.headers["host"]])


async def _fetch(routes: dict[str, httpx.Response], url: str = "https://a.test/x", **kw):
    """Run fetch_bounded against canned routes with a no-op before_send."""
    sends = AsyncMock()
    out = await rest.fetch_bounded(
        url, before_send=sends, resolve=_resolve, transport=_transport(routes), **kw
    )
    return out, sends


@pytest.mark.asyncio
async def test_redirect_followed_with_single_before_send() -> None:
    routes = {
        "a.test": httpx.Response(302, headers={"location": "https://b.test/y"}),
        "b.test": httpx.Response(200, content=b"ok"),
    }
    out, sends = await _fetch(routes)
    assert out.body == b"ok"
    assert sends.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://internal.test/y", "http://b.test/y"])
async def test_redirect_to_private_or_downgrade_blocked(target: str) -> None:
    routes = {"a.test": httpx.Response(302, headers={"location": target})}
    with pytest.raises(rest.UnsafeDestination):
        await _fetch(routes)


@pytest.mark.asyncio
async def test_redirect_loop_capped_at_three_hops() -> None:
    routes = {"a.test": httpx.Response(302, headers={"location": "https://a.test/x"})}
    with pytest.raises(rest.UnsafeDestination, match="redirect_blocked"):
        await _fetch(routes)


def test_retry_after_capped_at_24h() -> None:
    deadline = feed_catalog._retry_deadline(httpx.Headers({"retry-after": "9999999"}), NOW)
    assert deadline == NOW + timedelta(hours=24)


def test_live_quota_subject_is_separate_from_collector() -> None:
    policy = quota.get_quota_policies("nasa_firms")[0]
    live = quota.subject_hash(policy, "nasa_firms", credential=None, egress=None, live=True)
    coll = quota.subject_hash(policy, "nasa_firms", credential=None, egress=None)
    assert live != coll


@pytest.mark.parametrize(
    ("minutes", "expected"), [(1, 15), (15, 15), (16, 30), (61, 360), (400, 1440), (9999, 1440)]
)
def test_round_up_interval(minutes: int, expected: int) -> None:
    assert scheduler.round_up_interval(minutes) == expected


def test_every_provider_floor_is_an_allowed_interval() -> None:
    from modules.connectors.provider_specs import PROVIDER_MIN_INTERVAL_MINUTES

    assert set(PROVIDER_MIN_INTERVAL_MINUTES.values()) <= set(scheduler.ALLOWED_INTERVAL_MINUTES)


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_after_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base.scheduler, "renew_admission", AsyncMock(return_value=False))

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    attempt = SimpleNamespace(
        ref=SimpleNamespace(request_id=uuid4(), admission_token=uuid4()), multi=False
    )
    main = SimpleNamespace(cancel=lambda: pytest.fail("cancelled after accept"))
    lost = asyncio.Event()
    await base._heartbeat(
        lambda: Ctx(),
        attempt,
        main,
        lost,
        interval=0,
        accepted=lambda: True,  # type: ignore[arg-type]
    )
    assert not lost.is_set()


@pytest.mark.asyncio
async def test_header_recheck_skipped_when_recently_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = base.SendGate(None, None, settings=None, deadline=1e18, quota_provider=None)  # type: ignore[arg-type]
    gate._authorize = AsyncMock(
        side_effect=lambda: setattr(gate, "_last_authorized", base.time.monotonic())
    )  # type: ignore[method-assign]
    gate._debit = AsyncMock()  # type: ignore[method-assign]
    await gate()  # pre-send: authorize + debit
    await gate()  # header-time: skipped (fresh)
    assert gate._authorize.await_count == 1
    await gate()  # next send: authorize + debit, arms again
    gate._last_authorized = base.time.monotonic() - 10  # slow send: stamp is stale
    await gate()  # header-time recheck runs
    assert gate._authorize.await_count == 3
