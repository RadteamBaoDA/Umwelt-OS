"""Wake POST falls back to the legacy webhook path only when n8n reports the new path unregistered."""

from types import SimpleNamespace

import pytest

from modules.connectors.public import _post_wake

PATHS = ("umwelt-collect-x", "bbd-collect-x")


class _Client:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.urls: list[str] = []

    async def post(self, url: str, **_kwargs: object) -> SimpleNamespace:
        self.urls.append(url)
        return SimpleNamespace(status_code=self.statuses[len(self.urls) - 1])


@pytest.mark.asyncio
async def test_new_path_answered_no_fallback() -> None:
    client = _Client([200])
    response = await _post_wake(client, "http://n8n/", PATHS, {}, "t")
    assert response.status_code == 200
    assert client.urls == ["http://n8n/webhook/umwelt-collect-x"]


@pytest.mark.asyncio
async def test_404_falls_back_to_legacy_path() -> None:
    client = _Client([404, 200])
    response = await _post_wake(client, "http://n8n", PATHS, {}, "t")
    assert response.status_code == 200
    assert client.urls == ["http://n8n/webhook/umwelt-collect-x", "http://n8n/webhook/bbd-collect-x"]


@pytest.mark.asyncio
async def test_deferral_status_is_not_retried() -> None:
    client = _Client([429])
    response = await _post_wake(client, "http://n8n", PATHS, {}, "t")
    assert response.status_code == 429
    assert len(client.urls) == 1
