from uuid import UUID

import pytest
from pydantic import ValidationError

from modules.connectors.backends import (
    FIXED_SCOPE_PROVIDERS,
    GATED_EXECUTOR_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
    PROVIDER_SOURCE_TYPES,
)
from modules.connectors.builtins import BuiltinEnableRequest
from modules.connectors.provider_options import validate_options
from modules.connectors.service.schemas import ConnectorConfig
from modules.connectors.service.scope import OPTIONAL_SCOPE_FIELDS


@pytest.mark.parametrize(
    ("provider", "source_type"),
    (
        ("ann_news", "rss"),
        ("mangaupdates_releases", "api"),
        ("mangadex_releases", "api"),
        ("apple_books_rss", "api"),
        ("podcast_rss", "rss"),
        ("wikidata_movies", "api"),
        ("itunes_movies", "api"),
        ("letterboxd_rss", "rss"),
    ),
)
def test_media_provider_scopes_are_registered(provider: str, source_type: str) -> None:
    assert PROVIDER_SOURCE_TYPES[provider] == source_type
    assert provider in GATED_EXECUTOR_PROVIDERS
    assert provider not in FIXED_SCOPE_PROVIDERS
    assert PROVIDER_SCOPE_FIELDS[provider]
    assert provider in OPTIONAL_SCOPE_FIELDS


def test_media_provider_config_accepts_bounded_values() -> None:
    manga_id = UUID("5e7d5a22-3d67-49c5-89d3-9cd9a49a91de")
    config = ConnectorConfig.model_validate(
        {
            "ann_mode": "encyclopedia",
            "ann_media_type": "manga",
            "ann_category": "review",
            "ann_edition": "us",
            "manga_ids": [str(manga_id)],
            "mangadex_content_ratings": ["safe", "suggestive"],
            "mangadex_statuses": ["ongoing"],
            "kind": "books",
            "chart": "top-free",
            "country": "US",
            "letterboxd_username": "reader_01",
            "genre_qids": ["Q11424", "Q157394"],
            "updated_at_since": "2026-11-16T12:00:00Z",
        }
    )

    assert config.manga_ids == (manga_id,)
    assert config.mangadex_content_ratings == ("safe", "suggestive")
    assert config.chart == "top-free"
    assert config.country == "US"


@pytest.mark.parametrize(
    "payload",
    (
        {"apple_books_chart": "../private"},
        {"letterboxd_username": "../profile"},
        {"updated_at_since": "not-a-date"},
        {"itunes_podcast_id": True},
    ),
)
def test_media_provider_config_rejects_invalid_values(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConnectorConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("provider", "payload"),
    (
        ("mangaupdates_releases", {"series_ids": [41, 41]}),
        ("mangaupdates_releases", {"mangaupdates_types": ["Manga", "Manga"]}),
        ("mangaupdates_releases", {"exclude_genre": ["action", "action"]}),
        ("mangadex_releases", {"manga_ids": ["5e7d5a22-3d67-49c5-89d3-9cd9a49a91de"] * 2}),
        ("mangadex_releases", {"original_languages": ["ja", "ja"]}),
        ("mangadex_releases", {"available_translated_languages": ["en", "en"]}),
        ("mangadex_releases", {"mangadex_content_ratings": ["safe", "safe"]}),
        ("mangadex_releases", {"mangadex_statuses": ["ongoing", "ongoing"]}),
        ("mangadex_releases", {"publication_demographic": ["seinen", "seinen"]}),
        ("mangadex_releases", {"excluded_tags": ["5e7d5a22-3d67-49c5-89d3-9cd9a49a91de"] * 2}),
        ("wikidata_movies", {"genre_qids": ["Q11424", "Q11424"]}),
    ),
)
def test_media_provider_options_reject_duplicate_selectors(
    provider: str, payload: dict[str, object]
) -> None:
    config = ConnectorConfig.model_validate(payload).model_dump(mode="json", exclude_none=True)

    with pytest.raises(ValueError, match="unique"):
        validate_options(provider, config)


def test_ann_builtin_accepts_atom_scope_options() -> None:
    request = BuiltinEnableRequest(ann_mode="news", ann_category="review", ann_edition="us")

    assert request.ann_mode == "news"
    assert request.ann_category == "review"
    assert request.ann_edition == "us"


@pytest.mark.parametrize(
    ("provider", "required"),
    (
        ("apple_books_rss", {"country", "kind", "chart"}),
        ("itunes_movies", {"search_terms", "country"}),
        ("letterboxd_rss", {"letterboxd_username"}),
    ),
)
def test_scoped_media_providers_require_their_identity_fields(
    provider: str, required: set[str]
) -> None:
    assert required <= PROVIDER_SCOPE_FIELDS[provider] - OPTIONAL_SCOPE_FIELDS[provider]
