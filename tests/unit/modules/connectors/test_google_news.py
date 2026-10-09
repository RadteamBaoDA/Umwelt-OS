"""Google News RSS preset: server-built URL, allowlists, publisher attribution. Static fixture only; no network."""

import pytest
from pydantic import ValidationError

from modules.connectors.catalog import get_catalog_entry
from modules.connectors.provider_specs import DISPATCHABLE, get_provider_spec
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.news import (
    GOOGLE_NEWS_LICENSE,
    capture_validators,
    feed_request,
    google_news_url,
    map_feed,
    preset_url,
)
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import NOW, fixture


def test_url_is_fixed_host_path_with_escaped_query():
    url = google_news_url("Giá vàng & USD=1", "apnews.com", "vi-VN")
    assert url == (
        "https://news.google.com/rss/search?q=Gi%C3%A1%20v%C3%A0ng%20%26%20USD%3D1%20site%3Aapnews.com"
        "&hl=vi&gl=VN&ceid=VN%3Avi"
    )
    assert google_news_url("a", "any", "en-US").endswith("?q=a&hl=en&gl=US&ceid=US%3Aen")


def test_control_characters_stripped_and_injection_is_inert():
    url = google_news_url("a\r\nb\x00c&hl=xx#frag", "any", "en-US")
    assert "\n" not in url and "%0A" not in url and "%00" not in url
    assert url.count("hl=") == 1 and "#" not in url and url.count("&") == 3  # only our own separators


@pytest.mark.parametrize("query, site, locale", [
    ("", "any", "en-US"), ("   ", "any", "en-US"), ("x" * 201, "any", "en-US"), (None, "any", "en-US"),
    ("x", "evil.com", "en-US"), ("x", "https://evil.com", "en-US"), ("x", "any", "fr-FR"), ("x", "any", "en-us"),
    ("x site:evil.com", "any", "en-US"), ("x SITE : evil.com", "reuters.com", "en-US"),
])
def test_rejects_unknown_site_locale_and_bad_query(query, site, locale):
    with pytest.raises(ProviderPayloadError):
        google_news_url(query, site, locale)


def test_config_model_validates_scope_and_strips_controls():
    cfg = ConnectorConfig(news_query="a\x07\n b", news_site="reuters.com", news_locale="vi-VN")
    assert cfg.news_query == "a b"
    for bad in ({"news_site": "evil.com"}, {"news_locale": "de-DE"}, {"news_query": "x" * 201}, {"news_query": "\x00"},
                {"news_query": "q site:evil.com"}):
        with pytest.raises(ValidationError):
            ConnectorConfig(**bad)


def test_fixture_maps_publisher_license_and_keeps_item_link():
    records = {r.provider_id: r for r in map_feed("google_news", fixture("google_news_rss.xml"), NOW)}
    assert set(records) == {"SYNTHETIC0001", "SYNTHETIC0002", "SYNTHETIC0003"}
    first = records["SYNTHETIC0001"].metadata["provider_record"]
    assert first["source_fields"]["publisher"] == "Reuters"
    assert first["license_label"] == GOOGLE_NEWS_LICENSE == "Google News RSS / publisher"
    assert first["source_fields"]["canonical_url"] == "https://news.google.com/rss/articles/SYNTHETIC0001?oc=5"
    assert records["SYNTHETIC0002"].metadata["provider_record"]["source_fields"]["publisher"] == "AP News"
    assert "publisher" not in records["SYNTHETIC0003"].metadata["provider_record"]["source_fields"]
    assert first["world_data"] is None and records["SYNTHETIC0001"].metadata["full_text_available"] is False


def test_request_uses_supplied_url_and_validators_bind_to_it():
    url = preset_url("google_news", {"news_query": "q", "news_site": "any", "news_locale": "en-US"})
    assert feed_request("google_news", url=url).url == url
    assert capture_validators("google_news", {"etag": '"e"'}, "3", url).url == url
    with pytest.raises(ProviderPayloadError):
        preset_url("google_news", {})


def test_spec_and_catalog_flags():
    spec = get_provider_spec("google_news")
    assert spec and spec.eligibility == "review" and spec.default_interval_minutes == 30
    assert spec.hosts == ("news.google.com",) and spec.endpoints == ("/rss/search",)
    assert spec.code_implemented and not spec.runtime_verified and "google_news" in DISPATCHABLE
    entry = get_catalog_entry("google_news")
    assert entry and entry.availability == "implemented" and not entry.runtime_verified
    assert set(entry.scope_fields) == {"news_query", "news_site", "news_locale"}
    assert entry.example_config["news_locale"] == "en-US" and entry.sample_output["publisher"]


def _source(**config):
    from types import SimpleNamespace
    from uuid import uuid4

    return SimpleNamespace(id=uuid4(), status="active", type="rss", provider="google_news", generation=1,
                           configuration={"history_mode": "returned_snapshot", **config})


def test_registry_requires_full_scope_and_rejects_raw_urls():
    from modules.connectors import registry

    ok = {"news_query": "economy", "news_site": "apnews.com", "news_locale": "vi-VN"}
    assert registry.validate(_source(**ok))["configuration"]["news_site"] == "apnews.com"
    for bad in ({"news_query": "economy", "news_locale": "vi-VN"}, {**ok, "feed_url": "https://evil.example/rss"},
                {**ok, "news_site": "evil.com"}):
        with pytest.raises(ValueError):
            registry.validate(_source(**bad))
