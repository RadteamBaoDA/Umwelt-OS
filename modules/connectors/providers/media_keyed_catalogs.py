"""Pure one-GET request builders and media mappers for MAL v2 and Google Books."""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, get_args
from urllib.parse import quote, urlencode

from modules.connectors.provider_options import (
    MAL_RANKING_DEFAULTS,
    AnimeSeason,
    filter_records,
    validate_google_books_target,
    validate_mal_target,
)
from modules.connectors.providers import news
from modules.connectors.providers._media_common import (
    build_external_ids,
    build_media_identity,
    normalize_content_rating,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.macro import ProviderPayloadError, ProviderRequest
from modules.connectors.providers.media_anime_manga_sources import _record
from modules.ingestion.public import IngestionRecord

MAL_CLIENT_ID_HEADER = "X-MAL-CLIENT-ID"
GOOGLE_BOOKS_KEY_PLACEMENT = "query:key"
_MAL_LIMIT = 100
_GOOGLE_BOOKS_LIMIT = 40
_MAL_FIELDS = (
    "genres,media_type,status,rating,mean,nsfw,start_date,start_season,synopsis,main_picture"
)
_MAL_ANIME_FORMATS = frozenset({"tv", "movie", "special", "ova", "ona", "music"})
_MAL_MANGA_TYPES = {
    "manga": ("manga", "manga", "JP"),
    "manhwa": ("manhwa", "manhwa", "KR"),
    "manhua": ("manhua", "manhua", "CN"),
    "light_novel": ("light_novel", "novel", "JP"),
    "novel": ("light_novel", "novel", "JP"),
    "one_shot": ("manga", "one_shot", "JP"),
    "doujinshi": ("manga", "manga", "JP"),
}
_MAL_STATUSES = {
    "finished_airing": "completed",
    "currently_airing": "airing",
    "not_yet_aired": "upcoming",
    "finished": "completed",
    "currently_publishing": "airing",
    "not_yet_published": "upcoming",
    "on_hiatus": "hiatus",
}


def build_mal_request(provider: str, secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Build one fixed-host MAL ranking or seasonal GET and attach the redacted client ID header."""
    from modules.connectors.providers.keyed import place_secret

    validate_mal_target(provider, cfg)
    anime = provider == "mal_anime"
    # Manga ListField has no anime-only rating or start_season; requesting unknown fields is avoidable.
    fields = (
        _MAL_FIELDS if anime else _MAL_FIELDS.replace(",rating", "").replace(",start_season", "")
    )
    params = {
        "limit": min(cfg.get("max_items") or 25, _MAL_LIMIT),
        "nsfw": "true" if cfg.get("exclude_adult") is False else "false",
        "fields": fields,
    }
    if cfg.get("mal_mode") == "season":
        path = f"anime/season/{cfg['mal_season_year']}/{cfg['mal_season']}"
    else:
        path = f"{'anime' if anime else 'manga'}/ranking"
        params["ranking_type"] = cfg.get("mal_ranking_type") or MAL_RANKING_DEFAULTS[provider]
    return place_secret(
        provider,
        f"https://api.myanimelist.net/v2/{path}?{urlencode(params)}",
        MAL_CLIENT_ID_HEADER,
        secret,
    )


def _object(raw: object) -> Mapping[str, Any]:
    """Read an upstream object; a missing or malformed nested object has no fields."""
    return raw if isinstance(raw, dict) else {}


def _mal_content_rating(row: Mapping[str, Any]) -> str | None:
    """Keep the explicit flag authoritative and fall back to MAL's NSFW tier when no age rating is present."""
    nsfw = row.get("nsfw")
    if nsfw == "black":
        return "explicit"
    rating = normalize_content_rating(row.get("rating"))
    return rating or {"white": "safe", "gray": "suggestive"}.get(str(nsfw))


def _mal_record(
    provider: str, row: Mapping[str, Any], collected_at: datetime
) -> IngestionRecord | None:
    """Normalize one MAL node, inferring manga origin only from recognizable media types."""
    identifier, title = row.get("id"), row.get("title")
    if (
        type(identifier) is not int
        or identifier < 1
        or not isinstance(title, str)
        or not title.strip()
    ):
        return None
    anime = provider == "mal_anime"
    kind = str(row.get("media_type"))
    if anime:
        media_type, media_format, country = (
            "anime",
            kind if kind in _MAL_ANIME_FORMATS else None,
            None,
        )
    else:
        media_type, media_format, country = _MAL_MANGA_TYPES.get(kind, ("manga", None, None))
    release_date, release_year, observed_at = normalize_partial_date(row.get("start_date"))
    start_season = _object(row.get("start_season")) if anime else {}
    season, season_year = start_season.get("season"), start_season.get("year")
    if release_year is None and type(season_year) is int and 1800 <= season_year <= 2100:
        release_year = season_year
    picture = _object(row.get("main_picture"))
    genres = row.get("genres")
    genre_names = (
        (genre["name"] for genre in genres if isinstance(_object(genre).get("name"), str))
        if isinstance(genres, list)
        else ()
    )
    summary = (
        news.plain_text(row.get("synopsis"), 4000) if isinstance(row.get("synopsis"), str) else ""
    )
    fields = {
        "canonical_url": f"https://myanimelist.net/{'anime' if anime else 'manga'}/{identifier}",
        "media_type": media_type,
        "format": media_format,
        "country": country,
        "summary": summary or None,
        "genres": normalize_genres(genre_names),
        "content_rating": _mal_content_rating(row),
        "release_status": _MAL_STATUSES.get(str(row.get("status"))),
        "release_date": release_date,
        "release_year": release_year,
        "release_season": season if season in get_args(AnimeSeason) else None,
        "rating": normalize_rating(row.get("mean"), scale=10),
        "image_url": normalize_cover_url(picture.get("large"))
        or normalize_cover_url(picture.get("medium")),
        "artwork_aspect": "poster",
        "external_ids": build_external_ids({"mal": identifier}),
    }
    return _record(
        provider,
        build_media_identity(provider, identifier),
        title,
        summary,
        collected_at,
        {key: value for key, value in fields.items() if value is not None},
        observed_at=observed_at,
    )


def map_mal(
    provider: str,
    payload: object,
    collected_at: datetime,
    cfg: Mapping[str, Any],
) -> list[IngestionRecord]:
    """Map one bounded MAL page and enforce the canonical filters, including adult exclusion by default."""
    rows = _object(payload).get("data")
    if not isinstance(rows, list):
        raise ProviderPayloadError(f"{provider}_response_invalid")
    if len(rows) > _MAL_LIMIT:
        raise ProviderPayloadError(f"{provider}_response_oversized", "incomplete")
    records = [
        record
        for row in rows
        if (record := _mal_record(provider, _object(_object(row).get("node")), collected_at))
        is not None
    ]
    return filter_records(
        provider, {**cfg, "exclude_adult": cfg.get("exclude_adult") is not False}, records
    )


def build_google_books_request(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Build one authenticated volumes GET with Google's 40-item response limit."""
    from modules.connectors.providers.keyed import place_secret

    validate_google_books_target(cfg)
    query = (cfg.get("google_books_query") or "").strip()
    subject = (cfg.get("google_books_subject") or "").strip()
    if subject:
        # Keep a multi-word subject bound to its q operator.
        term = f'"{subject}"' if len(subject.split()) > 1 else subject
        query = f"{query} subject:{term}".strip()
    params = {"q": query, "maxResults": str(min(cfg.get("max_items") or 25, _GOOGLE_BOOKS_LIMIT))}
    languages: Sequence[str] = cfg.get("media_languages") or ()
    if len(languages) == 1 and re.fullmatch(r"[a-z]{2}", languages[0]):
        params["langRestrict"] = languages[0]
    for key, param in (("google_books_print_type", "printType"), ("google_books_filter", "filter")):
        if cfg.get(key) is not None:
            params[param] = cfg[key]
    return place_secret(
        "google_books",
        f"https://www.googleapis.com/books/v1/volumes?{urlencode(params)}",
        GOOGLE_BOOKS_KEY_PLACEMENT,
        secret,
    )


def _google_books_ids(identifier: str, raw: object) -> dict[str, str]:
    """Keep the Google volume ID and first correctly shaped ISBN-10 and ISBN-13 from a volume."""
    ids = {"google_books": identifier}
    if isinstance(raw, list):
        for row in raw:
            entry = _object(row)
            kind, value = entry.get("type"), entry.get("identifier")
            if not isinstance(value, str):
                continue
            if kind == "ISBN_10" and re.fullmatch(r"[0-9]{9}[0-9X]", value):
                ids.setdefault("isbn10", value)
            if kind == "ISBN_13" and re.fullmatch(r"[0-9]{13}", value):
                ids.setdefault("isbn13", value)
    return build_external_ids(ids)


def _google_books_record(row: Mapping[str, Any], collected_at: datetime) -> IngestionRecord | None:
    """Normalize one edition with scoped access-country, canonical categories, and safe cover artwork."""
    volume = _object(row.get("volumeInfo"))
    identifier, title = row.get("id"), volume.get("title")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    access = _object(row.get("accessInfo"))
    images = _object(volume.get("imageLinks"))
    cover = next(
        (
            url
            for key in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail")
            if (url := normalize_cover_url(images.get(key))) is not None
        ),
        None,
    )
    categories = volume.get("categories")
    # Google supplies hierarchical subjects with slashes; retain each recognizable category component.
    genres = (
        normalize_genres(
            part for value in categories if isinstance(value, str) for part in value.split("/")
        )
        if isinstance(categories, list)
        else []
    )
    release_date, release_year, observed_at = normalize_partial_date(volume.get("publishedDate"))
    pages = volume.get("pageCount")
    authors = volume.get("authors")
    summary = (
        news.plain_text(volume.get("description"), 4000)
        if isinstance(volume.get("description"), str)
        else ""
    )
    fields = {
        "canonical_url": f"https://books.google.com/books?id={quote(identifier, safe='')}",
        "media_type": "book",
        "format": "magazine"
        if volume.get("printType") == "MAGAZINE"
        else ("ebook" if _object(row.get("saleInfo")).get("isEbook") is True else None),
        "country": normalize_country(access.get("country")),
        "language": normalize_language(volume.get("language")),
        "creator": news.plain_text(
            ", ".join(author for author in authors if isinstance(author, str)), 240
        )
        if isinstance(authors, list)
        else None,
        "summary": summary or None,
        "genres": genres,
        "release_date": release_date,
        "release_year": release_year,
        "rating": normalize_rating(volume.get("averageRating"), scale=5),
        "page_count": pages if type(pages) is int and 0 <= pages <= 50000 else None,
        "image_url": cover,
        "artwork_aspect": "poster",
        "external_ids": _google_books_ids(identifier, volume.get("industryIdentifiers")),
    }
    return _record(
        "google_books",
        build_media_identity("google_books", identifier),
        title,
        summary,
        collected_at,
        {key: value for key, value in fields.items() if value is not None},
        observed_at=observed_at,
    )


def map_google_books(
    payload: object, collected_at: datetime, cfg: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map the bounded volumes page, distinguish a legitimate empty result, and apply canonical filters."""
    response = _object(payload)
    rows = response.get("items")
    if rows is None and type(response.get("totalItems")) is int and response["totalItems"] == 0:
        return []
    if not isinstance(rows, list):
        raise ProviderPayloadError("google_books_response_invalid")
    if len(rows) > _GOOGLE_BOOKS_LIMIT:
        raise ProviderPayloadError("google_books_response_oversized", "incomplete")
    records = [
        record
        for row in rows
        if (record := _google_books_record(_object(row), collected_at)) is not None
    ]
    return filter_records("google_books", cfg, records)
