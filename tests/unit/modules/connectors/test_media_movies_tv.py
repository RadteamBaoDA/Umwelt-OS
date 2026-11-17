"""TVMaze schedule mapping and upstream scope tests without network access."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.providers.media_movies_tv import (
    collect_tvmaze_schedule,
    map_tvmaze_schedule,
    tvmaze_schedule_url,
)
from modules.connectors.providers.rest import Fetched, ProviderHttpError


def test_tvmaze_url_preserves_global_web_scope_and_rejects_uk() -> None:
    url = tvmaze_schedule_url(country="", date="2026-11-18", web=True)
    assert urlsplit(url).path == "/schedule/web"
    assert parse_qs(urlsplit(url).query, keep_blank_values=True)["country"] == [""]
    assert "country=GB" in tvmaze_schedule_url(country="GB", date="2026-11-18")
    with pytest.raises(ValueError):
        tvmaze_schedule_url(country="UK", date="2026-11-18")


def test_tvmaze_maps_future_episode_and_applies_filters() -> None:
    row = {
        "id": 123,
        "name": "Pilot",
        "season": 1,
        "number": 2,
        "airstamp": "2026-11-18T20:00:00+00:00",
        "summary": "<p>Hello</p>",
        "show": {
            "id": 99,
            "name": "Series",
            "type": "Scripted",
            "language": "English",
            "genres": ["Drama"],
            "rating": {"average": 8.5},
            "network": {"country": {"code": "GB"}},
        },
    }
    now = datetime(2026, 11, 17, tzinfo=UTC)
    records = map_tvmaze_schedule(
        [row], collected_at=now, config={"media_genres": ["drama"], "min_rating": 80}
    )
    assert len(records) == 1
    record = records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "tvmaze:123" and record.observed_at == now
    assert fields["country"] == "GB" and fields["language"] == record.metadata["language"] == "en"
    assert fields["rating"] == 85 and fields["format"] == "scripted"
    assert fields["release_status"] == "upcoming" and "TVMaze" in fields["attribution"]
    assert fields["release_date"] == "2026-11-18T20:00:00Z"
    assert "<p>" not in record.content
    assert map_tvmaze_schedule([row], collected_at=now, config={"media_languages": ["ja"]}) == []
    assert len(map_tvmaze_schedule([row, row], collected_at=now, config={})) == 1
    with pytest.raises(ValueError):
        map_tvmaze_schedule([row] * 501, collected_at=now, config={})


@pytest.mark.asyncio
async def test_tvmaze_uses_injected_transport_and_conditional_304() -> None:
    fetch = AsyncMock(return_value=Fetched(body=None))
    params = dict(country="GB", date="2026-11-18", web=False, config={}, collected_at=datetime(2026, 11, 17, tzinfo=UTC))
    page = await collect_tvmaze_schedule(fetch, **params, etag='"v1"')
    assert page.not_modified and page.records == () and page.etag == '"v1"'
    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"v1"'
    assert fetch.await_args.kwargs["max_bytes"] == 2 * 1024 * 1024
    with pytest.raises(ProviderHttpError):
        await collect_tvmaze_schedule(fetch, **params)
    with pytest.raises(ProviderHttpError):
        await collect_tvmaze_schedule(fetch, **params, etag="unsafe\r\nheader")
    assert "If-None-Match" not in fetch.await_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_tvmaze_success_captures_only_safe_response_validators() -> None:
    body = json.dumps([{"id": 1, "airdate": "2026-11-18", "show": {"id": 2, "name": "Show"}}]).encode()
    fetch = AsyncMock(return_value=Fetched(body=body, etag='"v2"', last_modified="bad\r\nvalue"))
    page = await collect_tvmaze_schedule(
        fetch, country=None, date="2026-11-18", web=True, config={},
        collected_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    assert not page.not_modified and len(page.records) == 1
    assert page.etag == '"v2"' and page.last_modified is None


@pytest.mark.asyncio
async def test_runner_bounds_validators_to_url_and_clears_them_for_backlog(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.connectors._collection import providers

    url = tvmaze_schedule_url(country="GB", date=datetime.now(UTC).date().isoformat())
    fingerprint = sha256(url.encode()).hexdigest()
    state = SimpleNamespace(continuation_state=fingerprint, validators_revision=3, etag='"old"', last_modified=None)
    fetch = AsyncMock(return_value=Fetched(body=b"[]", etag='"new"'))
    run = SimpleNamespace(attempt=SimpleNamespace(source=SimpleNamespace(configuration={"media_country": "GB"}), connector_revision=3), gate=SimpleNamespace(fetch_bytes=fetch), extras={})
    accept = AsyncMock()
    monkeypatch.setattr(providers, "_state", AsyncMock(return_value=state))
    monkeypatch.setattr(providers, "_lease", AsyncMock(return_value=object()))
    monkeypatch.setattr(providers, "_accept_native", accept)

    async def select_with_backlog(*args: object) -> tuple[list[object], None]:
        run.extras["selection"] = {"backlog": 1}
        return [], None

    monkeypatch.setattr(providers, "_select", select_with_backlog)
    await providers._run_tvmaze(run)
    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"old"'
    assert accept.await_args.args[5].update_validators and accept.await_args.args[5].etag is None
    assert run.extras["native_fp"] == fingerprint

    state.continuation_state = "different-url"
    await providers._run_tvmaze(run)
    assert "If-None-Match" not in fetch.await_args.kwargs["headers"]
