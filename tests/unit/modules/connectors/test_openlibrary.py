"""Offline contract tests for the bounded Open Library collector."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from modules.connectors.provider_options import validate_options
from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers.openlibrary import (
    MAX_BODY,
    collect_openlibrary,
    map_openlibrary,
    openlibrary_request,
)
from modules.connectors.providers.rest import Fetched


def test_openlibrary_modes_build_bounded_official_urls_and_language_codes() -> None:
    """Scope modes use the fixed host, bounded page size, and ISO 639-2/B search codes."""
    assert "subjects/science_fiction.json?limit=100" in openlibrary_request(
        {"openlibrary_subject": "Science Fiction"}
    )
    search = openlibrary_request(
        {
            "openlibrary_mode": "search",
            "openlibrary_query": "Dune",
            "media_languages": ["fr"],
            "ebooks_only": True,
        }
    )
    assert "language=fre" in search and "ebook_access=public" in search
    assert "language=tha" in openlibrary_request(
        {"openlibrary_mode": "search", "openlibrary_query": "Thai", "media_languages": ["th"]}
    )
    assert "recentchanges.json?limit=10&offset=0" in openlibrary_request(
        {"openlibrary_mode": "recent"}, "2"
    )
    assert "offset=0" in openlibrary_request({"openlibrary_mode": "recent"}, "10000")


def test_openlibrary_registration_uses_supported_cc0_eligibility() -> None:
    """Open Library's CC0 terms map to the catalog's supported open-use category."""
    spec = get_provider_spec("openlibrary_books")
    assert spec is not None and spec.eligibility == "open"


def test_openlibrary_maps_bounded_fields_and_filters_rating() -> None:
    """Search mapping normalizes Open Library identifiers and returns only allowlisted media fields."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    payload = {
        "docs": [
            {
                "key": "/works/OL123W",
                "title": "Book",
                "isbn": ["9781234567890"],
                "first_publish_year": 1965,
                "first_publish_date": "1965-06-01",
                "language": ["eng"],
                "ratings_average": 4.2,
                "subject": ["Science Fiction"],
                "cover_i": 12,
            }
        ]
    }
    page = map_openlibrary(json.dumps(payload).encode(), mode="search", config={}, collected_at=now)
    record = page.records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "openlibrary:OL123W"
    assert fields["external_ids"] == {"openlibrary": "OL123W", "isbn13": "9781234567890"}
    assert fields["release_date"] == "1965-06-01" and fields["release_year"] == 1965
    assert fields["language"] == "en" and fields["rating"] == 84
    assert fields["genres"] == ["sci_fi"] and fields["image_url"].endswith("/12-L.jpg")
    assert "image_url" in fields and "artwork_url" not in fields


@pytest.mark.parametrize("year, expected", [(1799, None), (1800, 1800)])
def test_openlibrary_omits_years_below_shared_media_bound(year: int, expected: int | None) -> None:
    """Publication years below the shared 1800 minimum are omitted."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    body = json.dumps(
        {"docs": [{"key": "/works/OL42W", "title": "Old book", "first_publish_year": year}]}
    ).encode()
    fields = (
        map_openlibrary(body, mode="search", config={}, collected_at=now)
        .records[0]
        .metadata["provider_record"]["source_fields"]
    )
    assert fields.get("release_year") == expected


def test_openlibrary_rejects_noop_or_multilingual_scopes() -> None:
    """Each query mode accepts only filters it can apply at the provider or record layer."""
    with pytest.raises(ValueError, match="does not accept query or language"):
        validate_options(
            "openlibrary_books", {"openlibrary_subject": "fiction", "media_languages": ["en"]}
        )
    with pytest.raises(ValueError, match="one search language"):
        validate_options(
            "openlibrary_books",
            {
                "openlibrary_mode": "search",
                "openlibrary_query": "novel",
                "media_languages": ["en", "fr"],
            },
        )
    with pytest.raises(ValueError, match="does not accept subject, query, language, or ebook"):
        validate_options("openlibrary_books", {"openlibrary_mode": "recent", "ebooks_only": True})


def test_openlibrary_rejects_oversized_response() -> None:
    """The parser independently enforces its transport response byte cap."""
    with pytest.raises(ValueError, match="openlibrary_response_invalid"):
        map_openlibrary(
            b" " * (MAX_BODY + 1), mode="search", config={}, collected_at=datetime.now(UTC)
        )


def test_openlibrary_recent_cannot_emit_unenriched_change_rows() -> None:
    """Recent change keys cannot be mistaken for complete normalized media documents."""
    with pytest.raises(ValueError, match="openlibrary_recent_requires_work_details"):
        map_openlibrary(b"[]", mode="recent", config={}, collected_at=datetime.now(UTC))


def test_openlibrary_recent_fetches_work_detail_before_emitting_records() -> None:
    """Recent mode turns work change keys into complete normalized work records before checkpointing."""
    calls: list[str] = []

    async def fetch(url: str, **kwargs: object) -> Fetched:
        calls.append(url)
        body = (
            b'[{"changes":[{"key":"/works/OL123W"}]}]'
            if "recentchanges" in url
            else b'{"key":"/works/OL123W","title":"Recent book","first_publish_year":2000}'
        )
        return Fetched(body)

    page = asyncio.run(
        collect_openlibrary(
            fetch,
            config={"openlibrary_mode": "recent"},
            collected_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    assert len(calls) == 2 and calls[1].endswith("/works/OL123W.json")
    assert page.records[0].metadata["title"] == "Recent book" and page.cursor == "0"


def test_openlibrary_recent_collects_every_work_key_and_overlaps_offsets() -> None:
    """Every work key in each recent changeset is fetched; subsequent pages overlap for feed shifts."""
    calls: list[str] = []

    async def fetch(url: str, **kwargs: object) -> Fetched:
        calls.append(url)
        if "recentchanges" in url:
            changes = [
                {"changes": [{"key": "/works/OL5W"}, {"key": "/works/OL6W"}]} for _ in range(10)
            ]
            return Fetched(json.dumps(changes).encode())
        key = url.split("/works/", 1)[1].split(".json", 1)[0]
        return Fetched(json.dumps({"key": f"/works/{key}", "title": key}).encode())

    page = asyncio.run(
        collect_openlibrary(
            fetch,
            config={"openlibrary_mode": "recent"},
            collected_at=datetime(2026, 1, 1, tzinfo=UTC),
            cursor="10",
        )
    )
    assert calls[0].endswith("offset=8&kind=update&type=%2Ftype%2Fwork")
    assert len(page.records) == 2 and page.cursor == "18"
    assert openlibrary_request({"openlibrary_mode": "recent"}, page.cursor).endswith(
        "offset=16&kind=update&type=%2Ftype%2Fwork"
    )


def test_openlibrary_recent_fanout_cap_fails_without_cursor_result() -> None:
    """An oversized multi-work changeset fails closed before any work detail fetch or cursor advance."""
    calls: list[str] = []

    async def fetch(url: str, **kwargs: object) -> Fetched:
        calls.append(url)
        return Fetched(
            json.dumps([{"changes": [{"key": f"/works/OL{n}W"} for n in range(1, 12)]}]).encode()
        )

    with pytest.raises(ValueError, match="openlibrary_recent_fanout_exceeded"):
        asyncio.run(
            collect_openlibrary(
                fetch,
                config={"openlibrary_mode": "recent"},
                collected_at=datetime(2026, 1, 1, tzinfo=UTC),
                cursor="10",
            )
        )
    assert len(calls) == 1


def test_openlibrary_pushes_down_release_year_and_single_genre() -> None:
    from urllib.parse import parse_qs, urlsplit

    search = parse_qs(
        urlsplit(
            openlibrary_request(
                {
                    "openlibrary_mode": "search",
                    "openlibrary_query": "dune",
                    "release_year": 1965,
                    "media_genres": ["sci_fi"],
                }
            )
        ).query
    )
    assert search["first_publish_year"] == ["1965"] and search["subject"] == ["science_fiction"]
    several = openlibrary_request(
        {
            "openlibrary_mode": "search",
            "openlibrary_query": "dune",
            "media_genres": ["sci_fi", "drama"],
        }
    )
    assert "subject=" not in several
    subject = openlibrary_request(
        {"openlibrary_mode": "subject", "openlibrary_subject": "history", "release_year": 1999}
    )
    assert "published_in=1999-1999" in subject
