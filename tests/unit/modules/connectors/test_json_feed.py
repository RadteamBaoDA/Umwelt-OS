"""Table-driven JSON feeds: one fixture per feed, plus missing-path, oversize and registration checks (no network)."""

from datetime import UTC, datetime

import pytest

from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.providers.json_feed import (
    JSON_FEEDS,
    feed_request,
    json_feed_adapters,
    map_json_feed,
)
from modules.connectors.providers.macro import MAX_BODY_BYTES, ProviderPayloadError
from modules.connectors.providers.world_data import PURE_ADAPTERS
from tests.unit.modules.connectors.p3_helpers import fixture

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # after the fixtures' live capture dates
FIXTURES = {
    "gdacs_events": "gdacs_events.json", "nws_alerts": "nws_alerts.json", "emsc_quakes": "emsc_quakes.json",
    "launch_library": "launch_library.json", "wikipedia_mostread": "wikipedia_mostread.json",
    "wikipedia_top_views": "wikipedia_top_views.json", "bmkg_quakes": "bmkg_quakes.json",
    "satnogs_observations": "satnogs_observations.json",
}


def test_every_feed_has_a_fixture_registration_and_source_type():
    assert set(FIXTURES) == set(JSON_FEEDS)
    for provider in JSON_FEEDS:
        assert provider in PURE_ADAPTERS and PROVIDER_SOURCE_TYPES[provider] == "api"
        assert feed_request(JSON_FEEDS[provider]).url.startswith("https://")


@pytest.mark.parametrize("provider", sorted(FIXTURES))
def test_fixture_maps_to_news_shaped_records(provider):
    records = map_json_feed(JSON_FEEDS[provider], fixture(FIXTURES[provider]), NOW)
    assert records
    for rec in records:
        meta = rec.metadata["provider_record"]
        assert meta["provider"] == provider and meta["world_data"] is None
        assert rec.metadata["title"] and rec.provider_id.startswith(f"{provider}:")
        assert meta["source_fields"]["publisher"] == JSON_FEEDS[provider].publisher
        assert meta["license_label"]
    assert len({r.provider_id for r in records}) == len(records)


def test_gdacs_naive_time_is_treated_as_utc_and_location_is_in_summary():
    first = map_json_feed(JSON_FEEDS["gdacs_events"], fixture("gdacs_events.json"), NOW)[0]
    assert first.metadata["provider_record"]["timestamp_basis"] == "provider_published"
    assert first.observed_at.tzinfo is not None and "Location:" in first.content


def test_wikipedia_top_views_skips_non_content_pages_and_links_articles():
    records = map_json_feed(JSON_FEEDS["wikipedia_top_views"], fixture("wikipedia_top_views.json"), NOW)
    titles = {r.metadata["title"] for r in records}
    assert "Main Page" not in titles and "Kim Jong Un" in titles
    assert all(r.metadata["canonical_url"].startswith("https://en.wikipedia.org/wiki/") for r in records)


def test_identity_and_version_ignore_collection_clock():
    a = map_json_feed(JSON_FEEDS["bmkg_quakes"], fixture("bmkg_quakes.json"), NOW)
    b = map_json_feed(JSON_FEEDS["bmkg_quakes"], fixture("bmkg_quakes.json"), NOW.replace(hour=5))
    assert [(r.provider_id, r.version) for r in a] == [(r.provider_id, r.version) for r in b]


def test_missing_item_path_fails_loudly():
    with pytest.raises(ProviderPayloadError) as err:
        map_json_feed(JSON_FEEDS["emsc_quakes"], b'{"unexpected": []}', NOW)
    assert err.value.code == "emsc_quakes_response_invalid" and err.value.kind == "invalid"


def test_no_usable_items_is_incomplete_not_empty_success():
    with pytest.raises(ProviderPayloadError) as err:
        map_json_feed(JSON_FEEDS["emsc_quakes"], b'{"features": [{"id": "x"}]}', NOW)
    assert err.value.kind == "incomplete"


def test_oversize_body_is_refused():
    with pytest.raises(ProviderPayloadError, match="provider_response_too_large"):
        map_json_feed(JSON_FEEDS["emsc_quakes"], b" " * (MAX_BODY_BYTES + 1), NOW)


def test_non_json_body_is_refused():
    with pytest.raises(ProviderPayloadError, match="provider_response_not_json"):
        map_json_feed(JSON_FEEDS["nws_alerts"], b"<html>blocked</html>", NOW)


def test_date_addressed_feeds_resolve_to_a_concrete_day():
    url = feed_request(JSON_FEEDS["wikipedia_mostread"]).url
    assert "{" not in url and url.startswith("https://api.wikimedia.org/feed/v1/wikipedia/en/featured/")
    assert set(json_feed_adapters()) == set(JSON_FEEDS)
