"""Bounded Open Library search, subject, and recent-change collectors."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers._media_common import (
    build_external_ids,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
)
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

BASE = "https://openlibrary.org"
MAX_RESULTS = 100
MAX_RECENT = 10
MAX_RECENT_OFFSET = 10_000
RECENT_OVERLAP = 2
MAX_BODY = 4 * 1024 * 1024
_LANGUAGE = {
    "en": "eng",
    "ja": "jpn",
    "fr": "fre",
    "vi": "vie",
    "zh": "chi",
    "ko": "kor",
    "de": "ger",
    "es": "spa",
    "pt": "por",
    "it": "ita",
    "ru": "rus",
    "id": "ind",
    "th": "tha",
    "zh-hk": "chi",
}
_WORK = re.compile(r"^/works/OL[1-9][0-9]*W$")
_PACER: asyncio.Lock | None = None
_LAST_SEND = 0.0


@dataclass(frozen=True)
class OpenLibraryPage:
    """Mapped records and the next bounded recent-change cursor."""

    records: tuple[IngestionRecord, ...]
    cursor: str | None


def openlibrary_request(config: Mapping[str, object], cursor: str | None = None) -> str:
    """Build a fixed-host, bounded Open Library URL from validated source options."""
    mode = config.get("openlibrary_mode") or "subject"
    if mode == "subject":
        subject = str(config.get("openlibrary_subject") or "").strip().lower().replace(" ", "_")
        if not re.fullmatch(r"[a-z0-9_-]{1,80}", subject):
            raise ValueError("openlibrary_subject_invalid")
        query: dict[str, str | int] = {"limit": MAX_RESULTS}
        if config.get("ebooks_only"):
            query["ebooks"] = "true"
        return f"{BASE}/subjects/{subject}.json?{urlencode(query)}"
    if mode == "search":
        q = str(config.get("openlibrary_query") or "").strip()
        if not 1 <= len(q) <= 200:
            raise ValueError("openlibrary_query_invalid")
        query = {"q": q, "limit": MAX_RESULTS}
        languages = config.get("media_languages")
        lang = languages[0] if isinstance(languages, (list, tuple)) and languages else None
        if lang:
            query["language"] = _LANGUAGE[str(lang)]
        if config.get("ebooks_only"):
            query["ebook_access"] = "public"
        return f"{BASE}/search.json?{urlencode(query)}"
    if mode == "recent":
        offset = max(0, _recent_offset(cursor) - RECENT_OVERLAP)
        return f"{BASE}/recentchanges.json?{urlencode({'limit': MAX_RECENT, 'offset': offset, 'kind': 'update', 'type': '/type/work'})}"
    raise ValueError("openlibrary_mode_invalid")


def _recent_offset(cursor: str | None) -> int:
    """Parse the persisted recent-change offset, wrapping malformed or exhausted cursors safely."""
    try:
        offset = int(cursor or "0")
    except (ValueError, TypeError):
        return 0
    return offset if 0 <= offset < MAX_RECENT_OFFSET else 0


def _work(row: object, collected_at: datetime) -> IngestionRecord | None:
    """Normalize a search document into the shared media source field allowlist."""
    if not isinstance(row, Mapping):
        return None
    key = row.get("key")
    if not isinstance(key, str) or not _WORK.fullmatch(key):
        return None
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    work_id = key.rsplit("/", 1)[-1]
    isbns = row.get("isbn")
    isbn13 = (
        next((v for v in isbns if isinstance(v, str) and len(v) == 13 and v.isdigit()), None)
        if isinstance(isbns, list)
        else None
    )
    year = row.get("first_publish_year")
    if type(year) is not int or not 1800 <= year <= 2100:
        year = None
    date_text = row.get("first_publish_date")
    normalized_date = normalize_partial_date(date_text)[0] if isinstance(date_text, str) else None
    if year is None and normalized_date:
        candidate_year = int(normalized_date[:4])
        year = candidate_year if 1800 <= candidate_year <= 2100 else None
    languages = row.get("language")
    language = (
        normalize_language(languages[0])
        if isinstance(languages, list) and languages and isinstance(languages[0], str)
        else None
    )
    rating = row.get("ratings_average")
    rating = (
        round(float(rating) * 20, 2)
        if isinstance(rating, (int, float)) and 0 <= rating <= 5
        else None
    )
    raw_subjects = row.get("subject", row.get("subjects", []))
    subjects = normalize_genres(raw_subjects[:30]) if isinstance(raw_subjects, list) else []
    cover = row.get("cover_i")
    if cover is None and isinstance(row.get("covers"), list) and row["covers"]:
        cover = row["covers"][0]
    fields: dict[str, object] = {
        "title": title.strip()[:500],
        "media_type": "book",
        "format": "ebook" if row.get("ebook_access") in {"public", "borrowable"} else "novel",
        "attribution": "Data via Open Library (CC0 1.0)",
        "canonical_url": f"https://openlibrary.org{key}",
        "external_ids": build_external_ids({"openlibrary": work_id, "isbn13": isbn13}),
        "artwork_aspect": "poster",
    }
    if year:
        fields["release_year"] = year
    if normalized_date:
        fields["release_date"] = normalized_date
    if language:
        fields["language"] = language
    if rating is not None:
        fields["rating"] = rating
    if subjects:
        fields["genres"] = subjects
    if type(cover) is int and cover > 0:
        fields["image_url"] = f"https://covers.openlibrary.org/b/id/{cover}-L.jpg"
    metadata = ProviderRecordMetadata(
        provider="openlibrary_books",
        identity=f"openlibrary:{work_id}",
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=False,
        source_fields=fields,
    ).model_dump(mode="json", exclude_none=True)
    return IngestionRecord(
        provider_id=f"openlibrary:{work_id}",
        content=title.strip()[:500],
        observed_at=collected_at,
        collected_at=collected_at,
        metadata={
            "provider": "openlibrary_books",
            "title": title.strip()[:500],
            "canonical_url": fields["canonical_url"],
            "provider_record": metadata,
        },
    )


def map_openlibrary(
    body: bytes,
    *,
    mode: str,
    config: Mapping[str, object],
    collected_at: datetime,
    cursor: str | None = None,
) -> OpenLibraryPage:
    """Parse a size-bounded response, deduplicate normalized works, and filter configured media options."""
    if len(body) > MAX_BODY:
        raise ValueError("openlibrary_response_invalid")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("openlibrary_response_invalid") from exc
    if mode == "recent":
        raise ValueError("openlibrary_recent_requires_work_details")
    rows = payload.get("docs", []) if mode == "search" else payload.get("works", [])
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS:
        raise ValueError("openlibrary_response_invalid")
    records = {
        _id: item
        for item in (_work(row, collected_at) for row in rows)
        if item is not None
        for _id in [item.provider_id]
    }
    filtered = filter_media_records(config, list(records.values()), keep_missing=True)
    return OpenLibraryPage(tuple(filtered), None)


async def collect_openlibrary(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    config: Mapping[str, object],
    collected_at: datetime,
    cursor: str | None = None,
) -> OpenLibraryPage:
    """Fetch one bounded Open Library response through the caller's SendGate transport."""
    mode = str(config.get("openlibrary_mode") or "subject")
    fetched = await fetch_bytes(
        openlibrary_request(config, cursor),
        headers={"Accept": "application/json"},
        max_bytes=MAX_BODY,
    )
    if fetched.body is None:
        raise ProviderHttpError(304)
    if mode == "recent":
        global _PACER, _LAST_SEND
        if _PACER is None:
            _PACER = asyncio.Lock()
        try:
            changes = json.loads(fetched.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("openlibrary_response_invalid") from exc
        if not isinstance(changes, list) or len(changes) > MAX_RECENT:
            raise ValueError("openlibrary_response_invalid")
        works: dict[str, Mapping[str, object]] = {}
        change_keys: list[list[str]] = []
        for change in changes:
            if not isinstance(change, Mapping) or not isinstance(change.get("changes"), list):
                raise ValueError("openlibrary_response_invalid")
            keys = list(
                dict.fromkeys(
                    entry["key"]
                    for entry in change["changes"]
                    if isinstance(entry, Mapping)
                    and isinstance(entry.get("key"), str)
                    and _WORK.fullmatch(entry["key"])
                )
            )
            change_keys.append(keys)
        keys = list(dict.fromkeys(key for row_keys in change_keys for key in row_keys))
        if len(keys) > MAX_RECENT:
            raise ValueError("openlibrary_recent_fanout_exceeded")
        for key in keys:
            async with _PACER:
                loop = asyncio.get_running_loop()
                delay = 1 / 3 - (loop.time() - _LAST_SEND)
                if _LAST_SEND and delay > 0:
                    await asyncio.sleep(delay)
                _LAST_SEND = loop.time()
            detail = await fetch_bytes(
                f"{BASE}{key}.json", headers={"Accept": "application/json"}, max_bytes=256 * 1024
            )
            if detail.body is None:
                raise ProviderHttpError(502)
            try:
                row = json.loads(detail.body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("openlibrary_response_invalid") from exc
            if not isinstance(row, Mapping) or _work(row, collected_at) is None:
                raise ValueError("openlibrary_work_invalid")
            works.setdefault(key, row)
        rows = list(works.values())
        records = tuple(
            filter_media_records(
                config,
                [item for item in (_work(row, collected_at) for row in rows) if item is not None],
                keep_missing=True,
            )
        )
        offset = _recent_offset(cursor)
        request_offset = max(0, offset - RECENT_OVERLAP)
        next_offset = request_offset + len(changes)
        next_cursor = (
            "0"
            if len(changes) < MAX_RECENT or next_offset >= MAX_RECENT_OFFSET
            else str(next_offset)
        )
        return OpenLibraryPage(records, next_cursor)
    return map_openlibrary(
        fetched.body, mode=mode, config=config, collected_at=collected_at, cursor=cursor
    )
