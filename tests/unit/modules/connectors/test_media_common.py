"""Unit tests for shared media collector helpers in ``modules/connectors/providers/_media_common.py``."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from modules.connectors.providers import _media_common as mc
from modules.connectors.providers.news import ProviderPayloadError


def test_media_user_agent_formats_and_strips_control_chars() -> None:
    """User agent helper builds descriptive strings and strips control characters."""
    assert mc.media_user_agent() == mc.MEDIA_USER_AGENT
    ua = mc.media_user_agent("anilist\n_anime", "owner@example.com\r")
    assert "anilist _anime" in ua
    assert "mailto:owner@example.com" in ua
    assert "\n" not in ua and "\r" not in ua


@pytest.mark.asyncio
async def test_media_rate_pacer_enforces_per_key_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cooperative pacer sleeps only for remaining interval on the same key."""
    clock = [100.0]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(mc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    pacer = mc.MediaRatePacer()
    assert await pacer.pace("api.musicbrainz.org", 1.0) == 0.0
    clock[0] += 0.25
    waited = await pacer.pace("api.musicbrainz.org", 1.0)
    assert waited == pytest.approx(0.75)
    assert sleeps == [pytest.approx(0.75)]
    # Separate host key is independent.
    assert await pacer.pace("graphql.anilist.co", 1.0) == 0.0
    pacer.reset("api.musicbrainz.org")
    assert await pacer.pace("api.musicbrainz.org", 1.0) == 0.0


def test_rate_limit_delay_from_headers() -> None:
    """Inspect Retry-After and X-RateLimit-Remaining headers safely."""
    assert mc.rate_limit_delay_from_headers({}) is None
    assert mc.rate_limit_delay_from_headers({"Retry-After": "12"}) == 12.0
    assert mc.rate_limit_delay_from_headers({"retry-after": "99999"}) == 3600.0
    future = (datetime.now(UTC) + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    delay = mc.rate_limit_delay_from_headers({"Retry-After": future})
    assert delay is not None and 20.0 <= delay <= 35.0
    assert mc.rate_limit_delay_from_headers({"X-RateLimit-Remaining": "0"}) == 60.0
    assert mc.rate_limit_delay_from_headers(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "45"}
    ) == 45.0
    assert mc.rate_limit_delay_from_headers({"X-RateLimit-Remaining": "5"}) is None


def test_parse_media_xml_and_helpers() -> None:
    """XML parser reuses safe_xml_root protections and extracts local names/links."""
    atom = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<feed xmlns="http://www.w3.org/2005/Atom">'
        b'<entry><title>Ep 1</title><link rel="alternate" href="https://example.com/1"/></entry>'
        b"</feed>"
    )
    root = mc.parse_media_xml(atom)
    assert mc.xml_local(root.tag) == "feed"
    entry = next(iter(root))
    assert mc.xml_child_text(entry, "title") == "Ep 1"
    assert mc.xml_atom_link(entry) == "https://example.com/1"

    with pytest.raises(ProviderPayloadError):
        mc.parse_media_xml(b"<!DOCTYPE foo><feed/>")


@pytest.mark.parametrize(
    ("raw", "exp_iso", "exp_year", "has_dt"),
    [
        ("2026-11-17T12:30:00Z", "2026-11-17T12:30:00Z", 2026, True),
        ("2026-11-17T14:30:00+02:00", "2026-11-17T12:30:00Z", 2026, True),
        ("2026-11-17", "2026-11-17", 2026, True),
        ("2026-11", "2026-11", 2026, True),
        ("2026", "2026", 2026, True),
        ("Q4 2026", None, 2026, False),
        ("Mon, 16 Nov 2026 08:00:00 +0000", "2026-11-16T08:00:00Z", 2026, True),
        ("1799", None, None, False),
        ("TBA", None, None, False),
        (None, None, None, False),
    ],
)
def test_normalize_partial_date(
    raw: object, exp_iso: str | None, exp_year: int | None, has_dt: bool
) -> None:
    """Partial dates, quarters, RFC 3339, and RFC 2822 normalize consistently."""
    iso, year, dt = mc.normalize_partial_date(raw)
    assert iso == exp_iso
    assert year == exp_year
    assert (dt is not None) is has_dt
    if dt is not None:
        assert dt.tzinfo == UTC


def test_normalize_cover_url() -> None:
    """HTTP cover URLs upgrade to HTTPS and unsafe hosts/IPs are rejected."""
    assert (
        mc.normalize_cover_url("http://books.google.com/books/content?id=1")
        == "https://books.google.com/books/content?id=1"
    )
    assert mc.normalize_cover_url("https://uploads.mangadex.org/covers/a/b.jpg") is not None
    assert mc.normalize_cover_url("https://127.0.0.1/a.jpg") is None
    assert mc.normalize_cover_url("https://localhost/a.jpg") is None
    assert mc.normalize_cover_url("ftp://example.com/a.jpg") is None
    assert mc.normalize_cover_url(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01:02:03", 3723),
        ("12:34", 754),
        ("90:00", 5400),
        ("420", 420),
        (360, 360),
        (360.9, 360),
        (True, None),
        (-5, None),
        ("9999999", None),
        ("12:75", None),
        ("bad", None),
    ],
)
def test_parse_itunes_duration(raw: object, expected: int | None) -> None:
    """iTunes durations parse HH:MM:SS, MM:SS, and seconds within [0, 604800]."""
    assert mc.parse_itunes_duration(raw) == expected


def test_normalize_rating_country_language_genres_and_content_rating() -> None:
    """Taxonomy normalizers map upstream values onto canonical media schemas."""
    assert mc.normalize_rating(4.5, scale=5) == 90.0
    assert mc.normalize_rating("8.6", scale=10) == 86.0
    assert mc.normalize_rating(85, scale=100) == 85.0
    assert mc.normalize_rating(True, scale=10) is None
    assert mc.normalize_rating(float("nan"), scale=100) is None
    assert mc.normalize_rating(11, scale=10) is None

    assert mc.normalize_country("USA") == "US"
    assert mc.normalize_country("UK") == "GB"
    assert mc.normalize_country("South Korea") == "KR"
    assert mc.normalize_country("tw") == "TW"
    assert mc.normalize_country("invalid-country") is None

    assert mc.normalize_language("eng") == "en"
    assert mc.normalize_language("jpn") == "ja"
    assert mc.normalize_language("fre") == "fr"
    assert mc.normalize_language("vie") == "vi"
    assert mc.normalize_language("zh-HK") == "zh-hk"
    assert mc.normalize_language("en-US") == "en-us"
    assert mc.normalize_language("pt_BR") == "pt-br"
    assert mc.normalize_language("schinese") == "zh"
    assert mc.normalize_language("English") == "en"

    genres = mc.normalize_genres(["Action & Adventure", "Sci-Fi", "sci_fi", "Unknown"])
    assert genres == ["action", "adventure", "sci_fi"]
    assert mc.normalize_genres(["10759", "10765"], provider="tmdb_tv") == [
        "action",
        "adventure",
        "sci_fi",
        "fantasy",
    ]
    assert mc.normalize_genres(["391b0423-d847-456f-aff0-8b0cfc03066b"], provider="mangadex_releases") == [
        "action"
    ]
    # Capped at 12 items.
    many = mc.normalize_genres([
        "action", "adventure", "sci_fi", "fantasy", "romance", "thriller", "horror",
        "comedy", "drama", "mystery", "documentary", "animation", "history", "music",
    ])
    assert len(many) == 12

    assert mc.normalize_content_rating("TV-14") == "pg_13"
    assert mc.normalize_content_rating("R") == "r"
    assert mc.normalize_content_rating(True) == "explicit"
    assert mc.normalize_content_rating(False) == "clean"


def test_build_media_identity_and_external_ids() -> None:
    """Identity builder hashes >480-char IDs and external_ids enforces allowlist and bounds."""
    assert mc.build_media_identity("anilist_anime", 21) == "anilist_anime:21"
    assert mc.build_media_identity("youtube", "dQw4w9WgXcQ", scope="UC123") == "youtube:UC123:dQw4w9WgXcQ"
    long_id = mc.build_media_identity("podcast_rss", "x" * 500)
    assert long_id.startswith("sha256:") and len(long_id) == 71

    ext = mc.build_external_ids(
        {"tmdb": 123, "imdb": " tt123 ", "unknown_key": "1", "mal": None, "trakt": True, "kitsu": "x" * 65}
    )
    assert ext == {"tmdb": "123", "imdb": "tt123"}
