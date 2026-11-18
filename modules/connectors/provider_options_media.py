"""Media-provider option validators split out of ``provider_options`` (pure, I/O-free).

``provider_options`` re-exports every name here, so existing imports keep working. Shared literals and
helpers are imported lazily inside the functions that need them because ``provider_options`` imports this
module at load time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, get_args


def _validate_media_provider_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Reject incomplete or incompatible media options before persistence.

    TMDB certification on the ID-only changes mode is invalid because that response cannot apply certification
    without detail-request fan-out.
    """
    if provider == "ann_news":
        mode = cfg.get("ann_mode") or "news"
        if mode == "news" and cfg.get("ann_media_type") is not None:
            raise ValueError("ANN media type is only valid in encyclopedia mode")
    elif provider == "mangadex_releases":
        languages = set(cfg.get("original_languages") or ())
        if not languages <= {"ja", "ko", "zh", "zh-hk"}:
            raise ValueError("MangaDex original language is unsupported")
        translated = set(cfg.get("available_translated_languages") or ())
        if not translated <= {"en", "ja", "ko", "zh", "zh-hk", "vi", "fr", "de", "es"}:
            raise ValueError("MangaDex translated language is unsupported")
    elif provider == "youtube_api":
        validate_youtube_api_options(cfg)
    elif provider in {"steam_releases", "igdb_games"}:
        _validate_game_options(provider, cfg)
    elif provider in {"podcast_rss", "itunes_movies"}:
        _validate_rss_movie_options(provider, cfg)
    elif provider in {"tmdb_movies", "tmdb_tv", "trakt_media", "omdb_lookup"}:
        _validate_keyed_media_provider_options(provider, cfg)
    elif provider in {"mal_anime", "mal_manga"}:
        validate_mal_target(provider, cfg)
    elif provider == "google_books":
        validate_google_books_target(cfg)


def _validate_rss_movie_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Validate podcast source exclusivity or the required iTunes search term."""
    if provider == "podcast_rss":
        if bool(cfg.get("feed_url")) == bool(cfg.get("itunes_podcast_id")):
            raise ValueError("Choose exactly one podcast feed URL or iTunes podcast ID")
        feed_url = cfg.get("feed_url")
        if feed_url is not None and not str(feed_url).startswith("https://"):
            raise ValueError("Podcast feed URLs must use HTTPS")
    else:
        terms = cfg.get("search_terms")
        if not isinstance(terms, str) or not 1 <= len(terms.strip()) <= 100:
            raise ValueError("iTunes movie search terms are required")


def validate_youtube_api_options(cfg: Mapping[str, Any]) -> None:
    """Prevent filters that depend on channel metadata in direct playlist scope."""
    if cfg.get("youtube_playlist_ids") and (
        cfg.get("youtube_filter") not in (None, "all") or cfg.get("media_countries")
    ):
        raise ValueError("YouTube format and country filters require a channel or handle")


def validate_podcast_index_options(cfg: Mapping[str, Any]) -> None:
    """Validate the documented recent-feed and term-search query shapes before storing a scope."""
    from modules.connectors.provider_options import clean_query

    query = cfg.get("podcast_index_query")
    recent = ("podcast_index_languages", "podcast_index_categories", "podcast_index_excluded_categories", "podcast_index_since")
    if query is not None:
        if clean_query(query) != query or any(cfg.get(key) is not None for key in recent):
            raise ValueError("Podcast Index term search cannot use recent feed options")
    elif cfg.get("podcast_index_value") is not None or cfg.get("content_ratings"):
        raise ValueError("Podcast Index value type and explicit rating filters require a search term")
    languages = cfg.get("podcast_index_languages") or ()
    if len(set(languages)) != len(languages) or any(not re.fullmatch(r"[a-z]{2}|unknown", lang) for lang in languages):
        raise ValueError("Podcast Index languages must be distinct two-letter codes or unknown")
    for key in ("podcast_index_categories", "podcast_index_excluded_categories"):
        if (value := cfg.get(key)) is not None and not re.fullmatch(r"[A-Za-z0-9 &'-]+(?:,[A-Za-z0-9 &'-]+)*", value):
            raise ValueError("Podcast Index categories must be comma-separated names or IDs")


def validate_listenbrainz_options(cfg: Mapping[str, Any]) -> None:
    """Bound ListenBrainz scope and prevent Lucene syntax from adding unrequested search clauses."""
    tags = cfg.get("listenbrainz_tags") or ()
    if len(set(tags)) != len(tags) or any(not re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", tag) for tag in tags):
        raise ValueError("ListenBrainz tags must be distinct plain text values")


def validate_steam_options(cfg: Mapping[str, Any]) -> None:
    """Bound Steam detail fanout and enforce the selected featured or news request shape."""
    appids = cfg.get("steam_appids") or ()
    if len(set(appids)) != len(appids):
        raise ValueError("Steam app IDs must be distinct")
    mode = cfg.get("steam_mode") or "featured"
    if mode == "news" and not appids:
        raise ValueError("Steam news mode requires app IDs")
    if mode == "featured" and appids:
        raise ValueError("Steam app IDs require news mode")


def validate_igdb_options(cfg: Mapping[str, Any]) -> None:
    """Reject duplicate IGDB selectors and require a bounded game type, region, or platform scope."""
    for key in ("igdb_game_types", "igdb_platforms"):
        if len(set(cfg.get(key) or ())) != len(cfg.get(key) or ()):
            raise ValueError("IGDB selectors must be distinct")
    if not any(cfg.get(key) for key in ("igdb_game_types", "igdb_region", "igdb_platforms")):
        raise ValueError("IGDB requires a game type, release region, or platform")


def _validate_game_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Dispatch game-only selectors to their source-specific bounds and mutual-exclusion rules."""
    (validate_steam_options if provider == "steam_releases" else validate_igdb_options)(cfg)


def validate_mal_target(provider: str, cfg: Mapping[str, Any]) -> None:
    """Reject ranking and season targets incompatible with the selected MAL v2 endpoint."""
    from modules.connectors.provider_options import (
        AnimeSeason,
        MalAnimeRanking,
        MalMangaRanking,
    )

    mode = cfg.get("mal_mode", "ranking")
    ranking = cfg.get("mal_ranking_type")
    year, season = cfg.get("mal_season_year"), cfg.get("mal_season")
    rankings = get_args(MalAnimeRanking if provider == "mal_anime" else MalMangaRanking)
    if mode in (None, "ranking"):
        if year is not None or season is not None or (ranking is not None and ranking not in rankings):
            raise ValueError("MAL ranking target is invalid")
    elif mode == "season" and provider == "mal_anime":
        if type(year) is not int or not 1917 <= year <= 2100 or season not in get_args(AnimeSeason) or ranking is not None:
            raise ValueError("MAL anime season requires a season and year without a ranking type")
    else:
        raise ValueError("MAL season collection is only available for anime")


def validate_google_books_target(cfg: Mapping[str, Any]) -> None:
    """Require bounded query or subject text and validate Google Books search filter modes."""
    from modules.connectors.provider_options import _CONTROL

    query, subject = cfg.get("google_books_query"), cfg.get("google_books_subject")
    if query is None and subject is None:
        raise ValueError("Google Books requires a query or subject")
    for value, limit in ((query, 160), (subject, 100)):
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > limit or _CONTROL.search(value)
        ):
            raise ValueError("Google Books query or subject is invalid")
    if isinstance(subject, str) and ('"' in subject or "\\" in subject):
        raise ValueError("Google Books subject must be plain text")
    if cfg.get("google_books_print_type") not in (None, "all", "books", "magazines"):
        raise ValueError("Google Books print type is unsupported")
    if cfg.get("google_books_filter") not in (None, "ebooks", "free-ebooks", "paid-ebooks"):
        raise ValueError("Google Books ebook filter is unsupported")


def _validate_keyed_media_provider_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Validate endpoint modes and required targets for keyed movie, TV, Trakt, and OMDb providers."""
    if provider in {"tmdb_movies", "tmdb_tv"}:
        validate_tmdb_options(provider, cfg)
    elif provider == "trakt_media":
        mode = cfg.get("trakt_mode") or "movies_trending"
        if mode not in {"movies_trending", "movies_anticipated", "shows_trending", "shows_anticipated", "movies_calendar", "shows_calendar", "movies_search", "shows_search"}:
            raise ValueError("Trakt mode is unsupported")
        if mode.endswith("_search") and not str(cfg.get("trakt_query") or "").strip():
            raise ValueError("Trakt search requires a query")
        if not mode.endswith("_search") and cfg.get("trakt_query"):
            raise ValueError("Trakt query is only valid in search mode")
        days = cfg.get("trakt_calendar_days")
        if days is not None and (type(days) is not int or not 1 <= days <= 31):
            raise ValueError("Trakt calendar days must be between 1 and 31")
        if days is not None and not mode.endswith("_calendar"):
            raise ValueError("Trakt calendar days require calendar mode")
    elif provider == "omdb_lookup":
        mode = cfg.get("omdb_mode") or "title"
        key = {"imdb_id": "omdb_imdb_id", "title": "omdb_title", "search": "omdb_search"}.get(mode)
        value = cfg.get(key) if key else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("OMDb selected lookup field is required")
        if mode == "imdb_id" and not re.fullmatch(r"tt\d{7,12}", value):
            raise ValueError("OMDb IMDb ID is invalid")
        if cfg.get("omdb_type") not in (None, "movie", "series", "episode"):
            raise ValueError("OMDb type is unsupported")
        validate_omdb_search_filters(cfg)


TMDB_DISCOVER_OPTIONS = (
    "media_country", "media_countries", "media_genres", "media_languages",
    "release_year", "min_rating", "exclude_adult", "tmdb_certification", "tmdb_certification_country",
)


def validate_tmdb_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Reject modes and filters unavailable from the selected TMDB response shape."""
    mode = cfg.get("tmdb_mode")
    allowed = {"now_playing", "upcoming", "changes"} if provider == "tmdb_movies" else {"airing_today", "on_the_air"}
    if mode is not None and mode not in allowed:
        raise ValueError("TMDB mode is unsupported")
    _validate_tmdb_certification(provider, mode, cfg)
    if mode == "changes" and any(
        cfg.get(name) is not None for name in (*TMDB_DISCOVER_OPTIONS, "tmdb_language")
    ):
        raise ValueError("TMDB metadata filters are unavailable in changes mode")


def validate_omdb_search_filters(cfg: Mapping[str, Any]) -> None:
    """Reject detail-only filters for the abbreviated OMDb search response."""
    if cfg.get("omdb_mode") == "search" and any(
        cfg.get(name) is not None
        for name in ("media_country", "media_countries", "media_genres", "media_languages", "min_rating")
    ):
        raise ValueError("OMDb search does not return country, genre, language or rating details")


def _validate_tmdb_certification(provider: str, mode: object, cfg: Mapping[str, Any]) -> None:
    """Require a country for TMDB certification and reject the ID-only changes endpoint combination."""
    if not cfg.get("tmdb_certification"):
        return
    if not cfg.get("tmdb_certification_country"):
        raise ValueError("TMDB certification requires a certification country")
    if provider == "tmdb_movies" and mode == "changes":
        raise ValueError("TMDB certification is unavailable in changes mode")


def _validate_openlibrary_options(cfg: Mapping[str, Any]) -> None:
    """Require only scope options supported by the selected Open Library endpoint."""
    mode = cfg.get("openlibrary_mode") or "subject"
    subject, query = cfg.get("openlibrary_subject"), cfg.get("openlibrary_query")
    languages = cfg.get("media_languages") or ()
    if len(languages) > 1:
        raise ValueError("Open Library accepts one search language")
    if mode == "subject":
        if not subject or query or languages:
            raise ValueError(
                "Subject mode requires a subject and does not accept query or language"
            )
    elif mode == "search":
        if not query or subject:
            raise ValueError("Search mode requires a query and does not accept a subject")
    elif mode == "recent":
        if subject or query or languages or cfg.get("ebooks_only"):
            raise ValueError(
                "Recent mode does not accept subject, query, language, or ebook filters"
            )


def _validate_gutendex_options(cfg: Mapping[str, Any]) -> None:
    """Reject Gutendex author year ranges whose lower bound exceeds the upper bound."""
    start, end = cfg.get("gutendex_author_year_start"), cfg.get("gutendex_author_year_end")
    if start is not None and end is not None and start > end:
        raise ValueError("Gutendex author year start must not exceed end")
