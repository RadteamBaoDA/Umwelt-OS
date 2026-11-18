"""Bounded YouTube Data API and Podcast Index collectors using the caller's gated transport."""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1, sha256
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from modules.connectors.provider_options import (
    MEDIA_OPTION_NAMES,
    filter_records,
    native_fingerprint,
    validate_podcast_index_options,
    validate_youtube_api_options,
)
from modules.connectors.providers import rest
from modules.connectors.providers._media_common import (
    build_external_ids,
    normalize_content_rating,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
)
from modules.connectors.providers.feed_catalog import _youtube_targets, youtube_query_fingerprint
from modules.connectors.providers.media_anime_manga_sources import _record
from modules.connectors.public import ProviderRateLimited
from modules.connectors.quota import window_bounds
from modules.connectors.service.schemas import ConnectorConfig
from modules.ingestion.public import IngestionRecord

FetchBytes = Callable[..., Awaitable[rest.Fetched]]

YOUTUBE_API_HEADER = "X-Goog-Api-Key"
_YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
_PODCAST_API = "https://api.podcastindex.org/api/1.0"
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
_PLAYLIST = re.compile(r"[A-Za-z0-9_-]{10,64}")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")
_DURATION = re.compile(r"P(?:(\d{1,5})D)?T?(?:(\d{1,5})H)?(?:(\d{1,5})M)?(?:(\d{1,5})S)?")
_YOUTUBE_PAGES = 2
_YOUTUBE_PAGE_SIZE = 25
_PODCAST_LIMIT = 50


@dataclass(frozen=True)
class MediaNativePage:
    """Candidate records and continuation that must be accepted in one receipt."""

    records: tuple[IngestionRecord, ...]
    coverage: Literal["returned_snapshot", "truncated"]
    cursor_after: str


def _object(value: object) -> dict[str, Any]:
    """Read a provider object without assuming optional nested fields are present."""
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    """Discard non-text provider values."""
    return value if isinstance(value, str) else ""


def _decode(response: rest.Fetched) -> dict[str, Any]:
    """Require a JSON object and discard decoding details that may echo provider content."""
    try:
        payload = json.loads(response.body or b"")
    except (ValueError, UnicodeError):
        raise rest.RestSchemaChanged("media_response_invalid") from None
    if not isinstance(payload, dict):
        raise rest.RestSchemaChanged("media_response_invalid")
    return payload


def _rows(payload: Mapping[str, Any], field: str, maximum: int) -> list[dict[str, Any]]:
    """Bound response cardinality and fail closed on a malformed response envelope."""
    values = payload.get(field)
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise rest.RestSchemaChanged("media_response_invalid")
    if len(values) > maximum:
        raise rest.CollectionIncomplete
    return values


def _state(cursor: str | None, fingerprint: str) -> dict[str, Any]:
    """Load only bounded matching cursor state for this exact query."""
    if not cursor or len(cursor) > 4096:
        return {}
    try:
        payload = _object(json.loads(cursor))
    except ValueError:
        return {}
    return payload if payload.get("scope") == fingerprint else {}


def _etag(value: object) -> str | None:
    """Permit only bounded printable ASCII validators that fit atomically in the cursor."""
    return (
        value
        if isinstance(value, str)
        and 0 < len(value) <= 256
        and "\\" not in value
        and re.fullmatch(r'(?:W/)?"[!#-~]*"', value)
        else None
    )


def _youtube_record(
    row: Mapping[str, Any], expected_channel: str | None, country: str | None, now: datetime
) -> IngestionRecord:
    """Map one requested video to the Atom-compatible channel/video identity and bounded metadata."""
    snippet = _object(row.get("snippet"))
    video, channel, title = (
        _text(row.get("id")),
        _text(snippet.get("channelId")),
        _text(snippet.get("title")),
    )
    if not _VIDEO.fullmatch(video) or not _CHANNEL.fullmatch(channel) or not title:
        raise rest.RestSchemaChanged("youtube_video_invalid")
    if expected_channel is not None and expected_channel != channel:
        raise rest.RestSchemaChanged("youtube_channel_mismatch")
    date, year, released = normalize_partial_date(snippet.get("publishedAt"))
    details = _object(row.get("contentDetails"))
    language = normalize_language(snippet.get("defaultAudioLanguage")) or normalize_language(
        snippet.get("defaultLanguage")
    )
    fields: dict[str, object] = {
        "media_type": "youtube",
        "artwork_aspect": "video",
        "canonical_url": f"https://www.youtube.com/watch?v={video}",
        "image_url": f"https://i.ytimg.com/vi/{video}/mqdefault.jpg",
        "release_status": "upcoming" if released and released > now else "released",
        "external_ids": build_external_ids({"youtube": video}),
    }
    if date is not None and year is not None:
        fields.update(release_date=date, release_year=year)
    if creator := _text(snippet.get("channelTitle")):
        fields["creator"] = creator[:240]
    if country:
        fields["country"] = country
    if language:
        fields["language"] = language
    if match := _DURATION.fullmatch(_text(details.get("duration"))):
        duration = sum(
            int(number or 0) * unit
            for number, unit in zip(match.groups(), (86400, 3600, 60, 1), strict=True)
        )
        if duration <= 604800:
            fields["duration_seconds"] = duration
    if _object(details.get("contentRating")).get("ytRating") == "ytAgeRestricted":
        fields["content_rating"] = "mature"
    return _record(
        "youtube_api",
        f"youtube:{channel}:{video}",
        title,
        _text(snippet.get("description")),
        now,
        fields,
        observed_at=released,
    )


def _youtube_quota_error(body: bytes) -> None:
    """Map Google's known 403 quota codes to retry deadlines without exposing response text."""
    try:
        error = _object(_object(json.loads(body)).get("error"))
    except ValueError:
        return
    errors = error.get("errors")
    if not isinstance(errors, list):
        return
    # A permission denial still reaches the transport's normal invalid-credential classification.
    reasons = {_text(_object(item).get("reason")) for item in errors}
    now = datetime.now(UTC)
    if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        raise ProviderRateLimited(window_bounds("day_pacific", now)[1])
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        raise ProviderRateLimited(now + timedelta(minutes=1))


class _InvalidYouTubePageToken(rest.ProviderHttpError):
    """Typed, redacted rejection of a YouTube playlist continuation."""


def _youtube_page_token_error(body: bytes) -> None:
    """Recognize Google's exact invalidPageToken error without exposing the response body."""
    try:
        details = _object(_object(json.loads(body)).get("error")).get("errors")
    except (ValueError, UnicodeError):
        return
    if isinstance(details, list) and any(_object(error).get("reason") == "invalidPageToken" for error in details):
        raise _InvalidYouTubePageToken(400)


async def _youtube_fetch(
    fetch: FetchBytes, key: str, endpoint: str, params: dict[str, str], etag: str | None = None
) -> rest.Fetched:
    """Send one one-unit API GET to the fixed host with a header-only key and no redirects."""
    headers = {"Accept": "application/json", YOUTUBE_API_HEADER: key}
    if etag:
        headers["If-None-Match"] = etag
    result = await fetch(
        f"{_YOUTUBE_API}/{endpoint}?{urlencode(params)}",
        headers=headers,
        max_bytes=512_000,
        allow_redirects=False,
        on_forbidden=_youtube_quota_error,
        on_bad_request=_youtube_page_token_error if "pageToken" in params else None,
    )
    if result.body is None and not etag:
        raise rest.RestSchemaChanged("unexpected_not_modified")
    return result


async def _youtube_video_records(
    fetch: FetchBytes, key: str, items: list[dict[str, Any]], channel: str | None, country: str | None, now: datetime,
) -> tuple[list[IngestionRecord], bool]:
    """Enrich one complete playlist page and report whether any requested video was omitted."""
    videos = list(dict.fromkeys(
        _text(_object(item.get("contentDetails")).get("videoId")) for item in items
    ))
    if any(not _VIDEO.fullmatch(video) for video in videos):
        raise rest.RestSchemaChanged("youtube_playlist_item_invalid")
    if not videos:
        return [], True
    details = await _youtube_fetch(
        fetch, key, "videos", {"part": "snippet,contentDetails", "id": ",".join(videos)}
    )
    seen: set[str] = set()
    records = []
    for row in _rows(_decode(details), "items", len(videos)):
        video = _text(row.get("id"))
        if video not in videos or video in seen:
            raise rest.RestSchemaChanged("youtube_video_outside_scope")
        seen.add(video)
        records.append(_youtube_record(row, channel, country, now))
    return records, len(seen) == len(videos)


async def _youtube_uploads(
    fetch: FetchBytes,
    key: str,
    playlist: str,
    channel: str | None,
    country: str | None,
    now: datetime,
    prior_etag: str | None,
    resume_token: str,
) -> tuple[list[IngestionRecord], str | None, str | None]:
    """Read up to two playlist requests and return complete records, the head ETag, and unread token.

    An invalid saved token spends one request, leaving one for an unconditional restart.
    Other failures propagate before records and continuation can be accepted.
    """
    records = []
    page_token = resume_token
    returned_etag = prior_etag if resume_token else None
    tokens = {resume_token}
    for attempt in range(_YOUTUBE_PAGES):
        params = {
            "part": "contentDetails",
            "playlistId": playlist,
            "maxResults": str(_YOUTUBE_PAGE_SIZE),
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = await _youtube_fetch(
                fetch, key, "playlistItems", params, prior_etag if not page_token else None
            )
        except _InvalidYouTubePageToken:
            if attempt != 0 or not resume_token:
                raise
            # Do not reuse a head validator from a failed walk, or debit beyond this run's cap.
            page_token = ""
            prior_etag = returned_etag = None
            tokens.clear()
            continue
        if response.body is None:
            return [], prior_etag, None
        if not page_token:
            returned_etag = _etag(response.etag)
        payload = _decode(response)
        mapped, complete = await _youtube_video_records(
            fetch, key, _rows(payload, "items", _YOUTUBE_PAGE_SIZE), channel, country, now
        )
        records.extend(mapped)
        if not complete:
            # A missing private/deleted video must not validate the head for subsequent syncs.
            returned_etag = None
        raw_next = payload.get("nextPageToken")
        if raw_next is None:
            return records, returned_etag, None
        if (
            not isinstance(raw_next, str)
            or not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", raw_next)
            or raw_next in tokens
        ):
            raise rest.RestSchemaChanged("youtube_page_token_invalid")
        tokens.add(raw_next)
        page_token = raw_next
    # A bounded run may finish on a complete page while the playlist still has unread pages.
    return records, returned_etag, page_token


async def collect_youtube_api(
    config: ConnectorConfig,
    key: str,
    now: datetime,
    fetch: FetchBytes,
    cursor: str | None,
) -> MediaNativePage:
    """Collect up to 40 units and 400 upload candidates with a scope-bound continuation.

    The caller checkpoints complete pages and continuation tokens with their records. The saved
    head ETag becomes conditional again only after the remaining playlist pages are read.
    """
    cfg = config.model_dump(exclude_none=True)
    validate_youtube_api_options(cfg)
    filters = {name: cfg[name] for name in MEDIA_OPTION_NAMES & cfg.keys()}
    fingerprint = sha256(
        json.dumps([youtube_query_fingerprint(config), filters], sort_keys=True).encode()
    ).hexdigest()[:24]
    fp = "youtube-api:" + fingerprint
    prior = _object(_state(cursor, fp).get("etags"))
    validators: dict[str, dict[str, str]] = {}
    records: dict[str, IngestionRecord] = {}
    truncated = False
    for target in _youtube_targets(config):
        channel, country = target.channel_id, None
        playlist = (
            (parse_qs(urlsplit(target.url).query).get("playlist_id") or [""])[0]
            if target.url
            else ""
        )
        if target.handle or channel:
            params = {"part": "id,snippet,contentDetails"}
            if target.handle:
                params["forHandle"] = "@" + target.handle.removeprefix("@")
            elif channel:
                params["id"] = channel
            matches = _rows(
                _decode(await _youtube_fetch(fetch, key, "channels", params)), "items", 1
            )
            row = matches[0] if matches else {}
            resolved = _text(row.get("id"))
            if not _CHANNEL.fullmatch(resolved) or (channel is not None and resolved != channel):
                raise rest.RestSchemaChanged("youtube_channel_unresolved")
            channel = resolved
            country = normalize_country(_object(row.get("snippet")).get("country"))
            playlists = _object(_object(row.get("contentDetails")).get("relatedPlaylists"))
            kind = config.youtube_filter or "all"
            playlist = (
                _text(playlists.get("uploads"))
                if kind == "all"
                else ("UULF" if kind == "long_form" else "UUSH") + channel[2:]
            )
        if not _PLAYLIST.fullmatch(playlist):
            raise rest.RestSchemaChanged("youtube_uploads_playlist_invalid")
        route = sha256(f"{channel or ''}:{playlist}".encode()).hexdigest()[:16]
        previous = _object(prior.get(route))
        saved_token = _text(previous.get("page_token"))
        resume_token = saved_token if re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", saved_token) else ""
        # Old truncated checkpoints had no continuation, so their ETag must not block a fresh walk.
        prior_etag = None if previous.get("truncated") is True or resume_token != saved_token else _etag(previous.get("etag"))
        mapped, etag, next_token = await _youtube_uploads(
            fetch,
            key,
            playlist,
            channel,
            country,
            now,
            prior_etag,
            resume_token,
        )
        # Leave room for eight independent 256-byte tokens within the receipt cursor limit.
        state = {"etag": etag} if etag is not None and len(etag) <= 128 else {}
        if next_token is not None:
            state["page_token"] = next_token
            truncated = True
        if state:
            validators[route] = state
        records.update((record.provider_id, record) for record in mapped if record.provider_id)
    filtered = filter_records("youtube_api", cfg, list(records.values()))
    return MediaNativePage(
        tuple(filtered),
        "truncated" if truncated else "returned_snapshot",
        json.dumps({"scope": fp, "etags": validators}, sort_keys=True, separators=(",", ":")),
    )


def _podcast_feed(row: Mapping[str, Any], now: datetime) -> tuple[IngestionRecord, int]:
    """Map feed-level catalog metadata and return its provider-supplied newest-publication epoch."""
    feed_id, title = row.get("id"), _text(row.get("title"))
    if type(feed_id) is not int or not 0 < feed_id < 10**16 or not title:
        raise rest.RestSchemaChanged("podcast_index_feed_invalid")
    epoch = row.get("newestItemPublishTime")
    stamp = epoch if type(epoch) is int and 0 < epoch <= int(now.timestamp()) else 0
    published = datetime.fromtimestamp(stamp, UTC) if stamp else None
    fields: dict[str, object] = {
        "media_type": "podcast",
        "artwork_aspect": "square",
        "release_status": "released",
        "external_ids": build_external_ids(
            {"podcastindex": feed_id, "itunes": row.get("itunesId")}
        ),
    }
    if published:
        fields.update(release_date=published.date().isoformat(), release_year=published.year)
    if language := normalize_language(row.get("language")):
        fields["language"] = language
    if rating := normalize_content_rating(
        bool(row["explicit"]) if row.get("explicit") in (0, 1) else row.get("explicit")
    ):
        fields["content_rating"] = rating
    categories = _object(row.get("categories"))
    if genres := normalize_genres(
        (*categories.keys(), *categories.values()), provider="podcast_index"
    ):
        fields["genres"] = genres
    if artwork := normalize_cover_url(row.get("artwork") or row.get("image")):
        fields["image_url"] = artwork
    if canonical := normalize_cover_url(row.get("link") or row.get("url")):
        fields["canonical_url"] = canonical
    if author := _text(row.get("author")):
        fields["creator"] = author[:240]
    return _record(
        "podcast_index",
        f"podcast_index:{feed_id}",
        title,
        _text(row.get("description")),
        now,
        fields,
        observed_at=published,
    ), stamp


async def collect_podcast_index(
    config: ConnectorConfig,
    api_key: str,
    api_secret: str,
    now: datetime,
    fetch: FetchBytes,
    cursor: str | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> MediaNativePage:
    """Collect one documented Podcast Index query and stage its accepted high-water mark.

    Recent feeds use since/language/category query fields. Search by term owns the optional
    value-block filter. Query and selection filters define continuation state independently of
    source identity, so changing a selection restarts from its configured since value. A capped
    recent page keeps the old watermark to avoid skipping unseen rows.
    """
    params = {"max": str(_PODCAST_LIMIT)}
    cfg = config.model_dump(exclude_none=True)
    validate_podcast_index_options(cfg)
    fp = native_fingerprint("podcast_index", cfg)
    if fp is None:
        raise RuntimeError("podcast_index_fingerprint_unavailable")
    state_since = _state(cursor, fp).get("since")
    since = max(config.podcast_index_since or 0, state_since if type(state_since) is int else 0)
    endpoint = "recent/feeds"
    if config.podcast_index_query:
        endpoint = "search/byterm"
        params["q"] = config.podcast_index_query
        if config.podcast_index_value:
            params["val"] = config.podcast_index_value
    else:
        if since:
            params["since"] = str(since)
        if config.podcast_index_languages:
            params["lang"] = ",".join(config.podcast_index_languages)
        if config.podcast_index_categories:
            params["cat"] = config.podcast_index_categories
        if config.podcast_index_excluded_categories:
            params["notcat"] = config.podcast_index_excluded_categories
    stamp = str(int((clock() if clock else datetime.now(UTC)).timestamp()))
    # Podcast Index mandates SHA-1 over concatenated values, with a three-minute timestamp window.
    digest = sha1(f"{api_key}{api_secret}{stamp}".encode(), usedforsecurity=False).hexdigest()
    response = await fetch(
        f"{_PODCAST_API}/{endpoint}?{urlencode(params)}",
        headers={
            "Accept": "application/json",
            "X-Auth-Key": api_key,
            "X-Auth-Date": stamp,
            "Authorization": digest,
        },
        max_bytes=1024_000,
        allow_redirects=False,
    )
    payload = _decode(response)
    if payload.get("status") not in ("true", True):
        raise rest.RestSchemaChanged("podcast_index_response_invalid")
    rows = _rows(payload, "feeds", _PODCAST_LIMIT)
    mapped = [_podcast_feed(row, now) for row in rows]
    capped = len(rows) == _PODCAST_LIMIT
    if endpoint == "recent/feeds" and not capped:
        # Keep a one-second overlap for tied timestamps. Stable feed IDs and content revisions dedupe.
        since = max(since, *(max(0, timestamp - 1) for _, timestamp in mapped)) if mapped else since
    records = filter_records("podcast_index", cfg, [record for record, _ in mapped])
    return MediaNativePage(
        tuple(records),
        "truncated" if capped else "returned_snapshot",
        json.dumps({"scope": fp, "since": since}, separators=(",", ":")),
    )
