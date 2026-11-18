"""Focused contract tests for the keyless movie providers."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.providers.media_movie_sources import (
    collect_letterboxd_rss,
    collect_wikidata_movies,
    itunes_movies_url,
    letterboxd_rss_url,
    map_itunes_movies,
    map_letterboxd_rss,
    map_wikidata_movies,
    wikidata_query_url,
)
from modules.connectors.providers.rest import Fetched

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def test_wikidata_url_has_validated_pushdown_and_bounded_dates() -> None:
    url = wikidata_query_url(
        {"genre_qids": ["Q130232", "Q157444"], "language": "EN", "lookback_days": 30}, NOW
    )
    query = parse_qs(urlsplit(url).query)["query"][0]
    assert '"2026-10-18T00:00:00Z"' in query
    assert '"2027-05-16T23:59:59Z"' in query
    assert "VALUES ?genre { wd:Q130232 wd:Q157444 }" in query
    assert 'FILTER(?languageCode = "en")' in query
    assert "LIMIT 100" in query
    with pytest.raises(ValueError, match="wikidata_scope_invalid"):
        wikidata_query_url({"genre_qids": ["Q1) } SERVICE <https://attacker>"]}, NOW)


def test_wikidata_maps_deduplicated_genres_country_language_and_ids() -> None:
    def row(genre: str) -> dict[str, object]:
        return {
            "item": {"value": "https://www.wikidata.org/entity/Q123"},
            "itemLabel": {"value": "Example Movie"},
            "date": {"value": "2027-01-10T00:00:00Z"},
            "genreLabel": {"value": genre},
            "countryCode": {"value": "US"},
            "languageCode": {"value": "en"},
            "imdb": {"value": "tt12345"},
            "tmdb": {"value": "987"},
        }

    records = map_wikidata_movies(
        {"results": {"bindings": [row("Drama"), row("Comedy")]}},
        collected_at=NOW,
        config={},
    )
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert records[0].provider_id == "wikidata:Q123"
    assert fields["genres"] == ["comedy", "drama"]
    assert fields["country"] == "US" and fields["language"] == "en"
    assert fields["external_ids"] == {"wikidata": "Q123", "imdb": "tt12345", "tmdb": "987"}
    assert fields["release_status"] == "upcoming"
    assert (
        map_wikidata_movies(
            {"results": {"bindings": [row("Drama")]}},
            collected_at=NOW,
            config={"release_year": 2025},
        )
        == []
    )


def test_itunes_url_and_mapping_keep_fixed_movie_endpoint_and_normalize_fields() -> None:
    url = itunes_movies_url({"search_terms": "Dune & Part Two", "country": "GB"})
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "itunes.apple.com"
    assert query == {
        "term": ["Dune & Part Two"],
        "country": ["gb"],
        "media": ["movie"],
        "entity": ["movie"],
        "lang": ["en_us"],
        "limit": ["200"],
    }
    records = map_itunes_movies(
        {
            "resultCount": 1,
            "results": [
                {
                    "trackId": 31,
                    "trackName": "Dune",
                    "trackViewUrl": "https://itunes.apple.com/movie/id31",
                    "releaseDate": "2026-12-01T00:00:00Z",
                    "artistName": "Denis Villeneuve",
                    "primaryGenreName": "Sci-Fi",
                    "averageUserRating": 4.5,
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image.jpg",
                    "longDescription": "A film.",
                }
            ],
        },
        collected_at=NOW,
        config={"country": "GB"},
    )
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert records[0].provider_id == "itunes:31"
    assert fields["genres"] == ["sci_fi"] and fields["rating"] == 90
    assert fields["country"] == "GB" and fields["creator"] == "Denis Villeneuve"
    assert fields["release_status"] == "upcoming" and "A film." in records[0].content
    with pytest.raises(ValueError, match="itunes_scope_invalid"):
        itunes_movies_url({"search_terms": "Dune", "country": "GB&entity=podcast"})


def test_letterboxd_limits_items_and_normalizes_ratings_and_year_filter() -> None:
    items = "".join(
        f"<item><title>Film {i}</title><link>https://letterboxd.com/film/film-{i}/</link>"
        f"<guid>https://letterboxd.com/film/film-{i}/</guid><letterboxd:filmYear>2024</letterboxd:filmYear>"
        f"<letterboxd:memberRating>4</letterboxd:memberRating><pubDate>Wed, 18 Nov 2026 20:00:00 GMT</pubDate>"
        "</item>"
        for i in range(55)
    )
    body = (
        f'<rss xmlns:letterboxd="https://letterboxd.com"><channel>{items}</channel></rss>'.encode()
    )
    records = map_letterboxd_rss(body, collected_at=NOW, config={})
    assert len(records) == 50
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["release_year"] == 2024 and fields["rating"] == 80
    assert records[0].metadata["provider_record"]["coverage"] == "truncated"
    assert records[0].observed_at == datetime(2026, 11, 18, 20, tzinfo=UTC)
    assert map_letterboxd_rss(body, collected_at=NOW, config={"min_rating": 90}) == []
    assert letterboxd_rss_url("film.fan-1") == "https://letterboxd.com/film.fan-1/rss/"
    with pytest.raises(ValueError, match="letterboxd_username_invalid"):
        letterboxd_rss_url("https://attacker.example/@me")
    with pytest.raises(ValueError):
        map_letterboxd_rss(b"<!DOCTYPE rss><rss/>", collected_at=NOW, config={})


@pytest.mark.asyncio
async def test_collectors_use_fixed_destinations_and_conditional_letterboxd() -> None:
    fetch = AsyncMock(return_value=Fetched(body=b'{"results":{"bindings":[]}}'))
    await collect_wikidata_movies(fetch, config={}, collected_at=NOW)
    assert fetch.await_args.args[0].startswith("https://query.wikidata.org/sparql?")
    assert fetch.await_args.kwargs["headers"]["User-Agent"].startswith("Umwelt-OS/1.0")
    assert fetch.await_args.kwargs["max_bytes"] == 2 * 1024 * 1024

    conditional = AsyncMock(return_value=Fetched(body=None))
    page = await collect_letterboxd_rss(
        conditional, username="owner", config={}, collected_at=NOW, etag='"v1"'
    )
    assert page.not_modified and not page.records
    assert conditional.await_args.args[0] == "https://letterboxd.com/owner/rss/"
    assert conditional.await_args.kwargs["headers"]["If-None-Match"] == '"v1"'
