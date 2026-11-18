"""Bounded ListenBrainz fresh-release discovery with MusicBrainz and Cover Art Archive enrichment."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from modules.connectors.providers import rest
from modules.connectors.providers._media_common import (
    MediaRatePacer,
    build_external_ids,
    build_media_identity,
    media_user_agent,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
)
from modules.connectors.providers.media_anime_manga_sources import _record
from modules.ingestion.public import IngestionRecord

LISTENBRAINZ_BASE = "https://api.listenbrainz.org/1/explore/fresh-releases/"
LISTENBRAINZ_USER_BASE = "https://api.listenbrainz.org/1/user"
MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org/release-group"
MAX_RELEASES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PACER = MediaRatePacer()
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LANGUAGE_639_3 = {
    "de": "deu",
    "en": "eng",
    "es": "spa",
    "fr": "fre",
    "id": "ind",
    "it": "ita",
    "ja": "jpn",
    "ko": "kor",
    "pt": "por",
    "ru": "rus",
    "th": "tha",
    "vi": "vie",
    "zh": "zho",
    "zh-hk": "zho",
}
_PRIMARY_TYPES = {
    "album": "Album",
    "single": "Single",
    "ep": "EP",
}
_FORMAT_BY_PRIMARY_TYPE = {value.casefold(): key for key, value in _PRIMARY_TYPES.items()}

FetchBytes = Callable[..., Awaitable[rest.Fetched]]


@dataclass(frozen=True)
class MusicReleasePage:
    """One complete bounded release snapshot and its cursor for atomic native receipt acceptance."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str
    coverage: Literal["returned_snapshot", "truncated"]


def _text(value: object, limit: int = 512) -> str:
    """Return bounded provider text without accepting non-text values."""
    return value.strip()[:limit] if isinstance(value, str) else ""


def _object(value: object) -> Mapping[str, Any]:
    """Read a JSON object while treating malformed optional nested values as empty."""
    return value if isinstance(value, dict) else {}


def _json_object(response: rest.Fetched, code: str) -> Mapping[str, Any]:
    """Decode a capped response body as one JSON object and redact parser details from failures."""
    try:
        value = json.loads(response.body or b"")
    except (ValueError, UnicodeError):
        raise rest.RestSchemaChanged(code) from None
    if not isinstance(value, dict):
        raise rest.RestSchemaChanged(code)
    return value


def build_listenbrainz_url(config: Mapping[str, Any]) -> str:
    """Build the fixed-host fresh-release request using only ListenBrainz-supported options.

    Without a username this is the sitewide explore endpoint (``days``/``sort``). With a validated
    ``listenbrainz_username`` it is the per-user ``/1/user/{name}/fresh_releases`` endpoint, which
    returns the same ``payload.releases`` envelope for that user's artists and accepts only
    ``past``/``future`` (so ``days``/``sort`` do not apply). The sitewide endpoint ignores
    ``user_name``, so it must never be sent there. Country, genre, language and format filters belong
    to MusicBrainz enrichment and are never sent to ListenBrainz.

    Raises:
        ValueError: ``listenbrainz_username_invalid`` when the name is not ``[A-Za-z0-9_-]{1,30}``.
    """
    username = config.get("listenbrainz_username")
    if username:
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", username):
            raise ValueError("listenbrainz_username_invalid")
        query = urlencode({"past": "true", "future": "true"})
        return f"{LISTENBRAINZ_USER_BASE}/{username}/fresh_releases?{query}"
    params: dict[str, str | int] = {
        "days": config.get("listenbrainz_days") or 14,
        "sort": config.get("listenbrainz_sort") or "release_date",
    }
    return f"{LISTENBRAINZ_BASE}?{urlencode(params)}"


def normalize_musicbrainz_language(raw: object) -> str | None:
    """Convert MusicBrainz ISO 639-3 language values to the canonical media language code."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if len(value) == 2:
        return normalize_language(value)
    code = {"eng": "en", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de"}.get(
        value, next((two for two, three in _LANGUAGE_639_3.items() if three == value), None)
    )
    return normalize_language(code) if code else None


def _lucene_value(value: str) -> str:
    """Quote one validated Lucene value, escaping syntax that could add another search clause."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _any_clause(field: str, values: tuple[str, ...]) -> str | None:
    """Build one deterministic OR clause for a bounded multi-value MusicBrainz selector."""
    if not values:
        return None
    terms = [f"{field}:{_lucene_value(value)}" for value in sorted(set(values))]
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def build_musicbrainz_search_url(group_id: str, config: Mapping[str, Any]) -> str:
    """Build a MusicBrainz Lucene lookup bound to one release group and its supported selectors."""
    if not _MBID.fullmatch(group_id):
        raise ValueError("musicbrainz_group_id_invalid")
    clauses = [f"rgid:{group_id}"]
    countries = tuple(config.get("media_countries") or ())
    if country := config.get("media_country"):
        countries = (*countries, country)
    if clause := _any_clause("country", countries):
        clauses.append(clause)
    languages = tuple(
        _LANGUAGE_639_3[language]
        for language in config.get("media_languages") or ()
        if language in _LANGUAGE_639_3
    )
    if clause := _any_clause("lang", languages):
        clauses.append(clause)
    formats = tuple(
        _PRIMARY_TYPES[value]
        for value in config.get("media_formats") or ()
        if value in _PRIMARY_TYPES
    )
    if clause := _any_clause("primarytype", formats):
        clauses.append(clause)
    if year := config.get("release_year"):
        clauses.append(f"date:{year}")
    if clause := _any_clause("tag", tuple(config.get("listenbrainz_tags") or ())):
        clauses.append(clause)
    if status := config.get("listenbrainz_status"):
        clauses.append(f"status:{status}")
    query = " AND ".join(clauses)
    params = urlencode({"query": query, "fmt": "json", "limit": 1, "inc": "artist-credits+releases+tags"})
    return f"{MUSICBRAINZ_BASE}/release-group?{params}"


def _cursor_state(cursor: str | None, fingerprint: str) -> dict[str, str]:
    """Load enriched discovery-row digests for this exact scope, or rescan the bounded snapshot."""
    if not cursor or len(cursor) > 4096:
        return {}
    try:
        state = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    # Older cursors lack row digests and cannot distinguish corrected discovery metadata.
    if not isinstance(state, dict) or state.get("v") != 3 or state.get("fp") != fingerprint:
        return {}
    rows = state.get("rows")
    if not isinstance(rows, dict) or len(rows) > MAX_RELEASES:
        return {}
    return {
        group_id: digest
        for group_id, digest in rows.items()
        if _MBID.fullmatch(group_id) and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
    }


def _candidate_rows(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    """Validate the ListenBrainz fresh-release envelope and bound it to ``MAX_RELEASES`` rows.

    The real endpoint returns thousands of rows, so an oversized page is not an error: the newest rows
    (``release_date`` descending, then release-group MBID ascending for a deterministic tie-break) are kept
    and the flag reports that the snapshot was truncated. A malformed envelope raises ``RestSchemaChanged``.

    Returns:
        ``(rows, truncated)``.
    """
    nested = _object(payload.get("payload"))
    rows = nested.get("releases")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise rest.RestSchemaChanged("listenbrainz_response_invalid")
    if len(rows) <= MAX_RELEASES:
        return rows, False
    by_id = sorted(rows, key=lambda row: str(row.get("release_group_mbid") or ""))
    newest = sorted(by_id, key=lambda row: str(row.get("release_date") or ""), reverse=True)
    return newest[:MAX_RELEASES], True


def _release_group(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the first bounded MusicBrainz result, or no match for its validated search query."""
    groups = payload.get("release-groups")
    if not isinstance(groups, list) or len(groups) > 1:
        raise rest.RestSchemaChanged("musicbrainz_response_invalid")
    if not groups:
        return None
    if not isinstance(groups[0], dict):
        raise rest.RestSchemaChanged("musicbrainz_response_invalid")
    return groups[0]


def _artist_name(group: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    """Normalize the MusicBrainz artist-credit shape with the fresh-release name as fallback."""
    credit = group.get("artist-credit")
    names = [
        _text(item.get("name") or _object(item.get("artist")).get("name"), 200)
        for item in credit
        if isinstance(item, dict)
    ] if isinstance(credit, list) else []
    return "; ".join(name for name in names if name)[:240] or _text(
        candidate.get("artist_credit_name"), 240
    )


def _group_release(group: Mapping[str, Any], release_id: str) -> Mapping[str, Any]:
    """Select the ListenBrainz-matched release, otherwise the first complete release summary."""
    releases = group.get("releases")
    if not isinstance(releases, list):
        return {}
    valid = [item for item in releases if isinstance(item, dict)]
    return next((item for item in valid if item.get("id") == release_id), valid[0] if valid else {})


def _tags(group: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize a bounded MusicBrainz tag list while preserving provider tags separately from genres."""
    rows = group.get("tags")
    if not isinstance(rows, list):
        return ()
    ranked = sorted(
        (row for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)),
        key=lambda row: row.get("count", 0) if type(row.get("count")) is int else 0,
        reverse=True,
    )
    values = tuple(
        text
        for row in ranked[:32]
        if (text := " ".join(_text(row["name"], 64).casefold().split()))
    )
    return tuple(dict.fromkeys(values))


def _map_record(
    candidate: Mapping[str, Any],
    group: Mapping[str, Any],
    config: Mapping[str, Any],
    collected_at: datetime,
    cover_url: str | None,
) -> IngestionRecord | None:
    """Map one ListenBrainz item and its verified MusicBrainz group into canonical media provenance."""
    group_id = _text(group.get("id"), 36).lower()
    title = _text(group.get("title") or candidate.get("release_name"), 500)
    if not _MBID.fullmatch(group_id) or not title:
        return None
    release_id = _text(candidate.get("release_mbid"), 36).lower()
    release = _group_release(group, release_id)
    partial_date = group.get("first-release-date") or release.get("date") or candidate.get("release_date")
    date, year, observed_at = normalize_partial_date(partial_date)
    if date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        observed_at = None
    primary_type = _text(group.get("primary-type"), 32)
    media_format = _FORMAT_BY_PRIMARY_TYPE.get(primary_type.casefold())
    language = normalize_musicbrainz_language(
        _object(release.get("text-representation")).get("language")
    )
    country = _text(release.get("country"), 2).upper()
    tags = _tags(group)
    genres = normalize_genres(tags, provider="listenbrainz_releases")
    fields: dict[str, object] = {
        "media_type": "music",
        "artwork_aspect": "square",
        "canonical_url": f"https://musicbrainz.org/release-group/{group_id}",
        "external_ids": build_external_ids({"mbid": group_id}),
        "tags": list(tags),
    }
    if observed_at is not None:
        fields["release_status"] = "upcoming" if observed_at > collected_at else "released"
    if media_format:
        fields["format"] = media_format
    if date and year is not None:
        fields.update(release_date=date, release_year=year)
    if language:
        fields["language"] = language
    if re.fullmatch(r"[A-Z]{2}", country):
        fields["country"] = country
    if genres:
        fields["genres"] = genres
    if creator := _artist_name(group, candidate):
        fields["creator"] = creator
    if cover_url:
        fields["image_url"] = cover_url
    return _record(
        "listenbrainz_releases",
        build_media_identity("listenbrainz_releases", group_id),
        title,
        " · ".join(part for part in (creator, date or (str(year) if year else "")) if part),
        collected_at,
        fields,
        observed_at=observed_at,
    )


async def _cover_url(group_id: str, user_agent: str, fetch_bytes: FetchBytes) -> str | None:
    """Fetch one CAA cover through the gated transport and retain only its fixed public image hosts."""
    try:
        response = await fetch_bytes(
            f"{CAA_BASE}/{group_id}/front-500.jpg",
            headers={"Accept": "image/*", "User-Agent": user_agent},
            max_bytes=MAX_RESPONSE_BYTES,
            allow_redirects=True,
        )
    except rest.ProviderHttpError as exc:
        if exc.status_code == 404:
            return None
        raise
    final_url = response.final_url or f"{CAA_BASE}/{group_id}/front-500.jpg"
    host = (urlsplit(final_url).hostname or "").lower()
    # CAA answers 307 to an Internet Archive storage node (e.g. ia800000.us.archive.org), never to the bare
    # apex, so accept the exact fixed hosts or any ``*.archive.org`` subdomain (dot-anchored, no suffix tricks).
    if host not in {"coverartarchive.org", "archive.org"} and not host.endswith(".archive.org"):
        return None
    return normalize_cover_url(final_url)


async def collect_listenbrainz_releases(
    config: Mapping[str, Any],
    *,
    cursor: str | None,
    collected_at: datetime,
    fetch_bytes: FetchBytes,
) -> MusicReleasePage:
    """Fetch a bounded ListenBrainz snapshot (newest ``MAX_RELEASES`` rows, coverage ``truncated`` beyond) and enrich each item before returning a receiptable page.

    Every MusicBrainz request is spaced by the process-wide one-second pacer and carries a descriptive
    User-Agent. The caller supplies SendGate-backed transport. A failed request aborts the
    whole page, and the candidate cursor remembers only successfully enriched discovery rows in this snapshot.
    A changed row for the same group is eligible again.
    """
    from modules.connectors.provider_options import filter_records, native_fingerprint

    fingerprint = native_fingerprint("listenbrainz_releases", config)
    if fingerprint is None:
        raise ValueError("listenbrainz_fingerprint_missing")
    previously_enriched = _cursor_state(cursor, fingerprint)
    user_agent = media_user_agent("ListenBrainz and MusicBrainz")
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    response = await fetch_bytes(
        build_listenbrainz_url(config), headers=headers, max_bytes=MAX_RESPONSE_BYTES
    )
    candidates, truncated = _candidate_rows(_json_object(response, "listenbrainz_response_invalid"))
    records: list[IngestionRecord] = []
    processed_groups: set[str] = set()
    enriched_in_snapshot: dict[str, str] = {}
    for candidate in candidates:
        group_id = _text(candidate.get("release_group_mbid"), 36).lower()
        if not _MBID.fullmatch(group_id):
            raise rest.RestSchemaChanged("listenbrainz_release_id_invalid")
        if group_id in processed_groups:
            continue
        processed_groups.add(group_id)
        # Both older discoveries and changed rows must remain eligible irrespective of the sort.
        row_digest = sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if previously_enriched.get(group_id) == row_digest:
            enriched_in_snapshot[group_id] = row_digest
            continue
        await PACER.pace("musicbrainz.org", 1.0)
        mb_response = await fetch_bytes(
            build_musicbrainz_search_url(group_id, config),
            headers=headers,
            max_bytes=MAX_RESPONSE_BYTES,
        )
        group = _release_group(_json_object(mb_response, "musicbrainz_response_invalid"))
        if group is not None and _text(group.get("id"), 36).lower() != group_id:
            raise rest.RestSchemaChanged("musicbrainz_release_group_mismatch")
        if group is None:
            # An empty search result is retryable on the next snapshot.
            continue
        cover_url = await _cover_url(group_id, user_agent, fetch_bytes)
        if record := _map_record(candidate, group, config, collected_at, cover_url):
            records.append(record)
            enriched_in_snapshot[group_id] = row_digest
    filtered = filter_records("listenbrainz_releases", config, records)
    cursor_after = json.dumps(
        {"v": 3, "fp": fingerprint, "rows": enriched_in_snapshot},
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(cursor_after) > 4096:
        raise rest.CollectionIncomplete
    return MusicReleasePage(
        tuple(filtered), cursor_after, "truncated" if truncated else "returned_snapshot"
    )
