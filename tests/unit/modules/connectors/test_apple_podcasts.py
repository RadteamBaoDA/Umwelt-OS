from datetime import UTC, datetime

import pytest

from modules.connectors.providers import apple_podcasts as media
from modules.connectors.providers.rest import Fetched

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def test_apple_books_url_is_canonical_and_bounded() -> None:
    assert media.apple_books_url("US", "books", "top-free", limit=50) == (
        "https://rss.marketingtools.apple.com/api/v2/us/books/top-free/50/books.json"
    )
    with pytest.raises(ValueError):
        media.apple_books_url("US", "books", "../redirect")
    with pytest.raises(ValueError):
        media.apple_books_url("US", "books", "top-free", limit=101)


def test_map_apple_books_normalizes_chart_record() -> None:
    records = media.map_apple_books(
        {"feed": {"results": [{
            "id": "123", "name": "A Story", "artistName": "A. Writer", "releaseDate": "2026-11-18",
            "url": "https://books.apple.com/us/book/a-story/id123", "artworkUrl100": "http://is1-ssl.mzstatic.com/image.jpg",
            "genres": [{"name": "Mystery"}], "contentAdvisoryRating": "Explicit",
        }]}},
        collected_at=NOW, config={}, kind="books",
    )
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["media_type"] == "book"
    assert fields["release_status"] == "upcoming"
    assert fields["genres"] == ["mystery"]
    assert fields["image_url"].startswith("https://")
    assert fields["content_rating"] == "explicit"


def test_unknown_apple_advisory_is_not_mislabeled_clean() -> None:
    records = media.map_apple_books(
        {"feed": {"results": [{"id": "456", "name": "Unrated", "contentAdvisoryRating": "Not Rated"}]}},
        collected_at=NOW, config={}, kind="books",
    )
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert "content_rating" not in fields


def test_podcast_maps_duration_language_categories_and_explicit_rating() -> None:
    body = b"""<rss xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\"><channel>
    <language>en</language><itunes:type>serial</itunes:type><itunes:explicit>no</itunes:explicit>
    <item><guid>ep-1</guid><title>Episode One</title>
    <description><![CDATA[<p>Summary</p>]]></description><pubDate>Tue, 18 Nov 2026 12:00:00 GMT</pubDate>
    <itunes:category text=\"Technology\"/><itunes:duration>1:02:03</itunes:duration>
    <itunes:explicit>yes</itunes:explicit><link>https://example.org/episodes/1</link>
    </item><item><guid>ep-2</guid><title>Episode Two</title></item></channel></rss>"""
    records = media.map_podcast_feed(body, collected_at=NOW, config={})
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["duration_seconds"] == 3723
    assert fields["language"] == "en"
    assert fields["content_rating"] == "explicit"
    assert fields["genres"] == ["programming"]
    assert fields["release_status"] == "upcoming"
    assert fields["format"] == "serial"
    fallback_fields = records[1].metadata["provider_record"]["source_fields"]
    assert fallback_fields["content_rating"] == "clean"
    assert fallback_fields["format"] == "serial"


def test_podcast_rejects_dtd_and_oversized_feed() -> None:
    with pytest.raises(ValueError, match="forbidden_xml_construct"):
        media.map_podcast_feed(b"<!DOCTYPE rss [<!ENTITY x 'x'>]><rss/>", collected_at=NOW, config={})
    many_items = b"<rss><channel>" + b"<item/>" * 101 + b"</channel></rss>"
    with pytest.raises(ValueError, match="podcast_feed_response_invalid"):
        media.map_podcast_feed(many_items, collected_at=NOW, config={})


def test_podcast_url_and_lookup_id_validation() -> None:
    assert media.podcast_feed_url("https://example.org/feed.xml") == "https://example.org/feed.xml"
    with pytest.raises(ValueError):
        media.podcast_feed_url("http://127.0.0.1/feed.xml")
    with pytest.raises(ValueError):
        media.itunes_podcast_lookup_url("12&url=http://127.0.0.1")


@pytest.mark.asyncio
async def test_collectors_send_conditional_headers_and_return_validators() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(url: str, *, headers: dict[str, str], max_bytes: int) -> Fetched:
        calls.append((url, headers))
        return Fetched(b'{"feed":{"results":[]}}', etag='"v1"', last_modified="Tue, 17 Nov 2026 12:00:00 GMT")

    page = await media.collect_apple_books(
        fetch, country="US", kind="books", chart="top-paid", collected_at=NOW, config={}, etag='"old"'
    )
    assert calls[0][0].endswith("/us/books/top-paid/100/books.json")
    assert calls[0][1]["If-None-Match"] == '"old"'
    assert page.etag == '"v1"'


@pytest.mark.asyncio
async def test_podcast_collector_resolves_itunes_lookup_with_gated_fetch() -> None:
    calls: list[str] = []

    async def fetch(url: str, *, headers: dict[str, str], max_bytes: int) -> Fetched:
        calls.append(url)
        if "lookup" in url:
            return Fetched(b'{"results":[{"feedUrl":"https://example.org/podcast.xml"}]}')
        return Fetched(b"<rss><channel></channel></rss>")

    await media.collect_podcast_rss(fetch, itunes_id="12345", collected_at=NOW, config={})
    assert calls == [
        "https://itunes.apple.com/lookup?id=12345&entity=podcast",
        "https://example.org/podcast.xml",
    ]
