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
        {"genre_qids": ["Q130232", "Q157443"], "language": "EN", "lookback_days": 30}, NOW
    )
    query = parse_qs(urlsplit(url).query)["query"][0]
    assert '"2026-10-18T00:00:00Z"' in query
    assert '"2027-05-16T23:59:59Z"' in query
    assert "VALUES ?genre { wd:Q130232 wd:Q157443 }" in query
    assert 'VALUES ?languageCode { "en" }' in query
    assert "?item wdt:P364/wdt:P218 ?languageCode." in query
    assert "OPTIONAL { ?item wdt:P364" not in query
    assert "LIMIT 100" in query
    with pytest.raises(ValueError, match="wikidata_scope_invalid"):
        wikidata_query_url({"genre_qids": ["Q1) } SERVICE <https://attacker>"]}, NOW)


def _query(config: dict[str, object]) -> str:
    return parse_qs(urlsplit(wikidata_query_url(config, NOW)).query)["query"][0]


def test_wikidata_pushes_down_spec_genre_qids_country_and_language() -> None:
    query = _query(
        {
            "media_genres": ["horror", "action", "documentary", "non_fiction"],
            "media_countries": ["JP", "US"],
            "media_languages": ["ja", "zh-hk", "en"],
        }
    )
    assert "VALUES ?genre { wd:Q188473 wd:Q200092 wd:Q93204 }" in query
    assert 'VALUES ?countryCode { "JP" "US" }' in query
    assert "?item wdt:P495/wdt:P297 ?countryCode." in query
    assert "OPTIONAL { ?item wdt:P495" not in query
    # zh-hk is not an ISO 639-1 code, so it can never match P218 and is dropped from the join.
    assert 'VALUES ?languageCode { "ja" "en" }' in query


def test_wikidata_partial_genre_mapping_and_unset_dimensions_stay_optional() -> None:
    query = _query({"media_genres": ["drama", "history"]})
    assert "OPTIONAL { ?item wdt:P136 ?genre. }" in query
    assert "VALUES ?genre" not in query
    assert "OPTIONAL { ?item wdt:P495/wdt:P297 ?countryCode. }" in query
    assert "OPTIONAL { ?item wdt:P364/wdt:P218 ?languageCode. }" in query
    explicit = _query({"genre_qids": ["Q11424"], "media_genres": ["drama"]})
    assert "VALUES ?genre { wd:Q11424 }" in explicit


def test_wikidata_country_selectors_intersect_and_reject_unsafe_codes() -> None:
    both = _query({"media_countries": ["US", "GB"], "media_country": "GB"})
    assert 'VALUES ?countryCode { "GB" }' in both
    with pytest.raises(ValueError, match="wikidata_scope_invalid"):
        _query({"media_countries": ['US" } SERVICE <https://attacker> { "']})


def test_wikidata_genre_qids_are_the_corrected_film_genre_items() -> None:
    from modules.connectors.providers.media_movie_sources import _GENRE_QIDS

    assert _GENRE_QIDS["action"] == "Q188473"
    assert _GENRE_QIDS["comedy"] == "Q157443"
    assert _GENRE_QIDS["animation"] == "Q202866"
    # Slugs that are not film genres stay unmapped (post-fetch filter only).
    assert not {"history", "music", "sports", "programming", "rpg", "strategy"} & _GENRE_QIDS.keys()


def test_wikidata_languages_are_lowercased_before_intersecting() -> None:
    query = _query({"media_languages": ["en", "ja"], "language": "EN"})
    assert 'VALUES ?languageCode { "en" }' in query


@pytest.mark.parametrize(
    "options",
    [
        {"media_countries": ["US"], "media_country": "GB"},
        {"media_languages": ["en"], "language": "ja"},
        {"media_languages": ["zh-hk"]},
    ],
)
def test_wikidata_empty_selector_intersection_is_rejected_not_rendered(
    options: dict[str, object],
) -> None:
    from modules.connectors.provider_options import validate_options

    with pytest.raises(ValueError, match="wikidata_scope_invalid"):
        validate_options("wikidata_movies", options)
    with pytest.raises(ValueError, match="wikidata_scope_invalid"):
        _query(options)


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
