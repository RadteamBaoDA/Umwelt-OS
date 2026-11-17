"""Bounded public domain book search using the official Gutendex API."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers._media_common import (
    MediaRatePacer,
    normalize_genres,
    normalize_language,
)
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

BASE = "https://gutendex.com/books/"
MAX_BODY = 2 * 1024 * 1024
PAGE_SIZE = 32
MAX_PAGE = 10_000
PACER = MediaRatePacer()


@dataclass(frozen=True)
class GutendexPage:
    """Records accepted from one API page and its validated same-host continuation."""

    records: tuple[IngestionRecord, ...]
    cursor: str | None


def _page_number(cursor: str | None) -> int:
    """Extract and bound Gutendex's page number from the persisted next URL."""
    if not cursor:
        return 1
    parts = urlsplit(cursor)
    if (
        len(cursor) > 2048 or parts.scheme != "https" or parts.hostname != "gutendex.com"
        or parts.port or parts.username or parts.password or parts.fragment or parts.path != "/books/"
    ):
        raise ValueError("gutendex_pagination_invalid")
    query = parse_qs(parts.query, strict_parsing=True)
    allowed = {"page", "page_size", "languages", "topic", "search", "copyright", "mime_type", "author_year_start", "author_year_end"}
    if not set(query) <= allowed or any(len(values) != 1 for values in query.values()) or len(query.get("page", [])) != 1 or not query["page"][0].isdigit():
        raise ValueError("gutendex_pagination_invalid")
    page = int(query["page"][0])
    if not 1 < page <= MAX_PAGE:
        raise ValueError("gutendex_pagination_invalid")
    return page


def gutendex_request(config: Mapping[str, object], cursor: str | None = None) -> str:
    """Build a fixed-host request with Gutendex-supported filters and bounded pagination."""
    page = _page_number(cursor)
    params: dict[str, object] = {"page": page, "page_size": PAGE_SIZE}
    for key in ("languages", "topic", "search", "copyright", "mime_type", "author_year_start", "author_year_end"):
        value: object
        if key == "languages":
            languages = config.get("media_languages")
            if languages is not None and (
                not isinstance(languages, (list, tuple))
                or any(not isinstance(language, str) for language in languages)
            ):
                raise ValueError("gutendex_languages_invalid")
            value = ",".join(languages or ())
        else:
            value = config.get(f"gutendex_{key}")
        if value is not None:
            params[key] = str(value).lower() if isinstance(value, bool) else value
    return f"{BASE}?{urlencode(params)}"


def _book(row: object, collected_at: datetime) -> IngestionRecord | None:
    """Normalize one validated Gutendex book into the shared media record contract."""
    if not isinstance(row, Mapping) or type(row.get("id")) is not int or row["id"] <= 0:
        return None
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    authors = row.get("authors")
    if not isinstance(authors, list) or any(not isinstance(a, Mapping) for a in authors):
        return None
    author_names = [a["name"].strip()[:200] for a in authors if isinstance(a.get("name"), str) and a["name"].strip()]
    raw_subjects = row.get("subjects")
    raw_shelves = row.get("bookshelves")
    subjects = [value for value in raw_subjects if isinstance(value, str)] if isinstance(raw_subjects, list) else []
    shelves = [value for value in raw_shelves if isinstance(value, str)] if isinstance(raw_shelves, list) else []
    genres = normalize_genres((subjects + shelves)[:60])
    languages = row.get("languages")
    language = normalize_language(languages[0]) if isinstance(languages, list) and languages and isinstance(languages[0], str) else None
    book_id = str(row["id"])
    fields: dict[str, object] = {
        "title": title.strip()[:500], "media_type": "book", "format": "ebook",
        "attribution": "Data via Project Gutenberg / Gutendex", "canonical_url": f"https://www.gutenberg.org/ebooks/{book_id}",
        "external_ids": {}, "artwork_aspect": "poster",
    }
    if genres:
        fields["genres"] = genres
    if language:
        fields["language"] = language
    if author_names:
        fields["creator"] = "; ".join(author_names)[:240]
    metadata = ProviderRecordMetadata(provider="gutendex_books", identity=f"gutendex:{book_id}", timestamp_basis="collection", coverage="returned_snapshot", content_truncated=False, source_fields=fields).model_dump(mode="json", exclude_none=True)
    return IngestionRecord(provider_id=f"gutendex:{book_id}", content=title.strip()[:500], observed_at=collected_at, collected_at=collected_at, metadata={"provider": "gutendex_books", "title": title.strip()[:500], "canonical_url": fields["canonical_url"], "provider_record": metadata})


def map_gutendex(body: bytes, *, config: Mapping[str, object], collected_at: datetime, page_number: int) -> GutendexPage:
    """Map one bounded result page; malformed rows fail the page so its cursor cannot advance."""
    if len(body) > MAX_BODY:
        raise ValueError("gutendex_response_invalid")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("gutendex_response_invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list) or len(payload["results"]) > PAGE_SIZE:
        raise ValueError("gutendex_response_invalid")
    next_link = payload.get("next")
    next_cursor = None
    if next_link is not None:
        if not isinstance(next_link, str):
            raise ValueError("gutendex_pagination_invalid")
        if _page_number(next_link) != page_number + 1:
            raise ValueError("gutendex_pagination_invalid")
        next_cursor = next_link
    rows = []
    for item in payload["results"]:
        record = _book(item, collected_at)
        if record is None:
            raise ValueError("gutendex_book_invalid")
        rows.append(record)
    return GutendexPage(
        tuple(
            filter_media_records(
                config, rows, keep_missing=not bool(config.get("media_genres"))
            )
        ),
        next_cursor,
    )


async def collect_gutendex(fetch_bytes: Callable[..., Awaitable[Fetched]], *, config: Mapping[str, object], collected_at: datetime, cursor: str | None = None) -> GutendexPage:
    """Fetch one Gutendex page through the caller's SendGate and bounded REST transport."""
    await PACER.pace("gutendex", 1.1)
    fetched = await fetch_bytes(gutendex_request(config, cursor), headers={"Accept": "application/json"}, max_bytes=MAX_BODY)
    if fetched.body is None:
        raise ProviderHttpError(304)
    return map_gutendex(fetched.body, config=config, collected_at=collected_at, page_number=_page_number(cursor) if cursor else 1)
