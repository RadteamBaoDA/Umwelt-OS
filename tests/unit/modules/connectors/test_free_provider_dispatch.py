"""Executor wiring for the P2/P3 fixed-endpoint providers: fakes only (no DB, no network)."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.connectors import collection
from modules.connectors import public as connectors
from modules.connectors.provider_specs import DISPATCHABLE, get_provider_spec
from modules.connectors.providers import rest
from modules.connectors.providers.gdelt import GDELT_TIMEOUT_SECONDS
from modules.connectors.providers.macro import ProviderPayloadError
from tests.unit.modules.connectors.p3_helpers import fixture


class Gate:
    """Stands in for SendGate.fetch_bytes: one entry in ``calls`` per debited send."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def fetch_bytes(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url] if url in self.responses else self.responses["*"]
        if isinstance(response, Exception):
            raise response
        return response


def make_run(provider, gate, monkeypatch, state=None, connector_revision=2, configuration=None):
    accepted = []

    async def lease(_run):
        return SimpleNamespace(token=uuid4(), cursor_before=None)

    async def accept(_run, _lease, records, coverage, collected_at, state_update=None):
        accepted.append((tuple(records), coverage, state_update))

    async def get_state(_run):
        return state or SimpleNamespace(etag=None, last_modified=None, validators_revision=None)

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_state", get_state)
    source = SimpleNamespace(provider=provider, configuration=configuration or {})
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=source, connector_revision=connector_revision), gate=gate, extras={})
    return run, accepted


def test_every_dispatchable_provider_has_an_adapter_and_spec():
    from modules.connectors.backends import PROVIDER_SOURCE_TYPES

    assert all(collection._adapter_for(SimpleNamespace(
        provider=provider, type=PROVIDER_SOURCE_TYPES[provider])) is not None for provider in DISPATCHABLE)
    assert "coingecko" in DISPATCHABLE  # key-bearing: needs the owner credential slot
    assert all(get_provider_spec(p) is not None for p in DISPATCHABLE)


async def test_bluesky_dispatch_commits_adapter_cursor_with_the_native_receipt(monkeypatch):
    from modules.connectors.providers import social_feeds

    lease = SimpleNamespace(token=uuid4(), cursor_before='{"v":1,"actor":"alice.bsky.social","page":"old"}')
    page = SimpleNamespace(records=(), coverage="truncated", cursor_after='{"page":"next"}')
    collect = AsyncMock(return_value=page)
    accept = AsyncMock()
    monkeypatch.setattr(collection, "_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_credential_fence", AsyncMock())
    monkeypatch.setattr(social_feeds, "collect_bluesky_profile", collect)

    @asynccontextmanager
    async def factory():
        yield object()

    source = SimpleNamespace(provider="bluesky", configuration={"bluesky_actor": "alice.bsky.social"})
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=source, connector_revision=2),
        gate=SimpleNamespace(fetch_bytes=AsyncMock()), extras={}, factory=factory,
    )
    await collection._run_native(run)

    assert collect.await_args.kwargs["cursor"] == lease.cursor_before
    assert accept.await_args.kwargs["cursor_after"] == page.cursor_after


@pytest.mark.parametrize("provider, name, max_bytes", [
    ("world_bank", "world_bank_vn_gdp.json", 2 * 1024 * 1024),
    ("ecb", "ecb_eurofxref_daily.xml", 256 * 1024),
    ("alternative_me", "fear_greed.json", 2 * 1024 * 1024),
    ("cisa_kev", "cisa_kev.json", 4 * 1024 * 1024),
])
async def test_pure_provider_one_gated_send_then_typed_measurements(monkeypatch, provider, name, max_bytes):
    gate = Gate({"*": rest.Fetched(fixture(name))})
    run, accepted = make_run(provider, gate, monkeypatch)
    await collection._run_pure(run)
    assert len(gate.calls) == 1 and gate.calls[0][1]["max_bytes"] == max_bytes
    records, coverage, _ = accepted[0]
    assert records and all(r.metadata["provider_record"]["world_data"]["provider"] == provider for r in records)
    assert coverage == ("truncated" if provider == "world_bank" else "returned_snapshot")


async def test_pure_provider_304_is_not_a_page(monkeypatch):
    run, accepted = make_run("usgs", Gate({"*": rest.Fetched(None)}), monkeypatch)
    with pytest.raises(rest.ProviderHttpError):
        await collection._run_pure(run)
    assert not accepted


async def test_feed_first_fetch_stores_validators_and_carries_no_world_data(monkeypatch):
    gate = Gate({"*": rest.Fetched(fixture("bbc_world_rss.xml"), '"v1"', "Wed, 07 Oct 2026 08:00:00 GMT")})
    run, accepted = make_run("bbc_world", gate, monkeypatch)
    await collection._run_feed(run)
    records, _, update = accepted[0]
    assert records and all(r.metadata["provider_record"]["world_data"] is None for r in records)
    assert update.update_validators and update.etag == '"v1"' and update.last_modified.startswith("Wed")
    assert "If-None-Match" not in gate.calls[0][1]["headers"]


async def test_feed_replays_validators_and_304_keeps_last_good(monkeypatch):
    state = SimpleNamespace(etag='"v1"', last_modified=None, validators_revision=2)
    gate = Gate({"*": rest.Fetched(None)})
    run, accepted = make_run("bbc_world", gate, monkeypatch, state=state)
    await collection._run_feed(run)
    assert gate.calls[0][1]["headers"]["If-None-Match"] == '"v1"'
    assert accepted == [((), "returned_snapshot", None)]  # empty acceptance: no records, validators untouched


async def test_feed_ignores_validators_from_another_revision(monkeypatch):
    state = SimpleNamespace(etag='"v1"', last_modified=None, validators_revision=1)
    gate = Gate({"*": rest.Fetched(fixture("bbc_world_rss.xml"))})
    run, _ = make_run("bbc_world", gate, monkeypatch, state=state)
    await collection._run_feed(run)
    assert "If-None-Match" not in gate.calls[0][1]["headers"]


async def test_hn_sends_one_listing_plus_at_most_ten_items_each_gated(monkeypatch):
    story = fixture("hn_item_story.json")
    gate = Gate({
        "https://hacker-news.firebaseio.com/v0/topstories.json": rest.Fetched(fixture("hn_topstories.json")),
        "https://hacker-news.firebaseio.com/v0/item/41000002.json": rest.ProviderHttpError(404),
        "*": rest.Fetched(story),
    })
    run, accepted = make_run("hn_top", gate, monkeypatch)
    await collection._run_hn(run)
    assert len(gate.calls) == 11  # 1 listing + 10 items, each a separately debited send
    assert run.extras["skipped"]
    assert all(r.metadata["provider_record"]["world_data"] is None for r in accepted[0][0])


async def test_gdelt_uses_8_second_timeout_and_timeout_accepts_nothing(monkeypatch):
    gate = Gate({"*": rest.Fetched(fixture("gdelt_artlist.json"))})
    run, accepted = make_run("gdelt_economy", gate, monkeypatch)
    await collection._run_gdelt(run)
    assert gate.calls[0][1]["timeout"] == GDELT_TIMEOUT_SECONDS == 30 and accepted[0][0]
    slow, kept = make_run("gdelt_economy", Gate({"*": TimeoutError()}), monkeypatch)
    with pytest.raises(TimeoutError):
        await collection._run_gdelt(slow)
    assert not kept  # last good records stay; classified as a retryable provider_timeout
    assert collection._classify(TimeoutError())["error_code"] == "provider_timeout"


@pytest.mark.parametrize("kind, expected", [
    ("rate_limited", connectors.ProviderRateLimited), ("incomplete", rest.CollectionIncomplete),
    ("credential", rest.ProviderHttpError), ("entitlement", rest.ProviderHttpError), ("invalid", ProviderPayloadError),
])
def test_payload_error_kinds_map_to_executor_signals(kind, expected):
    def boom():
        raise ProviderPayloadError("x", kind)

    with pytest.raises(expected):
        collection._payload(boom)
    assert json.dumps(collection._classify(ProviderPayloadError("x"))["error_code"]) == '"provider_response_invalid"'


@pytest.mark.parametrize("provider", sorted(DISPATCHABLE))
def test_registration_agrees_across_registry_sources_and_scope_validators(provider):
    from modules.connectors import registry
    from modules.connectors.backends import (
        PROVIDER_SCOPE_FIELDS,
        PROVIDER_SOURCE_TYPES,
        native_dispatch_supported,
    )
    from modules.sources.schemas import SourceCreate

    source_type = PROVIDER_SOURCE_TYPES[provider]
    SourceCreate(type=source_type, name="n", provider=provider)  # sources schema map agrees
    scope = {
        "bluesky": {"bluesky_actor": "example.bsky.social"},
        "reddit": {"reddit_subreddit": "python"},
        "mastodon": {"mastodon_instance_url": "https://mastodon.social", "mastodon_account": "alice@mastodon.social"},
        "x": {"x_account": "example"},
        "google_mail": {"google_mail_label_ids": ["INBOX"]},
        "google_calendar": {"google_calendar_ids": ["primary"]},
        "google_drive": {"google_drive_ids": ["file-id"]},
        "health": {"fhir_base_url": "http://127.0.0.1:8080/fhir", "fhir_patient_id": "patient-1"},
        "home_assistant": {"ha_base_url": "http://127.0.0.1:8123", "ha_entity_ids": ["sensor.temperature"]},
        "notion": {"notion_page_ids": ["a1234567-b123-c123-d123-e123456789ab"]},
        "slack": {"slack_channel_ids": ["C012ABCDEF"]},
        "google_news": {"news_query": "economy", "news_site": "any", "news_locale": "en-US"},
        "sec_edgar": {"sec_cik": "0000320193", "sec_contact_email": "owner@example.com"},
        "openalex": {"scholarly_query": "economy"},
        "fred": {"fred_series_id": "DFF"},
        "openweathermap": {"owm_latitude": 21.0, "owm_longitude": 105.8},
        "openaq": {"openaq_location_id": 8118},
        "nasa_firms": {"firms_country": "VNM"},
    }.get(provider, {})
    assert native_dispatch_supported(source_type, provider) and PROVIDER_SCOPE_FIELDS[provider] == frozenset(scope)
    source = SimpleNamespace(
        id=uuid4(), status="active", type=source_type, provider=provider, generation=1,
        configuration={"history_mode": "returned_snapshot", **scope})
    assert registry.validate(source)["provider"] == provider
    source.configuration = {"history_mode": "returned_snapshot", **scope, "feed_url": "https://x.example/f"}
    with pytest.raises(ValueError):
        registry.validate(source)  # owner-supplied scope is refused for fixed endpoints
