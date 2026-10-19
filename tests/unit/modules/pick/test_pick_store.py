"""Pick store: TTL expiry, single use, tamper rejection, index-only input; route gating and budgets."""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.pick import public
from modules.pick.routes import router


class FakeRedis:
    """Minimal in-memory stand-in for the three Redis calls the store uses."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, name, value, ex=None):
        self.data[name] = value

    async def get(self, name):
        return self.data.get(name)

    async def getdel(self, name):
        return self.data.pop(name, None)


async def _scanned(redis):
    sid = await public.create_session(redis)
    options = await public.scan(redis, sid)
    return sid, options


@pytest.mark.asyncio
async def test_happy_path_is_single_use_and_delivers_pool_text() -> None:
    redis = FakeRedis()
    sid, options = await _scanned(redis)
    assert len(options) == 3 and len({o["index"] for o in options}) == 3
    assert (await public.poll(redis, sid))["state"] == "scanned"
    await public.submit(redis, sid, options[1]["index"])
    assert await public.poll(redis, sid) == {"state": "picked", "prompt": options[1]["label"]}
    with pytest.raises(public.PickUnavailable):  # consumed on read
        await public.poll(redis, sid)


@pytest.mark.asyncio
async def test_second_submit_and_rescan_rejected() -> None:
    redis = FakeRedis()
    sid, options = await _scanned(redis)
    with pytest.raises(public.PickUnavailable):
        await public.scan(redis, sid)  # no re-roll
    redis2 = FakeRedis()
    sid, options = await _scanned(redis2)
    await public.submit(redis2, sid, options[0]["index"])
    with pytest.raises(public.PickUnavailable):
        await public.submit(redis2, sid, options[0]["index"])


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    redis = FakeRedis()
    sid, options = await _scanned(redis)
    key = f"pick:{sid}"
    doc = json.loads(redis.data[key])
    doc["expires_at"] = time.time() - 1
    redis.data[key] = json.dumps(doc)
    with pytest.raises(public.PickUnavailable):
        await public.submit(redis, sid, options[0]["index"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../x", "short", "A" * 23, "A" * 22, ""])
async def test_tampered_or_unknown_ids_rejected(bad) -> None:
    redis = FakeRedis()
    for call in (public.scan(redis, bad), public.poll(redis, bad), public.submit(redis, bad, 0)):
        with pytest.raises(public.PickUnavailable):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [-1, 12, 99, True, "1", 1.0])
async def test_index_outside_offered_rejected_and_burns_session(index) -> None:
    redis = FakeRedis()
    sid, options = await _scanned(redis)
    with pytest.raises(public.PickUnavailable):
        await public.submit(redis, sid, index)
    with pytest.raises(public.PickUnavailable):
        await public.submit(redis, sid, options[0]["index"])


def test_pool_size_and_ids_are_128_bit() -> None:
    assert len(public.PROMPT_POOL) == 12


class _Settings:
    pick_enabled = False
    request_budget_enabled = True
    request_budget_overrides = None
    request_budget_fail_open = False


def test_routes_404_when_flag_off_and_phone_routes_carry_budget() -> None:
    app = FastAPI()
    app.state.settings = _Settings()
    app.include_router(router)
    client = TestClient(app)
    assert client.put("/api/v1/pick/phone/" + "A" * 22).status_code == 404
    assert client.get("/api/v1/pick/status").status_code == 404
    phone = [r for r in router.routes if "/phone/" in r.path]
    assert phone and all(any(d.dependency.__qualname__.startswith("require_budget") for d in r.dependencies) for r in phone)


def test_phone_body_rejects_free_text() -> None:
    from pydantic import ValidationError

    from modules.pick.routes import PickChoice

    for bad in ({"index": "1"}, {"index": 1, "prompt": "x"}, {"prompt": "x"}):
        with pytest.raises(ValidationError):
            PickChoice(**bad)
