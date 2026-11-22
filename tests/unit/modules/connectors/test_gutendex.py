"""Offline contract tests for the bounded Gutendex connector."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.provider_options import build_url, native_fingerprint, validate_options
from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers.gutendex import (
    MAX_BODY,
    collect_gutendex,
    gutendex_request,
    map_gutendex,
)
from modules.connectors.providers.rest import Fetched

FIXTURE = Path("tests/fixtures/connectors/gutendex-page.json")
FIXTURE_BODY = FIXTURE.read_bytes()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_gutendex_pushes_supported_scope_and_uses_official_fixed_host() -> None:
    """Every supported provider filter is pushed into a bounded official API request."""
    config = {
        "media_languages": ["en", "vi"],
        "gutendex_topic": "science fiction",
        "gutendex_search": "time travel",
        "gutendex_copyright": True,
        "gutendex_mime_type": "text/plain",
        "gutendex_author_year_start": 1800,
        "gutendex_author_year_end": 1900,
    }
    url = gutendex_request(config)
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "gutendex.com", "/books/")
    assert params == {
        "page": ["1"],
        "page_size": ["32"],
        "languages": ["en,vi"],
        "topic": ["science fiction"],
        "search": ["time travel"],
        "copyright": ["true"],
        "mime_type": ["text/plain"],
        "author_year_start": ["1800"],
        "author_year_end": ["1900"],
    }
    assert build_url(
        "gutendex_books", config, cursor="https://gutendex.com/books/?page=2", now=NOW
    ) == url.replace("page=1", "page=2")
    assert native_fingerprint("gutendex_books", config) != native_fingerprint(
        "gutendex_books", {**config, "gutendex_topic": "history"}
    )


def test_gutendex_maps_fixture_and_normalizes_genres() -> None:
    """Fixture results map to allowlisted media metadata and normalized genre IDs."""
    page = map_gutendex(
        FIXTURE.read_bytes(), config={"media_genres": ["sci_fi"]}, collected_at=NOW, page_number=1
    )
    assert page.cursor and page.cursor.startswith("https://gutendex.com/books/?page=2")
    assert len(page.records) == 1
    record = page.records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "gutendex:1"
    assert fields["genres"] == ["sci_fi"]
    assert fields["language"] == "en" and fields["creator"] == "Example Author"
    assert fields["canonical_url"] == "https://www.gutenberg.org/ebooks/1"


def test_gutendex_genre_filter_drops_books_without_a_matching_known_genre() -> None:
    """A selected genre cannot match books whose provider subjects normalize to no genre."""
    payload = {
        "results": [
            {
                "id": 2,
                "title": "Unclassified",
                "authors": [],
                "subjects": ["Unclassified topic"],
                "bookshelves": [],
                "languages": ["en"],
            }
        ],
        "next": None,
    }
    page = map_gutendex(
        json.dumps(payload).encode(),
        config={"media_genres": ["sci_fi"]},
        collected_at=NOW,
        page_number=1,
    )
    assert page.records == ()


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/books/?page=2",
        "https://gutendex.com.evil.example/books/?page=2",
        "https://gutendex.com/other/?page=2",
        "https://gutendex.com/books/?page=10001",
        "https://gutendex.com/books/?page=1",
        "https://gutendex.com/books/?page=3",
    ],
)
def test_gutendex_rejects_untrusted_or_unbounded_next_links(link: str) -> None:
    """Only advancing pagination within the official host and bounded endpoint is persisted."""
    payload = {"results": [], "next": link}
    with pytest.raises(ValueError, match="gutendex_pagination_invalid"):
        map_gutendex(json.dumps(payload).encode(), config={}, collected_at=NOW, page_number=1)


def test_gutendex_page_is_bounded_and_invalid_rows_do_not_advance() -> None:
    """Oversized bodies and malformed records fail before a continuation can be accepted."""
    with pytest.raises(ValueError, match="gutendex_response_invalid"):
        map_gutendex(b" " * (MAX_BODY + 1), config={}, collected_at=NOW, page_number=1)
    with pytest.raises(ValueError, match="gutendex_book_invalid"):
        map_gutendex(
            b'{"results":[{"id":1}],"next":"https://gutendex.com/books/?page=2"}',
            config={},
            collected_at=NOW,
            page_number=1,
        )


def test_gutendex_transport_uses_sendgate_fetcher_and_provider_registration() -> None:
    """The adapter uses its injected bounded transport and advertises conservative quota."""
    calls: list[tuple[str, int]] = []

    async def fetch(url: str, **kwargs: object) -> Fetched:
        calls.append((url, int(kwargs["max_bytes"])))
        return Fetched(FIXTURE_BODY)

    page = __import__("asyncio").run(collect_gutendex(fetch, config={}, collected_at=NOW))
    spec = get_provider_spec("gutendex_books")
    assert len(page.records) == 1 and calls[0][1] == MAX_BODY
    assert (
        spec is not None and spec.default_interval_minutes == 1440 and spec.eligibility == "review"
    )
    validate_options(
        "gutendex_books", {"gutendex_author_year_start": 1800, "gutendex_author_year_end": 1900}
    )
    with pytest.raises(ValueError, match="must not exceed"):
        validate_options(
            "gutendex_books", {"gutendex_author_year_start": 1900, "gutendex_author_year_end": 1800}
        )
