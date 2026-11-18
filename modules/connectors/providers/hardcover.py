"""Pure request and page mapping for the Hardcover catalog GraphQL connector."""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from modules.connectors.provider_options import filter_records
from modules.connectors.providers import news
from modules.connectors.providers._media_common import (
    build_external_ids,
    normalize_cover_url,
    normalize_genres,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.macro import ProviderPayloadError, ProviderRequest, decode_json
from modules.connectors.providers.media_anime_manga_sources import _record
from modules.ingestion.public import IngestionRecord

PROVIDER = "hardcover_books"
GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
MAX_BODY_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 25
MAX_PAGES = 2
_MAX_BOOK_ID = 2_147_483_647
_QUERY = """
query HardcoverBooks($where: books_bool_exp!, $limit: Int!) {
  books(where: $where, limit: $limit, order_by: {id: asc}) {
    id title slug description cached_image release_date release_year rating cached_tags pages audio_seconds
    editions(limit: 5, order_by: {id: asc}) { isbn_10 isbn_13 }
  }
}
"""


def cursor_id(cursor: str | None) -> int:
    """Return the last accepted GraphQL Int ID, restarting on an absent or malformed cursor."""
    if cursor is None or not re.fullmatch(r"[0-9]{1,10}", cursor):
        return 0
    value = int(cursor)
    return value if value <= _MAX_BOOK_ID else 0


def build_request(
    secret: str, config: Mapping[str, Any], collected_at: datetime, after: int
) -> ProviderRequest:
    """Build one fixed-host, read-only POST. Encode scope and cursor only as GraphQL variables."""
    where: dict[str, dict[str, Any]] = {"id": {"_gt": after}}
    mode = config.get("hardcover_mode")
    if mode not in ("catalog", "upcoming"):
        raise ValueError("hardcover_mode_required")
    if mode == "upcoming":
        where["release_date"] = {"_gte": collected_at.date().isoformat()}
    if (year := config.get("release_year")) is not None:
        where["release_year"] = {"_eq": year}
    if (rating := config.get("min_rating")) is not None:
        where["rating"] = {"_gte": rating / 20}
    body = json.dumps({"query": _QUERY, "variables": {"where": where, "limit": PAGE_SIZE}}).encode()
    return ProviderRequest(
        "POST",
        GRAPHQL_URL,
        {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        body,
    )


def _genres(raw: object) -> list[str]:
    """Read Hardcover's cached Genre bucket and drop unrecognized labels via the media taxonomy."""
    entries = raw.get("Genre") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    return normalize_genres(
        entry["tag"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("tag"), str)
    )


def _external_ids(book_id: int, editions: object) -> dict[str, str]:
    """Return the Hardcover ID and the first valid ISBN of each type in the ordered edition slice."""
    ids: dict[str, object] = {"hardcover": book_id}
    if isinstance(editions, list):
        for edition in editions[:5]:
            if not isinstance(edition, dict):
                continue
            for source, target, pattern in (
                ("isbn_10", "isbn10", r"[0-9]{9}[0-9X]"),
                ("isbn_13", "isbn13", r"[0-9]{13}"),
            ):
                raw = edition.get(source)
                if target not in ids and isinstance(raw, str):
                    isbn = re.sub(r"[-\s]", "", raw).upper()
                    if re.fullmatch(pattern, isbn):
                        ids[target] = isbn
    return build_external_ids(ids)


def _map_book(book_id: int, row: Mapping[str, Any], collected_at: datetime) -> IngestionRecord:
    """Project a complete book row using collection time for observation and a separate release date."""
    title, slug = row.get("title"), row.get("slug")
    title = news.plain_text(title, 300) if isinstance(title, str) else ""
    if not title:
        raise ProviderPayloadError("hardcover_book_invalid")
    release_date, date_year, release_at = normalize_partial_date(row.get("release_date"))
    raw_year = row.get("release_year")
    release_year = raw_year if type(raw_year) is int and 1800 <= raw_year <= 2100 else date_year
    raw_pages, audio = row.get("pages"), row.get("audio_seconds")
    pages = raw_pages if type(raw_pages) is int and 0 <= raw_pages <= 50000 else None
    image = row.get("cached_image")
    summary = row.get("description")
    summary = news.plain_text(summary, 4000) if isinstance(summary, str) else ""
    audiobook = type(audio) is int and 0 < audio <= 604800
    fields = {
        "canonical_url": f"https://hardcover.app/books/{slug}"
        if isinstance(slug, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,240}", slug)
        else None,
        "media_type": "audiobook" if audiobook and not pages else "book",
        "format": "audiobook" if audiobook and not pages else None,
        "release_date": release_date,
        "release_year": release_year,
        "release_status": ("upcoming" if release_at.date() >= collected_at.date() else "released")
        if release_at is not None and release_date is not None and len(release_date) == 10
        else None,
        "rating": normalize_rating(row.get("rating"), scale=5),
        "genres": _genres(row.get("cached_tags")),
        "page_count": pages,
        "duration_seconds": audio if audiobook else None,
        "summary": summary or None,
        "image_url": normalize_cover_url(image.get("url")) if isinstance(image, dict) else None,
        "artwork_aspect": "poster",
        "external_ids": _external_ids(book_id, row.get("editions")),
    }
    return _record(
        PROVIDER,
        f"hardcover:{book_id}",
        title,
        summary,
        collected_at,
        {key: value for key, value in fields.items() if value is not None},
    )


def map_page(
    body: bytes, config: Mapping[str, Any], collected_at: datetime, after: int
) -> tuple[list[IngestionRecord], int]:
    """Map one complete ordered page and return its next ID, or zero when the catalog walk ends.

    Error and malformed responses never yield a cursor, even when GraphQL also returned partial data.
    The cursor follows upstream IDs, including books excluded by canonical filters.
    """
    payload = decode_json(body)
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ProviderPayloadError("hardcover_page_incomplete", "incomplete")
    extensions = payload.get("extensions")
    if isinstance(extensions, dict) and extensions.get("truncated"):
        raise ProviderPayloadError("hardcover_page_incomplete", "incomplete")
    data = payload.get("data")
    books = data.get("books") if isinstance(data, dict) else None
    if not isinstance(books, list) or len(books) > PAGE_SIZE:
        raise ProviderPayloadError("hardcover_page_incomplete", "incomplete")
    records: list[IngestionRecord] = []
    for row in books:
        book_id = row.get("id") if isinstance(row, dict) else None
        if type(book_id) is not int or not after < book_id <= _MAX_BOOK_ID:
            raise ProviderPayloadError("hardcover_page_order_invalid", "incomplete")
        after = book_id
        records.append(_map_book(book_id, row, collected_at))
    return filter_records(PROVIDER, config, records), after if len(books) == PAGE_SIZE else 0
