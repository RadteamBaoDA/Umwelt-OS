"""Bounded ANN, MangaUpdates, and MangaDex collectors for media releases."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers import news
from modules.connectors.providers._media_common import (
    MEDIA_ATTRIBUTIONS,
    build_external_ids,
    build_media_identity,
    normalize_content_rating,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.rest import CollectionIncomplete, Fetched
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

ANN_NEWS_URL = "https://www.animenewsnetwork.com/news/atom.xml"
MANGAUPDATES_SEARCH_URL = "https://api.mangaupdates.com/v1/series/search"
MANGAUPDATES_RELEASE_SEARCH_URL = "https://api.mangaupdates.com/v1/releases/search"
MANGADEX_URL = "https://api.mangadex.org"
MAX_ITEMS = 100
MAX_BYTES = 2 * 1024 * 1024
MANGADEX_GENRE_TAGS = {
    "action": "391b0423-d847-456f-aff0-8b0cfc03066b",
    "adventure": "87cc87cd-a395-47af-b27a-93258283bbc6",
    "sci_fi": "256c8bd9-4904-4360-bf4f-508a76d67183",
    "fantasy": "cdc58593-87dd-415e-bbc0-2ec27bf404cc",
    "romance": "423e2eae-a7a2-4a8b-ac03-a8351462d71d",
    "thriller": "07251805-a27e-4d59-b488-f0bfbec15168",
    "horror": "cdad7e68-1419-41dd-bdce-27753074a640",
    "comedy": "4d32cc48-9f00-4cca-9b5a-a839f0764984",
    "drama": "b9af3a63-f058-46de-a9a0-e0c13906197a",
    "mystery": "ee653e07-d70f-4ec3-807b-38b78795154c",
    "slice_of_life": "e5301a23-ebd9-49dd-aca1-05ae9733532d",
    "history": "33208764-8efb-4232-a986-0eaca8d3f8ba",
    "sports": "69964a64-2f90-4d9a-beeb-f3ed2875eb4c",
}


@dataclass(frozen=True)
class CursorPage:
    """One bounded provider page with a cursor that the caller must commit atomically."""

    records: tuple[IngestionRecord, ...]
    cursor: str | None


def _record(
    provider: str,
    identity: str,
    title: str,
    summary: str,
    collected_at: datetime,
    fields: dict[str, object],
    *,
    observed_at: datetime | None = None,
) -> IngestionRecord:
    """Create a bounded media record with canonical identity and provider provenance."""
    clean_title = news.plain_text(title, 500)
    clean_summary = news.plain_text(summary, 8000)
    content = f"{clean_title}\n\n{clean_summary}".strip()[:12000]
    fields.update(
        {
            "title": clean_title,
            "attribution": MEDIA_ATTRIBUTIONS.get(provider, "Data via Anime News Network"),
        }
    )
    canonical = fields.get("canonical_url")
    provider_record = ProviderRecordMetadata(
        provider=provider,
        identity=identity,
        timestamp_basis="provider_published" if observed_at else "collection",
        coverage="returned_snapshot",
        content_truncated=len(f"{clean_title}\n\n{clean_summary}".strip()) > len(content),
        source_fields=fields,
    ).model_dump(mode="json", exclude_none=True)
    metadata: dict[str, object] = {
        "provider": provider,
        "title": clean_title,
        "provider_record": provider_record,
    }
    if isinstance(canonical, str):
        metadata["canonical_url"] = canonical
    return IngestionRecord(
        provider_id=identity,
        content=content,
        observed_at=observed_at or collected_at,
        collected_at=collected_at,
        metadata=metadata,
    )


def build_ann_news_url(config: Mapping[str, Any]) -> str:
    """Build a fixed-host ANN Atom category/edition URL from validated option values."""
    category = config.get("ann_category") or "all"
    edition = config.get("ann_edition")
    if category not in {"news", "review", "feature", "interview", "press-release", "all"}:
        raise ValueError("ann_category_invalid")
    if edition not in {None, "us", "w"}:
        raise ValueError("ann_edition_invalid")
    path = "news/atom.xml" if category in {"all", "news"} else f"news/{category}/atom.xml"
    query = "?ann-edition=" + str(edition) if edition else ""
    return f"https://www.animenewsnetwork.com/{path}{query}"


def build_ann_encyclopedia_url(media_type: str, *, offset: int = 0) -> str:
    """Build the official ANN encyclopedia report URL with a bounded row window."""
    if media_type not in {"anime", "manga"}:
        raise ValueError("ann_media_type_invalid")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("ann_offset_invalid")
    return (
        "https://www.animenewsnetwork.com/encyclopedia/reports.xml?"
        f"{urlencode({'id': 155, 'type': media_type, 'nskip': offset, 'nlist': MAX_ITEMS})}"
    )


@dataclass(frozen=True)
class _AnnEntry:
    """Raw, unvalidated text read from one ANN Atom entry's child elements."""

    title: str
    summary: str
    identifier: str
    date_raw: str
    categories: list[str]
    link: str


def _read_ann_entry(entry: ET.Element) -> _AnnEntry:
    """Collect an Atom entry's title, summary, id, first date, categories and alternate link.

    Missing elements yield empty strings. The first published/updated date wins; a later one is
    ignored. Only a link with rel "alternate" (the default) is read. Pure, and never raises for
    well-formed elements.
    """
    title = ""
    summary = ""
    identifier = ""
    date_raw = ""
    categories: list[str] = []
    link = ""
    for child in entry:
        local = news._local(child.tag)
        if local == "title":
            title = child.text or ""
        elif local in {"summary", "content"}:
            summary = child.text or ""
        elif local == "id":
            identifier = child.text or ""
        elif local in {"published", "updated"} and not date_raw:
            date_raw = child.text or ""
        elif local == "category":
            term = child.attrib.get("term")
            if term:
                categories.append(term)
        elif local == "link" and child.attrib.get("rel", "alternate") == "alternate":
            link = child.attrib.get("href", "")
    return _AnnEntry(title, summary, identifier, date_raw, categories, link)


def map_ann_news(
    body: bytes, *, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map bounded ANN Atom entries and preserve sparse news rows through media filtering.

    Raises ValueError for a naive collected_at, an invalid or oversized feed (more than
    MAX_ITEMS entries). Entries without a title, id, or an https animenewsnetwork.com link are
    skipped. Duplicate identities keep the first entry. Results are media-filtered with
    keep_missing=True.
    """
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    root = news.safe_xml_root(body, max_bytes=news.MAX_FEED_BYTES)
    entries = [node for node in root.iter() if news._local(node.tag) == "entry"]
    if len(entries) > MAX_ITEMS:
        raise ValueError("ann_news_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for entry in entries:
        parsed = _read_ann_entry(entry)
        title = news.plain_text(parsed.title, 500)
        safe_link = news.https_link(parsed.link)
        if (
            not title
            or not parsed.identifier
            or not safe_link
            or not safe_link.startswith("https://www.animenewsnetwork.com/")
        ):
            continue
        published, year, observed = normalize_partial_date(parsed.date_raw)
        genres = normalize_genres(parsed.categories)
        kinds = " ".join(parsed.categories + [title]).lower()
        media_type = "manga" if "manga" in kinds else "anime"
        fields: dict[str, object] = {
            "media_type": media_type,
            "canonical_url": safe_link,
        }
        if published:
            fields["release_date"] = published
        if year:
            fields["release_year"] = year
        if genres:
            fields["genres"] = genres
        identity = build_media_identity("ann_news", parsed.identifier)
        records.setdefault(
            identity,
            _record(
                "ann_news",
                identity,
                title,
                parsed.summary,
                collected_at,
                fields,
                observed_at=observed,
            ),
        )
    return filter_media_records(config, list(records.values()), keep_missing=True)


def map_ann_encyclopedia(
    body: bytes,
    *,
    media_type: str,
    collected_at: datetime,
    config: Mapping[str, Any],
) -> list[IngestionRecord]:
    """Map one bounded ANN encyclopedia XML report, preserving ANN attribution and entry links."""
    if media_type not in {"anime", "manga"}:
        raise ValueError("ann_media_type_invalid")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    root = news.safe_xml_root(body, max_bytes=news.MAX_FEED_BYTES)
    items = [node for node in root.iter() if news._local(node.tag) == "item"]
    if len(items) > MAX_ITEMS:
        raise ValueError("ann_encyclopedia_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for item in items:
        entry_id = item.attrib.get("id")
        if not isinstance(entry_id, str) or not entry_id.isdigit():
            continue
        values: dict[str, list[str]] = {}
        for child in item:
            local = news._local(child.tag).lower()
            value = news.plain_text(child.text or "", 4000)
            if local and value:
                values.setdefault(local, []).append(value)
        title = next(iter(values.get("name", values.get("title", []))), "")
        if not title:
            continue
        summary = " ".join(values.get("description", values.get("synopsis", [])))
        genres = normalize_genres(values.get("genre", []) + values.get("category", []))
        year_text = next(iter(values.get("year", values.get("start", []))), None)
        published, year, observed = normalize_partial_date(year_text)
        url = f"https://www.animenewsnetwork.com/encyclopedia/{media_type}.php?id={entry_id}"
        fields: dict[str, object] = {"media_type": media_type, "canonical_url": url}
        if genres:
            fields["genres"] = genres
        if published:
            fields["release_date"] = published
        if year:
            fields["release_year"] = year
        identity = build_media_identity("ann_news", entry_id)
        records.setdefault(
            identity,
            _record(
                "ann_news", identity, title, summary, collected_at, fields, observed_at=observed
            ),
        )
    return filter_media_records(config, list(records.values()), keep_missing=True)


def mangaupdates_search_body(config: Mapping[str, Any], *, page: int = 1) -> bytes:
    """Serialize the bounded series-search filters supported by MangaUpdates."""
    if type(page) is not int or not 1 <= page <= 100_000:
        raise ValueError("mangaupdates_page_invalid")
    body: dict[str, object] = {"page": page, "perpage": MAX_ITEMS}
    media_types = (
        config.get("types") or config.get("mangaupdates_types") or config.get("media_formats")
    )
    if media_types:
        normalized_types = [
            {"manga": "Manga", "manhwa": "Manhwa", "manhua": "Manhua"}.get(str(item).lower(), item)
            for item in media_types
        ]
        body["stype"] = "title"
        body["type"] = normalized_types
        body["filter_types"] = normalized_types
    if config.get("licensed_only") is not None:
        body["licensed"] = config["licensed_only"]
    if type(config.get("release_year")) is int:
        body["year"] = config["release_year"]
    genres = config.get("media_genres")
    if genres:
        body["genre"] = list(genres)
    if config.get("exclude_genre"):
        body["exclude_genre"] = list(config["exclude_genre"])
    return json.dumps(body, separators=(",", ":")).encode()


def mangaupdates_release_search_body(*, page: int = 0) -> bytes:
    """Serialize one bounded public release-search page with inline series metadata."""
    if type(page) is not int or not 0 <= page <= 100_000:
        raise ValueError("mangaupdates_page_invalid")
    return json.dumps(
        {
            "page": page,
            "perpage": MAX_ITEMS,
            "search_type": "regular",
            "orderby": "date",
            "asc": "desc",
            "include_metadata": True,
        },
        separators=(",", ":"),
    ).encode()


def _map_mangaupdates_release(
    row: Mapping[str, Any],
    *,
    collected_at: datetime,
    config: Mapping[str, Any],
    series_metadata: Mapping[str, Mapping[str, Any]] | None,
) -> IngestionRecord | None:
    """Map one release-search hit using its embedded metadata and apply strict series filters."""
    release_value = row.get("record")
    release: Mapping[str, Any] = release_value if isinstance(release_value, Mapping) else row
    metadata = row.get("metadata")
    series_value = metadata.get("series") if isinstance(metadata, Mapping) else row.get("series")
    series: Mapping[str, Any] = series_value if isinstance(series_value, Mapping) else {}
    series_id = series.get("series_id", series.get("id", release.get("series_id")))
    if isinstance(series_id, bool) or not str(series_id or "").isdigit():
        return None
    series_id = str(series_id)
    if series_metadata and series_id in series_metadata:
        series = {**series_metadata[series_id], **series}
    if config.get("series_ids") and series_id not in {str(value) for value in config["series_ids"]}:
        return None
    series_title = series.get("title") or release.get("title")
    if not isinstance(series_title, str) or not series_title.strip():
        return None
    chapter = release.get("chapter")
    chapter_number = chapter.get("number") if isinstance(chapter, Mapping) else chapter
    chapter_title = chapter.get("title") if isinstance(chapter, Mapping) else None
    heading = (
        f"{series_title.strip()} — Chapter {chapter_number}"
        if chapter_number is not None
        else series_title.strip()
    )
    if isinstance(chapter_title, str) and chapter_title.strip():
        heading += f": {chapter_title.strip()}"
    time_added = release.get("time_added")
    date_value = (
        time_added.get("as_rfc3339") or time_added.get("as_string")
        if isinstance(time_added, Mapping)
        else None
    )
    release_date, year, observed = normalize_partial_date(
        release.get("release_date") or date_value or release.get("date")
    )
    series_type = str(series.get("type") or "").lower()
    selected_types = (
        config.get("types") or config.get("mangaupdates_types") or config.get("media_formats")
    )
    if selected_types and series_type not in {str(value).lower() for value in selected_types}:
        return None
    if config.get("licensed_only") is True and series.get("licensed") is not True:
        return None
    country = {"manga": "JP", "manhwa": "KR", "manhua": "CN"}.get(series_type)
    raw_genres = series.get("genres", [])
    genres = (
        normalize_genres(
            [
                item.get("genre", item.get("name", "")) if isinstance(item, Mapping) else str(item)
                for item in raw_genres
            ]
        )
        if isinstance(raw_genres, list)
        else []
    )
    rating = normalize_rating(series.get("bayesian_rating"), scale=10)
    fields: dict[str, object] = {
        "media_type": "manga",
        "format": "manhwa" if country == "KR" else "manhua" if country == "CN" else "manga",
        "creator": series_title.strip()[:240],
        "canonical_url": f"https://www.mangaupdates.com/series/{series_id}",
        "external_ids": build_external_ids({"mangaupdates": series_id}),
    }
    if country:
        fields["country"] = country
    if release_date:
        fields["release_date"] = release_date
    if year:
        fields["release_year"] = year
    if genres:
        fields["genres"] = genres
    if rating is not None:
        fields["rating"] = rating
    if chapter_number is not None:
        fields["chapter"] = str(chapter_number)[:80]
    identity = build_media_identity(
        "mangaupdates", release.get("id") or f"{series_id}:{chapter_number}:{release_date}"
    )
    return _record(
        "mangaupdates_releases", identity, heading, "", collected_at, fields, observed_at=observed
    )


def map_mangaupdates_releases(
    payload: object,
    *,
    collected_at: datetime,
    config: Mapping[str, Any],
    series_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[IngestionRecord]:
    """Normalize one bounded MangaUpdates releases response and apply configured media filters."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    if not isinstance(payload, Mapping):
        raise ValueError("mangaupdates_response_invalid")
    rows = payload.get("results", payload.get("releases", payload.get("data", [])))
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise ValueError("mangaupdates_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in rows:
        if (
            isinstance(row, Mapping)
            and (
                record := _map_mangaupdates_release(
                    row, collected_at=collected_at, config=config, series_metadata=series_metadata
                )
            )
            is not None
        ):
            records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()))


def build_mangadex_manga_url(config: Mapping[str, Any], *, offset: int = 0) -> str:  # noqa: C901, PLR0912
    """Build one bounded MangaDex manga metadata query with validated URL parameters."""
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("mangadex_offset_invalid")
    query: list[tuple[str, str]] = [
        ("limit", str(MAX_ITEMS)),
        ("offset", str(offset)),
        ("order[updatedAt]", "desc"),
        ("includes[]", "cover_art"),
    ]
    raw_ids = config.get("manga_ids") or ()
    if isinstance(raw_ids, str):
        raw_ids = (raw_ids,)
    if not isinstance(raw_ids, (list, tuple)):
        raise ValueError("mangadex_ids_invalid")
    try:
        ids = [str(UUID(str(value))) for value in raw_ids]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("mangadex_ids_invalid") from exc
    if ids:
        if len(ids) > 100 or any(not value or len(value) > 64 for value in ids):
            raise ValueError("mangadex_ids_invalid")
        query.extend(("ids[]", value) for value in ids)
    for language in config.get("original_languages") or ():
        if language not in {"ja", "ko", "zh", "zh-hk"}:
            raise ValueError("mangadex_language_invalid")
        query.append(("originalLanguage[]", str(language)))
    for language in config.get("available_translated_languages") or ():
        if language not in {"en", "ja", "ko", "zh", "zh-hk", "vi", "fr", "de", "es"}:
            raise ValueError("mangadex_translation_language_invalid")
        query.append(("availableTranslatedLanguage[]", str(language)))
    content_ratings = config.get("content_rating") or config.get("content_ratings") or ()
    if isinstance(content_ratings, str):
        content_ratings = (content_ratings,)
    for rating in content_ratings:
        if rating not in {"safe", "suggestive", "erotica", "pornographic"}:
            raise ValueError("mangadex_rating_invalid")
        query.append(("contentRating[]", str(rating)))
    for status in config.get("mangadex_statuses") or ():
        if status not in {"ongoing", "completed", "hiatus", "cancelled"}:
            raise ValueError("mangadex_status_invalid")
        query.append(("status[]", str(status)))
    for demographic in config.get("publication_demographic") or ():
        if demographic not in {"shounen", "shoujo", "josei", "seinen", "none"}:
            raise ValueError("mangadex_demographic_invalid")
        query.append(("publicationDemographic[]", str(demographic)))
    if type(config.get("release_year")) is int:
        query.append(("year", str(config["release_year"])))
    updated_since = config.get("updated_at_since")
    if (
        isinstance(updated_since, str)
        and len(updated_since) <= 32
        and not any(ord(char) < 32 for char in updated_since)
    ):
        query.append(("updatedAtSince", updated_since))
    return f"{MANGADEX_URL}/manga?{urlencode(query)}"


def map_mangadex_manga(payload: object) -> dict[str, dict[str, Any]]:
    """Extract bounded MangaDex manga metadata by UUID for chapter mapping."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list) or len(data) > MAX_ITEMS:
        raise ValueError("mangadex_response_invalid")
    out: dict[str, dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        try:
            manga_id = str(UUID(row["id"]))
        except ValueError:
            continue
        attrs = row.get("attributes")
        if not isinstance(attrs, Mapping):
            continue
        title_map = attrs.get("title")
        title = (
            next(
                (value for value in title_map.values() if isinstance(value, str) and value.strip()),
                None,
            )
            if isinstance(title_map, Mapping)
            else None
        )
        if not title:
            continue
        tags = attrs.get("tags")
        tag_names = (
            [
                tag.get("id", tag.get("attributes", {}).get("name", {}).get("en", ""))
                for tag in tags
                if isinstance(tag, Mapping) and isinstance(tag.get("attributes"), Mapping)
            ]
            if isinstance(tags, list)
            else []
        )
        relationships = row.get("relationships", [])
        cover = (
            next(
                (
                    item
                    for item in relationships
                    if isinstance(item, Mapping) and item.get("type") == "cover_art"
                ),
                None,
            )
            if isinstance(relationships, list)
            else None
        )
        filename = (
            (cover.get("attributes") or {}).get("fileName")
            if isinstance(cover, Mapping) and isinstance(cover.get("attributes"), Mapping)
            else None
        )
        image = (
            normalize_cover_url(
                f"https://uploads.mangadex.org/covers/{manga_id}/{filename}.256.jpg"
            )
            if isinstance(filename, str)
            else None
        )
        out[manga_id] = {
            "title": title,
            "attrs": attrs,
            "image_url": image,
            "genres": normalize_genres(tag_names, provider="mangadex_releases"),
        }
    return out


def build_mangadex_chapters_url(
    config: Mapping[str, Any], manga: Mapping[str, Mapping[str, Any]], *, offset: int = 0
) -> str:
    """Build one MangaDex chapter-feed URL; OR tag mode prevents accidental AND narrowing."""
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("mangadex_offset_invalid")
    query: list[tuple[str, str]] = [
        ("limit", str(MAX_ITEMS)),
        ("offset", str(offset)),
        ("order[readableAt]", "desc"),
        ("includes[]", "scanlation_group"),
    ]
    query.extend(("manga[]", identifier) for identifier in list(manga)[:100])
    tag_ids = [
        MANGADEX_GENRE_TAGS[genre]
        for genre in config.get("media_genres") or ()
        if genre in MANGADEX_GENRE_TAGS
    ]
    if tag_ids:
        query.extend(("includedTags[]", value) for value in tag_ids)
        query.append(("includedTagsMode", "OR"))
    excluded_ids = config.get("excluded_tags") or ()
    if isinstance(excluded_ids, str):
        excluded_ids = (excluded_ids,)
    if not isinstance(excluded_ids, (list, tuple)) or len(excluded_ids) > 100:
        raise ValueError("mangadex_excluded_tags_invalid")
    try:
        excluded_ids = [str(UUID(value)) for value in excluded_ids]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("mangadex_excluded_tags_invalid") from exc
    query.extend(("excludedTags[]", value) for value in excluded_ids)
    for language in (
        config.get("available_translated_languages") or config.get("media_languages") or ()
    ):
        query.append(("translatedLanguage[]", str(language)))
    if config.get("release_year"):
        year = config["release_year"]
        query.extend(
            (
                ("publishAtSince", f"{year}-01-01T00:00:00"),
                ("publishAtUntil", f"{year}-12-31T23:59:59"),
            )
        )
    return f"{MANGADEX_URL}/chapter?{urlencode(query)}"


def _mangadex_chapter(row: object) -> tuple[str, Mapping[str, Any], list[Any]] | None:
    """Validate one MangaDex chapter row; return (canonical chapter UUID, attributes, relations).

    Returns None for a non-mapping row, a non-string or non-UUID id, or missing attributes or
    relationships. The UUID is normalized with str(UUID(...)) so the identity is canonical.
    """
    if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
        return None
    try:
        chapter_id = str(UUID(row["id"]))
    except ValueError:
        return None
    attrs = row.get("attributes")
    relationships = row.get("relationships")
    if not isinstance(attrs, Mapping) or not isinstance(relationships, list):
        return None
    return chapter_id, attrs, relationships


def _mangadex_series(
    relationships: list[Any], manga: Mapping[str, Mapping[str, Any]]
) -> tuple[str, Mapping[str, Any]] | None:
    """Find the first "manga" relation and its cached series info as (manga id, info).

    Returns None when no manga relation exists, the id is not a string, or the id is not in
    the prefetched manga map (or maps to an empty value).
    """
    series = next(
        (
            item
            for item in relationships
            if isinstance(item, Mapping) and item.get("type") == "manga"
        ),
        None,
    )
    manga_id = series.get("id") if isinstance(series, Mapping) else None
    series_info = manga.get(manga_id) if isinstance(manga_id, str) else None
    if not series_info or not isinstance(manga_id, str):
        return None
    return manga_id, series_info


def _mangadex_fields(
    attrs: Mapping[str, Any],
    series_info: Mapping[str, Any],
    *,
    manga_id: str,
    chapter_id: str,
    release_date: str | None,
    year: int | None,
) -> dict[str, object]:
    """Build the chapter's source fields: country and language come from the series' original language.

    Key order is part of the stored source_fields shape. Chapter and volume are kept only when
    they are non-blank strings, truncated to 80 characters. A pornographic series rating is
    stored as "explicit".
    """
    manga_attrs = series_info["attrs"]
    language = manga_attrs.get("originalLanguage")
    country = {"ja": "JP", "ko": "KR", "zh": "CN", "zh-hk": "TW"}.get(language)
    fields: dict[str, object] = {
        "media_type": "manga",
        "format": "manhwa" if country == "KR" else "manhua" if country in {"CN", "TW"} else "manga",
        "canonical_url": f"https://mangadex.org/chapter/{chapter_id}",
        "external_ids": build_external_ids({"mangadex": manga_id}),
    }
    if country:
        fields["country"] = country
    if language := normalize_language(language):
        fields["language"] = language
    if release_date:
        fields["release_date"] = release_date
    if year:
        fields["release_year"] = year
    if series_info.get("genres"):
        fields["genres"] = series_info["genres"]
    for key, target in (("chapter", "chapter"), ("volume", "volume")):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            fields[target] = value[:80]
    raw_rating = manga_attrs.get("contentRating")
    content_rating = (
        "explicit" if raw_rating == "pornographic" else normalize_content_rating(raw_rating)
    )
    if content_rating:
        fields["content_rating"] = content_rating
    if series_info.get("image_url"):
        fields["image_url"] = series_info["image_url"]
    return fields


def map_mangadex_chapters(
    payload: object,
    *,
    manga: Mapping[str, Mapping[str, Any]],
    collected_at: datetime,
    config: Mapping[str, Any],
) -> list[IngestionRecord]:
    """Map a bounded MangaDex chapter page with original-language country and poster metadata.

    Raises ValueError for a naive collected_at, or a payload whose data is not a list of at most
    MAX_ITEMS rows. Rows that are malformed, or whose series is not in the prefetched manga map,
    are skipped. Duplicate chapter identities keep the first row.
    """
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list) or len(data) > MAX_ITEMS:
        raise ValueError("mangadex_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in data:
        parsed = _mangadex_chapter(row)
        if parsed is None:
            continue
        chapter_id, attrs, relationships = parsed
        series = _mangadex_series(relationships, manga)
        if series is None:
            continue
        manga_id, series_info = series
        title = str(series_info["title"])
        chapter = attrs.get("chapter")
        heading = f"{title} — Chapter {chapter}" if chapter else title
        release_date, year, observed = normalize_partial_date(attrs.get("readableAt"))
        fields = _mangadex_fields(
            attrs,
            series_info,
            manga_id=manga_id,
            chapter_id=chapter_id,
            release_date=release_date,
            year=year,
        )
        identity = build_media_identity("mangadex", chapter_id)
        summary = attrs.get("title")
        records.setdefault(
            identity,
            _record(
                "mangadex_releases",
                identity,
                heading,
                summary if isinstance(summary, str) else "",
                collected_at,
                fields,
                observed_at=observed,
            ),
        )
    return filter_media_records(config, list(records.values()))


async def _json(
    fetch_bytes: Callable[..., Awaitable[Fetched]], url: str, *, body: bytes | None = None
) -> object:
    """Fetch one fixed-host bounded JSON response using the caller's SendGate callback."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "Umwelt-OS/1.0 (+https://github.com/umwelt-os)",
    }
    fetched = await fetch_bytes(url, headers=headers, max_bytes=MAX_BYTES, json_body=body)
    if fetched.body is None:
        return None
    try:
        return json.loads(fetched.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("media_response_invalid") from exc


async def collect_ann_news(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    collected_at: datetime,
    config: Mapping[str, Any],
) -> list[IngestionRecord]:
    """Fetch the fixed ANN Atom source and map its bounded entries."""
    fetched = await fetch_bytes(
        build_ann_news_url(config),
        headers={"Accept": "application/atom+xml, application/xml, text/xml"},
        max_bytes=news.MAX_FEED_BYTES,
    )
    return (
        []
        if fetched.body is None
        else map_ann_news(fetched.body, collected_at=collected_at, config=config)
    )


async def collect_ann_encyclopedia(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    media_type: str,
    collected_at: datetime,
    config: Mapping[str, Any],
    offset: int = 0,
) -> CursorPage:
    """Fetch one report page; return its next offset for atomic persistence with the records."""
    url = build_ann_encyclopedia_url(media_type, offset=offset)
    fetched = await fetch_bytes(
        url,
        headers={
            "Accept": "application/xml, text/xml",
            "User-Agent": "Umwelt-OS/1.0 (+https://github.com/umwelt-os)",
        },
        max_bytes=news.MAX_FEED_BYTES,
    )
    if fetched.body is None:
        return CursorPage((), None)
    records = map_ann_encyclopedia(
        fetched.body,
        media_type=media_type,
        collected_at=collected_at,
        config=config,
    )
    items = [
        node
        for node in news.safe_xml_root(fetched.body, max_bytes=news.MAX_FEED_BYTES).iter()
        if news._local(node.tag) == "item"
    ]
    next_offset = str(offset + len(items)) if len(items) == MAX_ITEMS else None
    return CursorPage(tuple(records), next_offset)


async def collect_mangaupdates_releases(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    collected_at: datetime,
    config: Mapping[str, Any],
    page: int = 0,
) -> CursorPage:
    """Fetch one POST search page plus one GET releases page; caller commits cursor with records."""
    if type(page) is not int or not 0 <= page <= 100_000:
        raise ValueError("mangaupdates_page_invalid")
    releases = await _json(
        fetch_bytes,
        MANGAUPDATES_RELEASE_SEARCH_URL,
        body=mangaupdates_release_search_body(page=page),
    )
    records = map_mangaupdates_releases(
        releases,
        collected_at=collected_at,
        config=config,
    )
    rows = releases.get("results", []) if isinstance(releases, Mapping) else []
    total = releases.get("total_hits") if isinstance(releases, Mapping) else None
    per_page = releases.get("per_page", MAX_ITEMS) if isinstance(releases, Mapping) else MAX_ITEMS
    if type(total) is int and type(per_page) is int and total > (page + 1) * per_page and not rows:
        raise CollectionIncomplete("mangaupdates_release_page_incomplete")
    has_more = type(total) is int and type(per_page) is int and total > (page + 1) * per_page
    if type(total) is not int and len(rows) == MAX_ITEMS:
        has_more = True
    return CursorPage(tuple(records), str(page + 1) if has_more else None)


def _mangadex_cursor(cursor: str | None) -> tuple[int, int]:
    """Parse bounded manga/chapter offsets; malformed saved state restarts from the first page."""
    if cursor:
        try:
            manga_offset, chapter_offset = (int(part) for part in cursor.split(":", 1))
            if 0 <= manga_offset <= 1_000_000 and 0 <= chapter_offset <= 1_000_000:
                return manga_offset, chapter_offset
        except (ValueError, TypeError):
            pass
    return 0, 0


async def collect_mangadex_releases(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    collected_at: datetime,
    config: Mapping[str, Any],
    cursor: str | None = None,
) -> CursorPage:
    """Fetch one manga and one chapter page; caller commits the next cursor with accepted records."""
    manga_offset, chapter_offset = _mangadex_cursor(cursor)
    if config.get("manga_ids"):
        manga_offset = 0
    payload = await _json(fetch_bytes, build_mangadex_manga_url(config, offset=manga_offset))
    manga = map_mangadex_manga(payload)
    if not manga:
        return CursorPage((), None)
    chapters = await _json(
        fetch_bytes, build_mangadex_chapters_url(config, manga, offset=chapter_offset)
    )
    records = map_mangadex_chapters(chapters, manga=manga, collected_at=collected_at, config=config)
    manga_rows = payload.get("data", []) if isinstance(payload, Mapping) else []
    chapter_rows = chapters.get("data", []) if isinstance(chapters, Mapping) else []
    manga_meta = payload.get("total") if isinstance(payload, Mapping) else None
    chapter_meta = chapters.get("total") if isinstance(chapters, Mapping) else None
    if type(manga_meta) is int and manga_meta > manga_offset and not manga_rows:
        raise CollectionIncomplete("mangadex_manga_page_incomplete")
    if type(chapter_meta) is int and chapter_meta > chapter_offset and not chapter_rows:
        raise CollectionIncomplete("mangadex_chapter_page_incomplete")
    more_chapters = (
        type(chapter_meta) is int and chapter_meta > chapter_offset + len(chapter_rows)
    ) or (len(chapter_rows) == MAX_ITEMS and type(chapter_meta) is not int)
    more_manga = not config.get("manga_ids") and (
        (type(manga_meta) is int and manga_meta > manga_offset + len(manga_rows))
        or (len(manga_rows) == MAX_ITEMS and type(manga_meta) is not int)
    )
    if more_chapters:
        next_cursor = f"{manga_offset}:{chapter_offset + len(chapter_rows)}"
    elif more_manga:
        next_cursor = f"{manga_offset + len(manga_rows)}:0"
    else:
        next_cursor = None
    return CursorPage(tuple(records), next_cursor)
