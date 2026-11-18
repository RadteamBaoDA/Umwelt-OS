"""One-request MAL and Google Books collection contracts."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.catalog import get_catalog_entry
from modules.connectors.provider_options import native_fingerprint, validate_options
from modules.connectors.providers.keyed import KEYED
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.service.schemas import ConnectorConfig
from modules.connectors.service.scope import _scope_values

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("provider", "configuration", "path", "ranking", "nsfw"),
    [
        ("mal_anime", {}, "/v2/anime/ranking", "all", "false"),
        ("mal_manga", {}, "/v2/manga/ranking", "manga", "false"),
        ("mal_manga", {"mal_ranking_type": "manhwa"}, "/v2/manga/ranking", "manhwa", "false"),
        (
            "mal_anime",
            {
                "mal_mode": "season",
                "mal_season_year": 2026,
                "mal_season": "fall",
                "exclude_adult": False,
            },
            "/v2/anime/season/2026/fall",
            None,
            "true",
        ),
    ],
)
def test_mal_one_get_with_exact_route_header_and_nsfw(provider, configuration, path, ranking, nsfw):
    request = KEYED[provider].build(
        "private-key", {**configuration, "media_genres": ["action"], "max_items": 100}
    )
    url = urlsplit(request.url)
    query = parse_qs(url.query)
    assert request.method == "GET"
    assert (url.hostname, url.path) == ("api.myanimelist.net", path)
    assert request.headers["X-MAL-CLIENT-ID"] == "private-key"
    assert "private-key" not in repr(request)
    assert query.get("ranking_type") == ([ranking] if ranking else None)
    assert query["limit"] == ["100"]
    assert query["nsfw"] == [nsfw]
    assert {"genres", "media_type", "status", "mean", "nsfw", "start_date"} <= set(
        query["fields"][0].split(",")
    )
    assert not {"genre", "country", "score"} & query.keys()


def test_mal_manga_normalizes_formats_and_inferred_origin():
    kinds = ["manga", "manhwa", "manhua", "light_novel", "one_shot"]
    payload = {
        "data": [
            {
                "node": {
                    "id": idx + 1,
                    "title": kind,
                    "media_type": kind,
                    "mean": 8.25,
                    "genres": [{"id": 1, "name": "Action"}],
                    "nsfw": "white",
                    "status": "finished",
                    "start_date": "2023-06-01",
                }
            }
            for idx, kind in enumerate(kinds)
        ]
    }
    mapper = KEYED["mal_manga"].mapper
    records = mapper(payload, NOW, {})
    fields = [record.metadata["provider_record"]["source_fields"] for record in records]
    assert [(f["media_type"], f["format"], f.get("country")) for f in fields] == [
        ("manga", "manga", "JP"),
        ("manhwa", "manhwa", "KR"),
        ("manhua", "manhua", "CN"),
        ("light_novel", "novel", "JP"),
        ("manga", "one_shot", "JP"),
    ]
    assert fields[1]["rating"] == 82.5
    assert fields[1]["external_ids"] == {"mal": "2"}
    assert fields[1]["release_status"] == "completed"
    assert fields[1]["release_year"] == 2023
    assert mapper(
        payload,
        NOW,
        {
            "media_countries": ["KR"],
            "media_formats": ["manhwa"],
            "media_genres": ["action"],
            "release_year": 2023,
            "min_rating": 82,
        },
    ) == [records[1]]
    assert mapper(payload, NOW, {"min_rating": 83}) == []


def test_mal_anime_season_rating_and_explicit_metadata():
    payload = {
        "data": [
            {
                "node": {
                    "id": 1,
                    "title": "Example",
                    "media_type": "tv",
                    "rating": "pg_13",
                    "nsfw": "white",
                    "status": "currently_airing",
                    "start_season": {"season": "fall", "year": 2026},
                }
            },
            {
                "node": {
                    "id": 2,
                    "title": "Adult",
                    "media_type": "ova",
                    "rating": "rx",
                    "nsfw": "black",
                }
            },
        ]
    }
    mapper = KEYED["mal_anime"].mapper
    records = mapper(payload, NOW, {})
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert (fields["media_type"], fields["format"], fields["release_status"]) == (
        "anime",
        "tv",
        "airing",
    )
    assert (fields["content_rating"], fields["release_season"], fields["release_year"]) == (
        "pg_13",
        "fall",
        2026,
    )
    assert (
        mapper(payload, NOW, {"exclude_adult": False, "content_ratings": ["explicit"]})[
            0
        ].provider_id
        == "mal_anime:2"
    )


def test_google_books_single_query_language_and_bounded_page():
    request = KEYED["google_books"].build(
        "private-key",
        {
            "google_books_subject": "Science Fiction",
            "google_books_query": "inauthor:Le Guin",
            "media_languages": ["en"],
            "google_books_print_type": "books",
            "google_books_filter": "ebooks",
            "max_items": 100,
        },
    )
    url = urlsplit(request.url)
    assert request.method == "GET"
    assert (url.hostname, url.path) == ("www.googleapis.com", "/books/v1/volumes")
    assert parse_qs(url.query) == {
        "q": ['inauthor:Le Guin subject:"Science Fiction"'],
        "key": ["private-key"],
        "langRestrict": ["en"],
        "printType": ["books"],
        "filter": ["ebooks"],
        "maxResults": ["40"],
    }
    assert "private-key" not in repr(request)
    assert "langRestrict" not in parse_qs(
        urlsplit(
            KEYED["google_books"]
            .build(
                "private-key",
                {"google_books_subject": "Fantasy", "media_languages": ["en", "fr"]},
            )
            .url
        ).query
    )


def test_google_books_normalizes_isbns_cover_and_filters():
    payload = {
        "items": [
            {
                "id": "book-1",
                "volumeInfo": {
                    "title": "Example",
                    "publishedDate": "2024-09",
                    "language": "en",
                    "pageCount": 320,
                    "averageRating": 4.5,
                    "categories": ["Fiction / Science Fiction / Adventure"],
                    "industryIdentifiers": [
                        {"type": "ISBN_10", "identifier": "0143111590"},
                        {"type": "ISBN_13", "identifier": "9780143111597"},
                        {"type": "OTHER", "identifier": "abc"},
                    ],
                    "imageLinks": {
                        "large": "https://127.0.0.1/unsafe",
                        "thumbnail": "http://books.google.com/cover",
                    },
                },
                "saleInfo": {"isEbook": True},
                "accessInfo": {"country": "US"},
            }
        ]
    }
    mapper = KEYED["google_books"].mapper
    records = mapper(
        payload,
        NOW,
        {
            "media_countries": ["US"],
            "media_genres": ["sci_fi"],
            "media_languages": ["en"],
            "media_formats": ["ebook"],
            "release_year": 2024,
            "min_rating": 90,
        },
    )
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert records[0].provider_id == "google_books:book-1"
    assert fields["external_ids"] == {
        "google_books": "book-1",
        "isbn10": "0143111590",
        "isbn13": "9780143111597",
    }
    assert fields["rating"] == 90.0
    assert fields["page_count"] == 320
    assert fields["genres"] == ["sci_fi", "adventure"]
    assert fields["image_url"] == "https://books.google.com/cover"
    assert mapper(payload, NOW, {"media_languages": ["fr"]}) == []
    assert mapper({"totalItems": 0}, NOW, {}) == []


@pytest.mark.parametrize(
    ("provider", "configuration", "placement"),
    [
        ("mal_anime", {}, "X-MAL-CLIENT-ID"),
        ("mal_manga", {}, "X-MAL-CLIENT-ID"),
        ("google_books", {"google_books_query": "intitle:Dune"}, "query:key"),
    ],
)
def test_media_provider_key_validation_and_filter_fingerprint(provider, configuration, placement):
    assert KEYED[provider].placement == placement
    with pytest.raises(ProviderPayloadError, match="key_invalid"):
        KEYED[provider].build("", configuration)
    assert native_fingerprint(provider, configuration) != native_fingerprint(
        provider, {**configuration, "min_rating": 90}
    )


def test_media_targets_are_validated_and_filters_do_not_change_scope():
    with pytest.raises(ValueError, match="season"):
        validate_options("mal_anime", {"mal_mode": "season"})
    with pytest.raises(ValueError, match="ranking"):
        validate_options("mal_manga", {"mal_ranking_type": "airing"})
    with pytest.raises(ValueError, match="ranking"):
        validate_options("mal_manga", {"mal_ranking_type": "all"})
    validate_options("mal_anime", {"mal_ranking_type": "all"})
    with pytest.raises(ValueError, match="query or subject"):
        validate_options("google_books", {})
    assert ConnectorConfig().google_books_filter is None
    assert ConnectorConfig().mal_ranking_type is None
    targets = ("google_books_query", "google_books_subject")
    config = {"google_books_subject": "Fantasy"}
    assert _scope_values("google_books", targets, {**config, "min_rating": 80}) == config


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("mal_anime", {"data": [{}] * 101}),
        ("mal_manga", {"data": [{}] * 101}),
        ("google_books", {"items": [{}] * 41}),
    ],
)
def test_unexpected_oversized_page_fails_before_any_record_is_accepted(provider, body):
    with pytest.raises(ProviderPayloadError) as exc:
        KEYED[provider].mapper(body, NOW, {})
    assert exc.value.kind == "incomplete"


@pytest.mark.parametrize(("provider", "ranking"), [("mal_anime", "all"), ("mal_manga", "manga")])
def test_mal_explicit_and_implicit_ranking_defaults_share_fingerprint_and_catalog(provider, ranking):
    cfg = {"mal_ranking_type": ranking}
    assert native_fingerprint(provider, {}) == native_fingerprint(provider, cfg)
    entry = get_catalog_entry(provider)
    assert entry and entry.example_config and entry.example_config["mal_ranking_type"] == ranking
