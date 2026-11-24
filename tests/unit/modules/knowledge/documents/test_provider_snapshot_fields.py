"""Characterization tests for ``ProviderRecordMetadata.validate_provider_snapshot_fields``.

The validator is the trust boundary for provider-controlled metadata. These tests pin each
field-class rule (allowlist, bounded strings/lists, booleans, integers, links, timestamps) and
the exact error messages so the table-driven refactor stays behaviour-identical.
"""

import pytest
from pydantic import ValidationError

from modules.knowledge.documents.schemas import (
    BUILTIN_FEED_PROVIDER_IDS,
    MEASUREMENT_PROVIDER_IDS,
    PROVIDER_IDS,
    ProviderRecordMetadata,
)


def _meta(provider: str, fields: dict, **over):  # type: ignore[no-untyped-def,type-arg]
    base = {
        "provider": provider,
        "identity": "id-1",
        "timestamp_basis": "collection",
        "coverage": "returned_snapshot",
        "content_truncated": False,
        "source_fields": fields,
    }
    base.update(over)
    return ProviderRecordMetadata(**base)


def _rejects(provider: str, fields: dict, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValidationError, match=message):
        _meta(provider, fields)


def test_every_non_special_provider_accepts_empty_fields_and_unknown_keys_are_rejected() -> None:
    plain = [p for p in PROVIDER_IDS if p != "telegram" and p not in MEASUREMENT_PROVIDER_IDS]
    for provider in plain:
        assert _meta(provider, {}).source_fields == {}
        _rejects(provider, {"zzz_unknown": "x"}, "unsupported source fields")


def test_telegram_and_world_data_pairing_rules() -> None:
    _rejects("telegram", {}, "Telegram detail must match provider")
    with pytest.raises(ValidationError, match="World-data measurement must match provider"):
        _meta("fred", {})


def test_feed_providers_share_the_feed_allowlist() -> None:
    provider = BUILTIN_FEED_PROVIDER_IDS[0]
    ok = {
        "title": "t",
        "summary": "s",
        "guid": "g",
        "publisher": "p",
        "published_at": "2026-01-01T00:00:00Z",
    }
    assert _meta(provider, ok).source_fields == ok
    assert "form" in _meta("sec_edgar", {"form": "10-K", "cik": "123"}).source_fields
    _rejects(provider, {"form": "10-K"}, "unsupported source fields")


def test_bounded_string_and_length_limits() -> None:
    assert _meta("youtube", {"title": "x" * 500}).source_fields["title"] == "x" * 500
    _rejects("youtube", {"title": "x" * 501}, "Provider title must be a bounded string")
    _rejects("youtube", {"title": 5}, "Provider title must be a bounded string")
    _rejects("youtube", {"author": "x" * 1001}, "Provider author must be a bounded string")


def test_string_lists_are_bounded() -> None:
    assert _meta("youtube", {"tags": ["a", "b"]}).source_fields["tags"] == ["a", "b"]
    _rejects("youtube", {"tags": "a"}, "Provider tags must be a bounded string list")
    _rejects("youtube", {"tags": ["a"] * 101}, "Provider tags must be a bounded string list")
    _rejects("youtube", {"tags": ["x" * 256]}, "Provider tags must be a bounded string list")
    _rejects("youtube", {"tags": [1]}, "Provider tags must be a bounded string list")
    assert _meta("google_calendar", {"recurrence": ["x" * 1000]}).source_fields["recurrence"]
    _rejects(
        "google_calendar",
        {"recurrence": ["x" * 1001]},
        "Provider recurrence must be a bounded string list",
    )


def test_boolean_and_integer_fields() -> None:
    assert (
        _meta("github_releases", {"draft": True, "prerelease": False}).source_fields["draft"]
        is True
    )
    _rejects("github_releases", {"draft": 1}, "Provider draft must be boolean")
    assert _meta("hn_top", {"time": 5, "id": 0, "deleted": False}).source_fields["time"] == 5
    _rejects("hn_top", {"time": -1}, "Provider time must be a non-negative integer")
    _rejects("hn_top", {"id": True}, "Provider id must be a non-negative integer")
    _rejects("hn_top", {"id": "5"}, "Provider id must be a non-negative integer")
    _rejects(
        "openalex",
        {"cited_by_count": 1.5},
        "Provider cited_by_count must be a non-negative integer",
    )


def test_https_links_without_credentials() -> None:
    assert _meta("bluesky", {"canonical_url": "https://example.com/p"}).source_fields
    for bad in (
        "http://example.com",
        "https://u:p@example.com",
        "https:///nohost",
        5,
        "https://e.com/" + "a" * 2048,
    ):
        _rejects(
            "bluesky",
            {"canonical_url": bad},
            "Provider canonical_url must be an HTTPS link without credentials",
        )
    _rejects(
        "hn_top", {"url": "ftp://x.test"}, "Provider url must be an HTTPS link without credentials"
    )
    _rejects(
        "google_drive",
        {"web_view_link": "http://x.test"},
        "Provider web_view_link must be an HTTPS link",
    )
    _rejects("google_calendar", {"html_link": None}, "Provider html_link must be an HTTPS link")


def test_release_url_must_target_github_com() -> None:
    assert _meta("github", {"html_url": "https://github.com/o/r"}).source_fields
    for bad in ("https://evil.com/o/r", "http://github.com/o/r", "https://u@github.com/o/r"):
        _rejects("github", {"html_url": bad}, "Provider release URL must target github.com")
    _rejects("github", {"html_url": 5}, "Provider release URL must use HTTPS")
    _rejects(
        "github",
        {"html_url": "https://github.com/" + "a" * 2048},
        "Provider release URL must use HTTPS",
    )


def test_iso_timestamps_require_offsets() -> None:
    assert _meta("bluesky", {"created_at": "2026-01-01T00:00:00Z"}).source_fields
    assert _meta("bluesky", {"created_at": "2026-01-01T00:00:00+02:00"}).source_fields
    _rejects("bluesky", {"created_at": "yesterday"}, "Provider created_at must be an ISO timestamp")
    _rejects(
        "bluesky",
        {"created_at": "2026-01-01T00:00:00"},
        "Provider created_at must include a timezone",
    )
    _rejects(
        "youtube",
        {"provider_updated_at": "nope"},
        "Provider provider_updated_at must be an ISO timestamp",
    )
    _rejects(
        "google_drive",
        {"modified_time": "2026-01-01"},
        "Provider modified_time must include a timezone",
    )


def test_calendar_start_end_accept_dates_and_offset_timestamps() -> None:
    assert _meta("google_calendar", {"start": "2026-01-01", "end": "2026-01-02"}).source_fields
    assert _meta("google_calendar", {"start": "2026-01-01T10:00:00Z"}).source_fields
    _rejects(
        "google_calendar",
        {"start": "2026-01-01T10:00:00"},
        "Provider start timestamp must include a timezone",
    )
    _rejects("google_calendar", {"end": "garbage"}, "Provider end must be an ISO date or timestamp")


def test_slack_optional_ids_may_be_null_but_others_may_not() -> None:
    assert _meta("slack", {"thread_ts": None, "user_id": None, "channel_id": "C1"}).source_fields
    _rejects("slack", {"channel_id": None}, "Provider channel_id must be a bounded string")
    _rejects("slack", {"message_ts": None}, "Provider message_ts must be a bounded string")


def test_media_providers_and_media_field_validation() -> None:
    from modules.knowledge.documents.public import MEDIA_PROVIDER_IDS

    assert len(MEDIA_PROVIDER_IDS) == 27
    assert "youtube" not in MEDIA_PROVIDER_IDS
    for provider in MEDIA_PROVIDER_IDS:
        ok = {
            "title": "Dandadan",
            "media_type": "anime",
            "artwork_aspect": "poster",
            "country": "JP",
            "language": "ja",
            "genres": ["action", "sci_fi"],
            "format": "tv",
            "content_rating": "pg_13",
            "release_status": "airing",
            "release_date": "2026-10-01",
            "release_year": 2026,
            "release_season": "Fall 2026",
            "rating": 87.5,
            "creator": "Science SARU",
            "season": "2",
            "episode": "1",
            "duration_seconds": 1440,
            "external_ids": {"anilist": "171018", "mal": "57334"},
            "trailer_url": "https://www.youtube.com/watch?v=abc",
            "attribution": "Data via AniList",
        }
        assert _meta(provider, ok).source_fields["rating"] == 87.5

    _rejects("anilist_anime", {"genres": ["Action"]}, "lowercase slug list")
    _rejects("anilist_anime", {"genres": ["a"] * 13}, "lowercase slug list")
    _rejects("anilist_anime", {"genres": ["x" * 49]}, "lowercase slug list")
    _rejects("anilist_anime", {"release_year": 1799}, "bounded integer")
    _rejects("anilist_anime", {"release_year": True}, "bounded integer")
    _rejects("anilist_anime", {"duration_seconds": 604801}, "bounded integer")
    _rejects("openlibrary_books", {"page_count": 50001}, "bounded integer")
    _rejects("tmdb_movies", {"rating": True}, "finite number between 0 and 100")
    _rejects("tmdb_movies", {"rating": float("nan")}, "finite number between 0 and 100")
    _rejects("tmdb_movies", {"rating": 101.0}, "finite number between 0 and 100")
    _rejects("tmdb_movies", {"external_ids": {"bad_key": "1"}}, "bounded identifier map")
    _rejects("tmdb_movies", {"external_ids": {"tmdb": " "}}, "bounded identifier map")
    _rejects(
        "tmdb_movies", {"trailer_url": "http://example.com/t"}, "HTTPS link without credentials"
    )
    _rejects("tmdb_movies", {"country": "USA"}, "bounded string")
    _rejects("tmdb_movies", {"country": "us"}, "ISO 3166-1 alpha-2")
    _rejects("tmdb_movies", {"media_type": "invalid"}, "media_type is unsupported")
    _rejects("tmdb_movies", {"artwork_aspect": "wide"}, "artwork_aspect is unsupported")
    _rejects("tmdb_movies", {"release_status": "unknown"}, "release_status is unsupported")
    # Existing non-media providers preserve 64-char language limit (e.g. "English").
    assert (
        _meta("gdelt_economy", {"language": "English", "sourcecountry": "US"}).source_fields[
            "language"
        ]
        == "English"
    )
    assert (
        _meta("tmdb_movies", {"artwork_aspect": "landscape"}).source_fields["artwork_aspect"]
        == "landscape"
    )


def test_media_release_dates_accept_partial_dates_and_reject_invalid_calendar_values() -> None:
    for value in ("2026", "2026-02", "2024-02-29", "2026-01-01T12:30:00Z"):
        assert (
            _meta("anilist_anime", {"release_date": value}).source_fields["release_date"] == value
        )
    for value in (
        "2026-99-99",
        "2026-02-29",
        "2026-13",
        "2026-01-01T25:00:00Z",
        "2026-01-01T12:00:00",
    ):
        with pytest.raises(ValueError):
            _meta("anilist_anime", {"release_date": value})


def test_bounded_float_fields_for_emsc_and_hn_score() -> None:
    assert (
        _meta("emsc_quakes", {"magnitude": 5.1, "depth_km": 10.0}).source_fields["magnitude"] == 5.1
    )
    assert _meta("emsc_quakes", {"magnitude": 10.0, "depth_km": -10}).source_fields
    assert _meta("hn_top", {"score": 321}).source_fields["score"] == 321
    for bad in (float("nan"), float("inf"), 11.0, -2.5, "5.1", True):
        _rejects("emsc_quakes", {"magnitude": bad}, "Provider magnitude must be")
    for bad in (801.0, -10.5, "3", False):
        _rejects("emsc_quakes", {"depth_km": bad}, "Provider depth_km must be")
    _rejects("hn_top", {"score": -1}, "Provider score must be a non-negative integer")
    _rejects("hn_top", {"score": True}, "Provider score must be a non-negative integer")
