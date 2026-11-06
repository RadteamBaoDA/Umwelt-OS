import json
import logging
from uuid import uuid4

import httpx
import pytest

from modules.connectors import live_map


def test_usgs_projection_bounds_and_https_links():
    body = json.dumps(
        {
            "features": [
                {
                    "id": "e1",
                    "properties": {
                        "title": "Earthquake",
                        "time": 1700000000000,
                        "url": "http://invalid.example",
                    },
                    "geometry": {"coordinates": [120, 15]},
                }
            ]
        }
    ).encode()
    items, truncated = live_map._parse("usgs", body)
    assert not truncated
    assert items[0]["latitude"] == 15 and items[0]["longitude"] == 120
    assert items[0]["url"] is None
    assert items[0]["observed_at"].startswith("2023-")


def test_cisa_caps_latest_additions_and_usgs_covers_the_month():
    rows = [
        {
            "cveID": f"CVE-2026-{i:04d}",
            "vulnerabilityName": "Issue",
            "dateAdded": f"2026-01-{1 + i // 24:02d}",
        }
        for i in range(120)
    ]
    items, truncated = live_map._parse("cisa_kev", json.dumps({"vulnerabilities": rows}).encode())
    assert truncated and len(items) == 100
    assert items[0]["observed_at"].startswith("2026-01-05")
    assert items[-1]["observed_at"].startswith("2026-01-01")
    assert live_map._endpoint("usgs", None, None, None, None, None).endswith("/4.5_month.geojson")


def test_feed_refuses_dtd_and_normalizes_rss_time():
    with pytest.raises(ValueError):
        live_map._parse("business", b'<!DOCTYPE rss [<!ENTITY x "oops">]><rss/>')
    body = b"<rss><channel><item><guid>1</guid><title>News</title><pubDate>Wed, 04 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    items, _ = live_map._parse("business", body)
    assert items[0]["observed_at"] == "2026-11-04T10:00:00+00:00"


def test_eonet_uses_newest_timestamp_and_does_not_guess_polygon_location():
    body = json.dumps(
        {
            "events": [
                {
                    "id": "x",
                    "title": "Event",
                    "geometry": [
                        {
                            "date": "2026-11-04T03:00:00Z",
                            "type": "Polygon",
                            "coordinates": [[[1, 2]]],
                        },
                        {"date": "2026-11-03T03:00:00Z", "type": "Point", "coordinates": [1, 2]},
                    ],
                    "sources": [
                        {"id": "agency", "url": "https://example.com/event"},
                        {"url": "http://unsafe.example"},
                    ],
                }
            ]
        }
    ).encode()
    items, _ = live_map._parse("eonet", body)
    assert items[0]["latitude"] is None
    assert items[0]["observed_at"] == "2026-11-04T03:00:00+00:00"
    assert items[0]["sources"] == [{"id": "agency", "url": "https://example.com/event"}]


def test_catalog_uses_distinct_reviewed_tile_and_release_identities():
    from modules.connectors.provider_specs import get_quota_policies
    from modules.connectors.public import CATALOG_IDS, live_terms_catalog

    catalog = live_terms_catalog()
    assert catalog["openweather"]["eligibility"] == "review"
    assert catalog["github_releases"]["eligibility"] == "review"
    assert catalog["business"]["label"] == "VnExpress Business"
    assert get_quota_policies(CATALOG_IDS["openweather"])
    assert get_quota_policies(CATALOG_IDS["github_releases"])


@pytest.mark.parametrize(
    "provider,kwargs",
    [
        ("github_releases", {"owner": "../evil", "repo": "test"}),
        ("sec", {"cik": "1/../"}),
        ("firms", {"country": "../../evil", "secret": "key"}),
    ],
)
def test_provider_scope_never_selects_arbitrary_host(provider, kwargs):
    values = {"country": None, "owner": None, "repo": None, "cik": None, "secret": None}
    values.update(kwargs)
    with pytest.raises(ValueError):
        live_map._endpoint(provider, **values)


@pytest.mark.asyncio
async def test_keyed_transport_pins_and_does_not_log_key(monkeypatch, caplog):
    secret = "unique-super-secret-value"
    sent = []

    async def resolve(host, port):
        assert host == "tile.openweathermap.org" and port == 443
        return ["1.1.1.1"]

    async def before_send():
        sent.append("admitted")

    async def handle(request):
        assert request.url.host == "1.1.1.1"
        assert request.headers["host"] == "tile.openweathermap.org"
        assert request.extensions["sni_hostname"] == "tile.openweathermap.org"
        assert secret in str(request.url)
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfixture", request=request)

    monkeypatch.setattr(live_map.rest, "resolve_public", resolve)
    monkeypatch.setattr(
        live_map.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    with caplog.at_level(logging.DEBUG):
        body = await live_map.fetch_weather("temperature", 2, 1, 1, secret, before_send=before_send)
    assert body.startswith(b"\x89PNG") and sent == ["admitted"]
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_weather_rejects_bad_coordinates_without_network():
    async def before_send():
        raise AssertionError("must not send")

    with pytest.raises(ValueError):
        await live_map.fetch_weather("wind", 2, 4, 0, "key", before_send=before_send)


@pytest.mark.asyncio
async def test_durable_quota_receives_only_provider_and_no_workspace_or_key(monkeypatch):
    captured = {}

    async def debit(session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(live_map.quota, "debit_transient_send", debit)
    await live_map.debit_live_send(None, provider="firms", workspace_id=uuid4())
    assert captured == {"provider_id": "nasa_firms"}


@pytest.mark.asyncio
async def test_transient_quota_updates_only_shared_window_rows():
    """A live send never adds a per-send or per-workspace record."""
    from modules.connectors import quota

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Result:
        def first(self):
            return (1,)

    class Session:
        def __init__(self):
            self.statements = []

        def begin_nested(self):
            return Transaction()

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    session = Session()
    await quota.debit_transient_send(session, provider_id="nasa_firms")
    assert session.statements
    assert all(
        statement.table.name == "connector_quota_windows" for statement in session.statements
    )


@pytest.mark.parametrize(
    ("provider", "payload", "title"),
    [
        (
            "github_releases",
            [
                {
                    "id": 8,
                    "tag_name": "v1",
                    "published_at": "2026-11-04T00:00:00Z",
                    "html_url": "https://github.com/a/b/releases/tag/v1",
                }
            ],
            "v1",
        ),
        (
            "nvd",
            {
                "totalResults": 1,
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2026-1234",
                            "published": "2026-11-04T00:00:00Z",
                            "descriptions": [{"lang": "en", "value": "Issue"}],
                        }
                    }
                ],
            },
            "CVE-2026-1234",
        ),
        (
            "sec",
            {
                "cik": "1",
                "name": "Company",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000000001-26-000001"],
                        "form": ["10-Q"],
                        "filingDate": ["2026-11-04"],
                        "primaryDocument": ["report.htm"],
                    }
                },
            },
            "Company: 10-Q",
        ),
    ],
)
def test_technology_and_finance_projection_has_real_source_and_no_guessed_coordinates(
    provider, payload, title
):
    items, truncated = live_map._parse(provider, json.dumps(payload).encode())
    assert not truncated and items[0]["title"] == title
    assert items[0]["source"] and items[0]["url"].startswith("https://")
    assert items[0]["latitude"] is None and items[0]["longitude"] is None


def test_firms_marks_detection_as_unconfirmed_and_preserves_coordinates():
    body = b"latitude,longitude,acq_date,acq_time,satellite\n10.2,106.5,2026-11-04,915,N\n"
    items, truncated = live_map._parse("firms", body)
    assert not truncated and items[0]["latitude"] == 10.2 and items[0]["longitude"] == 106.5
    assert "not a confirmed fire" in items[0]["title"]
    assert items[0]["observed_at"] == "2026-11-04T09:15:00+00:00"
