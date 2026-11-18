"""Shared rate-pacing, XML, date, image, duration, and taxonomy normalization for media collectors.

Pure helpers plus cooperative async per-key rate pacing for Wave 1–3 media providers.
Nothing here opens network sockets or bypasses ``SendGate`` / ``rest.fetch_bounded``.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from core.media_proxy import MediaError, check_feed_image_url, clean_image_url
from modules.connectors.providers import news, rest

MEDIA_USER_AGENT = rest.DEFAULT_USER_AGENT
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_DURATION_SECONDS = 604800
_MIN_YEAR = 1800
_MAX_YEAR = 2100

MEDIA_EXTERNAL_ID_KEYS = frozenset(
    {
        "tmdb",
        "imdb",
        "anilist",
        "mal",
        "kitsu",
        "mangadex",
        "mangaupdates",
        "isbn13",
        "isbn10",
        "google_books",
        "hardcover",
        "openlibrary",
        "mbid",
        "tvmaze",
        "youtube",
        "steam",
        "igdb",
        "wikidata",
        "itunes",
        "trakt",
        "podcastindex",
    }
)

MEDIA_ATTRIBUTIONS: dict[str, str] = {
    "anilist_anime": "Data via AniList",
    "anilist_manga": "Data via AniList",
    "wikidata_movies": "Data via Wikidata (CC0 1.0)",
    "itunes_movies": "Data via Apple iTunes Search API",
    "letterboxd_rss": "Data via Letterboxd RSS",
    "tvmaze_schedule": "Data via TVMaze (CC BY-SA 4.0)",
    "mangadex_releases": "Metadata via MangaDex",
    "mangaupdates_releases": "Data via MangaUpdates",
    "openlibrary_books": "Data via Open Library (CC0 1.0)",
    "gutendex_books": "Data via Project Gutenberg / Gutendex",
    "listenbrainz_releases": "Data via ListenBrainz / MusicBrainz (CC0 1.0)",
    "steam_releases": "Data via Steam",
    "tmdb_movies": "Metadata by TMDB",
    "tmdb_tv": "Metadata by TMDB",
    "trakt_media": "Data via Trakt",
    "mal_anime": "Data via MyAnimeList",
    "mal_manga": "Data via MyAnimeList",
    "google_books": "Data via Google Books",
    "hardcover_books": "Data via Hardcover",
    "youtube_api": "Data via YouTube Data API",
    "podcast_index": "Data via Podcast Index",
    "omdb_lookup": "Data via OMDb API",
    "igdb_games": "Data via IGDB",
}

_COUNTRY_ALIASES: dict[str, str] = {
    "UK": "GB",
    "USA": "US",
    "GBR": "GB",
    "JPN": "JP",
    "KOR": "KR",
    "CHN": "CN",
    "TWN": "TW",
    "HKG": "HK",
    "VNM": "VN",
    "FRA": "FR",
    "DEU": "DE",
    "GER": "DE",
    "CAN": "CA",
    "AUS": "AU",
    "IND": "IN",
    "ESP": "ES",
    "ITA": "IT",
    "BRA": "BR",
    "RUS": "RU",
    "MEX": "MX",
    "THA": "TH",
    "IDN": "ID",
    "SGP": "SG",
    "MYS": "MY",
    "PHL": "PH",
    "NLD": "NL",
    "CHE": "CH",
    "IRL": "IE",
    "NZL": "NZ",
    "SWE": "SE",
    "NOR": "NO",
    "DNK": "DK",
    "FIN": "FI",
    "POL": "PL",
    "PRT": "PT",
    "BEL": "BE",
    "AUT": "AT",
    "ARG": "AR",
    "ZAF": "ZA",
    "TUR": "TR",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "JAPAN": "JP",
    "SOUTH KOREA": "KR",
    "REPUBLIC OF KOREA": "KR",
    "KOREA": "KR",
    "CHINA": "CN",
    "TAIWAN": "TW",
    "HONG KONG": "HK",
    "VIETNAM": "VN",
    "VIET NAM": "VN",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "CANADA": "CA",
    "AUSTRALIA": "AU",
    "INDIA": "IN",
    "SPAIN": "ES",
    "ITALY": "IT",
    "BRAZIL": "BR",
    "RUSSIA": "RU",
    "MEXICO": "MX",
    "THAILAND": "TH",
    "INDONESIA": "ID",
    "SINGAPORE": "SG",
    "MALAYSIA": "MY",
    "PHILIPPINES": "PH",
    "NETHERLANDS": "NL",
    "SWITZERLAND": "CH",
    "IRELAND": "IE",
    "NEW ZEALAND": "NZ",
    "SWEDEN": "SE",
    "NORWAY": "NO",
    "DENMARK": "DK",
    "FINLAND": "FI",
    "POLAND": "PL",
    "PORTUGAL": "PT",
    "BELGIUM": "BE",
    "AUSTRIA": "AT",
    "ARGENTINA": "AR",
    "SOUTH AFRICA": "ZA",
    "TURKEY": "TR",
}

_LANGUAGE_ALIASES: dict[str, str] = {
    "eng": "en",
    "english": "en",
    "vie": "vi",
    "vietnamese": "vi",
    "jpn": "ja",
    "japanese": "ja",
    "kor": "ko",
    "korean": "ko",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "schinese": "zh",
    "tchinese": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh-hk": "zh-hk",
    "zh-hant-hk": "zh-hk",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "spa": "es",
    "spanish": "es",
    "por": "pt",
    "portuguese": "pt",
    "ita": "it",
    "italian": "it",
    "rus": "ru",
    "russian": "ru",
    "ind": "id",
    "indonesian": "id",
    "tha": "th",
    "thai": "th",
}

_GENRE_ALIASES: dict[str, tuple[str, ...]] = {
    "action": ("action",),
    "adventure": ("adventure",),
    "action & adventure": ("action", "adventure"),
    "action and adventure": ("action", "adventure"),
    "action/adventure": ("action", "adventure"),
    "action_adventure": ("action",),
    "sci_fi": ("sci_fi",),
    "sci-fi": ("sci_fi",),
    "scifi": ("sci_fi",),
    "science fiction": ("sci_fi",),
    "science-fiction": ("sci_fi",),
    "science_fiction": ("sci_fi",),
    "sci-fi & fantasy": ("sci_fi", "fantasy"),
    "fantasy": ("fantasy",),
    "romance": ("romance",),
    "thriller": ("thriller",),
    "thrillers": ("thriller",),
    "psychological": ("thriller",),
    "suspense": ("thriller",),
    "horror": ("horror",),
    "comedy": ("comedy",),
    "humor": ("comedy",),
    "humour": ("comedy",),
    "drama": ("drama",),
    "mystery": ("mystery",),
    "mystery_and_detective_stories": ("mystery",),
    "detective": ("mystery",),
    "documentary": ("documentary",),
    "animation": ("animation",),
    "anime": ("animation",),
    "slice_of_life": ("slice_of_life",),
    "slice of life": ("slice_of_life",),
    "slice-of-life": ("slice_of_life",),
    "history": ("history",),
    "historical": ("history",),
    "music": ("music",),
    "sports": ("sports",),
    "sport": ("sports",),
    "non_fiction": ("non_fiction",),
    "non-fiction": ("non_fiction",),
    "nonfiction": ("non_fiction",),
    "non fiction": ("non_fiction",),
    "biography": ("non_fiction",),
    "education": ("non_fiction",),
    "programming": ("programming",),
    "computers": ("programming",),
    "technology": ("programming",),
    "software": ("programming",),
    "rpg": ("rpg",),
    "role-playing": ("rpg",),
    "role-playing (rpg)": ("rpg",),
    "strategy": ("strategy",),
}

_PROVIDER_GENRE_IDS: dict[str, dict[str, tuple[str, ...]]] = {
    "tmdb_movies": {
        "28": ("action",),
        "12": ("adventure",),
        "878": ("sci_fi",),
        "14": ("fantasy",),
        "10749": ("romance",),
        "53": ("thriller",),
        "27": ("horror",),
        "35": ("comedy",),
        "18": ("drama",),
        "9648": ("mystery",),
        "99": ("documentary",),
        "16": ("animation",),
        "36": ("history",),
        "10402": ("music",),
    },
    "tmdb_tv": {
        "10759": ("action", "adventure"),
        "10765": ("sci_fi", "fantasy"),
        "35": ("comedy",),
        "18": ("drama",),
        "9648": ("mystery",),
        "99": ("documentary",),
        "16": ("animation",),
    },
    "mangadex_releases": {
        "391b0423-d847-456f-aff0-8b0cfc03066b": ("action",),
        "87cc87cd-a395-47af-b27a-93258283bbc6": ("adventure",),
        "256c8bd9-4904-4360-bf4f-508a76d67183": ("sci_fi",),
        "cdc58593-87dd-415e-bbc0-2ec27bf404cc": ("fantasy",),
        "423e2eae-a7a2-4a8b-ac03-a8351462d71d": ("romance",),
        "07251805-a27e-4d59-b488-f0bfbec15168": ("thriller",),
        "cdad7e68-1419-41dd-bdce-27753074a640": ("horror",),
        "4d32cc48-9f00-4cca-9b5a-a839f0764984": ("comedy",),
        "b9af3a63-f058-46de-a9a0-e0c13906197a": ("drama",),
        "ee653e07-d70f-4ec3-807b-38b78795154c": ("mystery",),
        "e5301a23-ebd9-49dd-aca1-05ae9733532d": ("slice_of_life",),
        "33208764-8efb-4232-a986-0eaca8d3f8ba": ("history",),
        "69964a64-2f90-4d33-beeb-f3ed2875eb4c": ("sports",),
    },
    "steam_releases": {
        "1": ("action",),
        "25": ("adventure",),
        "81": ("documentary",),
        "18": ("sports",),
        "54": ("non_fiction",),
        "3": ("rpg",),
        "2": ("strategy",),
    },
    "podcast_index": {
        "16": ("comedy",),
        "20": ("non_fiction",),
        "26": ("sci_fi",),
        "27": ("drama",),
        "28": ("history",),
        "53": ("music",),
        "67": ("sci_fi",),
        "79": ("documentary",),
        "86": ("sports",),
        "102": ("programming",),
        "104": ("animation",),
        "105": ("animation",),
    },
}

_CONTENT_RATING_ALIASES: dict[str, str] = {
    "safe": "safe",
    "g": "safe",
    "tv-g": "safe",
    "tv-y": "safe",
    "tv-y7": "safe",
    "all": "safe",
    "everyone": "safe",
    "e": "safe",
    "pg": "pg",
    "tv-pg": "pg",
    "e10+": "pg",
    "pg_13": "pg_13",
    "pg-13": "pg_13",
    "pg13": "pg_13",
    "tv-14": "pg_13",
    "teen": "pg_13",
    "t": "pg_13",
    "r": "r",
    "tv-ma": "r",
    "nc-17": "r",
    "18+": "r",
    "suggestive": "suggestive",
    "erotica": "suggestive",
    "mature": "mature",
    "m": "mature",
    "r+": "mature",
    "explicit": "explicit",
    "rx": "explicit",
    "adult": "explicit",
    "yes": "explicit",
    "true": "explicit",
    "clean": "clean",
    "no": "clean",
    "false": "clean",
}

_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_YEAR_RE = re.compile(r"^(\d{4})$")
_QUARTER_RE = re.compile(r"^(?:Q[1-4]\s+(\d{4})|(\d{4})\s+Q[1-4])$", re.IGNORECASE)


def media_user_agent(provider: str | None = None, contact: str | None = None) -> str:
    """Build a descriptive ``User-Agent`` string for media APIs (MusicBrainz, Wikidata, Open Library).

    Control characters are stripped from optional ``provider`` and ``contact`` fragments.
    """
    clean_provider = " ".join(_CONTROL_RE.sub(" ", provider).split()) if provider else ""
    clean_contact = " ".join(_CONTROL_RE.sub(" ", contact).split()) if contact else ""
    if not clean_provider and not clean_contact:
        return MEDIA_USER_AGENT
    details = [part for part in (clean_provider, "+https://github.com/umwelt-os") if part]
    if clean_contact:
        details.append(f"mailto:{clean_contact}")
    return f"Umwelt-OS/1.0 ({'; '.join(details)})"


class MediaRatePacer:
    """Cooperative async per-key/host rate pacer using monotonic clocks and ``asyncio.sleep``."""

    def __init__(self) -> None:
        """Initialize per-key lock and last-dispatch monotonic timestamp tables."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_dispatch: dict[str, float] = {}

    async def pace(self, key: str, min_interval_seconds: float) -> float:
        """Wait until ``min_interval_seconds`` has elapsed since the previous call for ``key``.

        Returns the seconds slept (``0.0`` when no wait was necessary or interval <= 0).
        """
        if min_interval_seconds <= 0:
            return 0.0
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last_dispatch.get(key)
            wait = (min_interval_seconds - (now - last)) if last is not None else 0.0
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_dispatch[key] = time.monotonic()
            return max(0.0, wait)

    def reset(self, key: str | None = None) -> None:
        """Clear recorded dispatch timestamps for ``key`` or for all keys when ``key`` is None."""
        if key is None:
            self._last_dispatch.clear()
        else:
            self._last_dispatch.pop(key, None)


def rate_limit_delay_from_headers(headers: Mapping[str, str]) -> float | None:
    """Inspect ``Retry-After`` and ``X-RateLimit-*`` headers to compute a bounded wait in seconds.

    Returns ``None`` when no throttling signal is present; values are clamped to ``[0.0, 3600.0]``.
    """
    lowered = {k.lower(): v.strip() for k, v in headers.items()}
    retry_after = lowered.get("retry-after")
    if retry_after:
        try:
            return min(max(0.0, float(retry_after)), 3600.0)
        except ValueError:
            try:
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                delta = (target.astimezone(UTC) - datetime.now(UTC)).total_seconds()
                return min(max(0.0, delta), 3600.0)
            except (ValueError, TypeError, OverflowError):
                return 60.0
    remaining = lowered.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            rem_val = int(remaining)
        except ValueError:
            rem_val = 1
        if rem_val <= 0:
            reset_raw = lowered.get("x-ratelimit-reset")
            if reset_raw:
                try:
                    reset_num = float(reset_raw)
                    # Epoch timestamps (>1e9) convert to delta from wall clock; small numbers are deltas.
                    delay = reset_num - time.time() if reset_num > 1_000_000_000 else reset_num
                    return min(max(0.0, delay), 3600.0)
                except ValueError:
                    return 60.0
            return 60.0
    return None


def parse_media_xml(body: object, *, max_bytes: int = 2 * 1024 * 1024) -> ET.Element:
    """Parse bounded UTF-8 media XML/Atom/RSS safely via ``providers.news.safe_xml_root``."""
    return news.safe_xml_root(body, max_bytes=max_bytes)


def xml_local(tag: object) -> str:
    """Strip an XML namespace prefix from ``tag``; non-strings yield an empty string."""
    return news._local(tag)


def xml_child_text(el: ET.Element, *names: str) -> str | None:
    """Return the first non-blank direct child text among ``names`` (namespace-insensitive)."""
    return news._child_text(el, *names)


def xml_atom_link(el: ET.Element) -> str | None:
    """Return the first alternate ``<link>`` ``href`` on an Atom entry, or ``None``."""
    return news._atom_link(el)


def _valid_year(year: int) -> bool:
    """Return True when ``year`` sits inside the supported media range ``[1800, 2100]``."""
    return _MIN_YEAR <= year <= _MAX_YEAR


def normalize_partial_date(raw: object) -> tuple[str | None, int | None, datetime | None]:
    """Normalize ISO-8601, RFC 2822, partial ``YYYY``/``YYYY-MM``, or ``Q1-Q4 YYYY`` strings.

    Returns ``(iso_string, year, utc_datetime)``; quarter strings yield ``(None, year, None)``
    because they carry no exact calendar day, and unparseable inputs yield ``(None, None, None)``.
    """
    if not isinstance(raw, str):
        return None, None, None
    text = raw.strip()
    if not text:
        return None, None, None
    if m := _QUARTER_RE.fullmatch(text):
        year = int(m.group(1) or m.group(2))
        return (None, year, None) if _valid_year(year) else (None, None, None)
    if m := _ISO_YEAR_RE.fullmatch(text):
        year = int(m.group(1))
        if not _valid_year(year):
            return None, None, None
        return text, year, datetime(year, 1, 1, tzinfo=UTC)
    if m := _ISO_YM_RE.fullmatch(text):
        year, month = int(m.group(1)), int(m.group(2))
        if not _valid_year(year) or not 1 <= month <= 12:
            return None, None, None
        return text, year, datetime(year, month, 1, tzinfo=UTC)
    if m := _ISO_DATE_RE.fullmatch(text):
        try:
            parsed_d = date.fromisoformat(text)
        except ValueError:
            return None, None, None
        if not _valid_year(parsed_d.year):
            return None, None, None
        return (
            text,
            parsed_d.year,
            datetime(parsed_d.year, parsed_d.month, parsed_d.day, tzinfo=UTC),
        )
    return _normalize_full_datetime(text)


def _normalize_full_datetime(text: str) -> tuple[str | None, int | None, datetime | None]:
    """Parse RFC 3339 or RFC 2822 timestamps into UTC ``(iso_z, year, utc_datetime)``."""
    dt: datetime | None = None
    if _ISO_TS_RE.fullmatch(text):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    else:
        try:
            dt = parsedate_to_datetime(text)
        except (ValueError, TypeError, IndexError, OverflowError):
            dt = None
    if dt is None:
        return None, None, None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    if not _valid_year(dt.year):
        return None, None, None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt.year, dt


def normalize_cover_url(raw: object) -> str | None:
    """Upgrade ``http://`` to ``https://`` and validate cover artwork for feed proxying.

    Returns ``None`` if the URL fails ``clean_image_url`` or ``check_feed_image_url``.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.lower().startswith("http://"):
        candidate = "https://" + candidate[7:]
    cleaned = clean_image_url(candidate)
    if cleaned is None:
        return None
    try:
        return check_feed_image_url(cleaned)
    except MediaError:
        return None


def parse_itunes_duration(raw: object) -> int | None:
    """Parse ``HH:MM:SS``, ``MM:SS``, or numeric seconds into bounded ``[0, 604800]`` ints."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        if not math.isfinite(float(raw)) or raw < 0 or raw > _MAX_DURATION_SECONDS:
            return None
        return int(raw)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if ":" not in text:
        try:
            seconds = float(text)
        except ValueError:
            return None
        if not math.isfinite(seconds) or seconds < 0 or seconds > _MAX_DURATION_SECONDS:
            return None
        return int(seconds)
    parts = text.split(":")
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        mins, secs = nums
        if secs >= 60:
            return None
        total = mins * 60 + secs
    else:
        hrs, mins, secs = nums
        if mins >= 60 or secs >= 60:
            return None
        total = hrs * 3600 + mins * 60 + secs
    return total if 0 <= total <= _MAX_DURATION_SECONDS else None


def normalize_rating(raw: object, *, scale: Literal[5, 10, 100] = 100) -> float | None:
    """Scale a finite numeric rating from ``[0, scale]`` onto ``[0.0, 100.0]``."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0 or value > float(scale):
        return None
    return round(value * (100.0 / float(scale)), 2)


def normalize_country(raw: object) -> str | None:
    """Normalize ISO2, ISO3, ``UK``, or common English country names to uppercase ISO alpha-2."""
    if not isinstance(raw, str):
        return None
    token = " ".join(raw.strip().upper().split())
    if not token:
        return None
    if token in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[token]
    if re.fullmatch(r"[A-Z]{2}", token):
        return token
    return None


def normalize_language(raw: object) -> str | None:
    """Normalize ISO 639-1, BCP-47, ISO 639-2/B, and full English names to lowercase codes."""
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower().replace("_", "-")
    if not token:
        return None
    if token in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[token]
    if re.fullmatch(r"[a-z]{2}-[a-z0-9]{2,8}", token):
        return token
    if re.fullmatch(r"[a-z]{2}", token):
        return token
    return None


def normalize_genres(raw_values: Iterable[str], *, provider: str | None = None) -> list[str]:
    """Map upstream genre labels, slugs, or IDs to at most 12 unique canonical ``MediaGenre`` slugs."""
    provider_map = _PROVIDER_GENRE_IDS.get(provider or "", {})
    seen: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        cleaned = " ".join(raw.strip().lower().split())
        if not cleaned:
            continue
        mapped = provider_map.get(cleaned) or _GENRE_ALIASES.get(cleaned)
        if mapped is None:
            continue
        for slug in mapped:
            if slug not in seen:
                seen.append(slug)
                if len(seen) >= 12:
                    return seen
    return seen


def normalize_content_rating(raw: object) -> str | None:
    """Map upstream maturity/explicit strings or booleans onto canonical ``ContentRating`` values."""
    if isinstance(raw, bool):
        return "explicit" if raw else "clean"
    if not isinstance(raw, str):
        return None
    token = " ".join(raw.strip().lower().split())
    if not token:
        return None
    return _CONTENT_RATING_ALIASES.get(token)


def build_media_identity(provider: str, external_id: object, *, scope: str | None = None) -> str:
    """Build a bounded stable identity ``provider:[scope:]external_id`` (SHA-256 fallback > 480 chars)."""
    ext = str(external_id).strip()
    raw = f"{provider}:{scope.strip()}:{ext}" if scope and scope.strip() else f"{provider}:{ext}"
    return news._stable_id(raw)


def build_external_ids(mapping: Mapping[str, object]) -> dict[str, str]:
    """Filter ``mapping`` to allowlisted cross-provider ID keys with <= 64-char non-blank values."""
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if key not in MEDIA_EXTERNAL_ID_KEYS or value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text or len(text) > 64:
            continue
        out[key] = text
        if len(out) >= 16:
            break
    return out
