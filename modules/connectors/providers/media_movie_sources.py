"""Keyless movie source mappings for Wikidata, iTunes Search, and Letterboxd RSS."""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from modules.connectors.provider_options import filter_media_records
from modules.connectors.provider_options_media import wikidata_selector_sets
from modules.connectors.providers._media_common import (
    MEDIA_ATTRIBUTIONS,
    media_user_agent,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.feed_catalog import _plain_text
from modules.connectors.providers.news import safe_xml_root
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
ITUNES_ENDPOINT = "https://itunes.apple.com/search"
LETTERBOXD_BASE = "https://letterboxd.com"
LETTERBOXD_MAX_ITEMS = 50
_QID = re.compile(r"^Q[1-9][0-9]{0,11}$")
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")
_ISO2 = re.compile(r"^[A-Z]{2}$")
_LANG2 = re.compile(r"^[a-z]{2}$", re.IGNORECASE)


@dataclass(frozen=True)
class LetterboxdPage:
    """Bounded Letterboxd snapshot and validators for the configured username feed."""

    records: tuple[IngestionRecord, ...]
    not_modified: bool
    etag: str | None
    last_modified: str | None


# Spec 3.1 E: canonical MediaGenre -> Wikidata film-genre QID. Only genres with a QID can be pushed down.
# The spec table's QIDs were corrected to the well-known Wikidata film-genre items (action film Q188473,
# comedy film Q157443, ...); live verification of these values is pending owner approval (no live calls).
# Film genres with no MediaGenre slug (crime Q959790, war Q369747, musical Q842256, western Q172980,
# family Q1361932) stay unmapped, as do non-film slugs, so those selections are only post-filtered.
_GENRE_QIDS = {
    "action": "Q188473",
    "adventure": "Q319221",
    "sci_fi": "Q471839",
    "fantasy": "Q157394",
    "romance": "Q1054574",
    "thriller": "Q2484376",
    "horror": "Q200092",
    "comedy": "Q157443",
    "drama": "Q130232",
    "mystery": "Q1200678",
    "documentary": "Q93204",
    "non_fiction": "Q93204",
    "animation": "Q202866",
}


def _wikidata_pushdown(
    config: Mapping[str, object], genres: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...] | None, tuple[str, ...] | None]:
    """Resolve the genre QIDs, required country codes and required language codes of one query.

    Genres: explicit ``genre_qids`` win; otherwise ``media_genres`` is pushed down only when every selected
    genre has a spec 3.1 E QID (a partial push-down would drop genres the owner also selected; the post-fetch
    filter still enforces the rest). Countries and languages are ``None`` when unrestricted, else the
    intersection of every configured selector (matching the AND semantics of ``filter_media_records``),
    with languages lowercased first. Every value is re-validated, so nothing unvalidated reaches the SPARQL text.

    Raises:
        ValueError: ``wikidata_scope_invalid`` when a selector is not an ISO code / QID, or a selected
        country/language dimension is empty after intersection (never an empty ``VALUES`` clause).
    """
    raw_genres = config.get("media_genres")
    media_genres = (
        (raw_genres,)
        if isinstance(raw_genres, str)
        else tuple(g for g in raw_genres if isinstance(g, str))
        if isinstance(raw_genres, (list, tuple))
        else ()
    )
    if not genres and media_genres and all(g in _GENRE_QIDS for g in media_genres):
        genres = tuple(sorted({_GENRE_QIDS[g] for g in media_genres}))
    countries, languages = wikidata_selector_sets(config)
    if countries is not None and any(not _ISO2.fullmatch(code) for code in countries):
        raise ValueError("wikidata_scope_invalid")
    return genres, countries, languages


def _values_clause(variable: str, values: tuple[str, ...] | None, template: str) -> str | None:
    """Render a required ``VALUES`` binding, or ``None`` when the dimension is unrestricted."""
    if values is None:
        return None
    return f"VALUES ?{variable} {{ " + " ".join(template.format(v=v) for v in values) + " }"


def wikidata_query_url(config: Mapping[str, object], now: datetime) -> str:
    """Build a bounded Wikidata SPARQL query from validated date, genre, country and language scope.

    ``media_genres`` (spec 3.1 E QIDs), ``media_countries`` / ``media_country`` (P495 -> P297) and
    ``media_languages`` / ``language`` (P364 -> P218) are pushed down as required ``VALUES`` joins so the
    100-row cap is spent on matching films; unset dimensions stay ``OPTIONAL`` display fields.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    raw_days = config.get("lookback_days", 365)
    if type(raw_days) is not int or not 1 <= raw_days <= 3650:
        raise ValueError("wikidata_scope_invalid")
    genres = config.get("genre_qids", ())
    if (
        not isinstance(genres, (list, tuple))
        or len(genres) > 12
        or any(not isinstance(qid, str) or not _QID.fullmatch(qid) for qid in genres)
    ):
        raise ValueError("wikidata_scope_invalid")
    language = config.get("language")
    if language is not None and (not isinstance(language, str) or not _LANG2.fullmatch(language)):
        raise ValueError("wikidata_scope_invalid")
    genres, countries, languages = _wikidata_pushdown(config, tuple(genres))

    today = now.astimezone(UTC).date()
    start = (today - timedelta(days=raw_days)).isoformat()
    end = (today + timedelta(days=180)).isoformat()
    genre_clause = (
        "VALUES ?genre { " + " ".join(f"wd:{qid}" for qid in genres) + " }\n"
        "?item wdt:P136 ?genre.\n"
        if genres
        else "OPTIONAL { ?item wdt:P136 ?genre. }\n"
    )
    country_values = _values_clause("countryCode", countries, '"{v}"')
    country_clause = (
        f"{country_values}\n  ?item wdt:P495/wdt:P297 ?countryCode.\n"
        if country_values
        else "OPTIONAL { ?item wdt:P495/wdt:P297 ?countryCode. }\n"
    )
    language_values = _values_clause("languageCode", languages, '"{v}"')
    language_clause = (
        f"{language_values}\n  ?item wdt:P364/wdt:P218 ?languageCode.\n"
        if language_values
        else "OPTIONAL { ?item wdt:P364/wdt:P218 ?languageCode. }\n"
    )
    query = f"""
SELECT ?item ?itemLabel ?description ?date ?genreLabel ?countryCode ?languageCode ?imdb ?tmdb ?image WHERE {{
  ?item wdt:P31 wd:Q11424; wdt:P577 ?date.
  FILTER(?date >= "{start}T00:00:00Z"^^xsd:dateTime && ?date <= "{end}T23:59:59Z"^^xsd:dateTime)
  {genre_clause}
  OPTIONAL {{ ?genre rdfs:label ?genreLabel. FILTER(LANG(?genreLabel) = "en") }}
  {country_clause}
  {language_clause}
  OPTIONAL {{ ?item wdt:P345 ?imdb. }}
  OPTIONAL {{ ?item wdt:P4947 ?tmdb. }}
  OPTIONAL {{ ?item wdt:P18 ?image. }}
  OPTIONAL {{ ?item schema:description ?description. FILTER(LANG(?description) = "en") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} ORDER BY DESC(?date) LIMIT 100
""".strip()
    return WIKIDATA_ENDPOINT + "?" + urlencode({"query": query, "format": "json"})


def _movie_record(
    *,
    provider: str,
    identity: str,
    title: str,
    collected_at: datetime,
    fields: dict[str, object],
    summary: str = "",
    canonical_url: str | None = None,
    observed_at: datetime | None = None,
    coverage: Literal["returned_snapshot", "truncated"] = "returned_snapshot",
) -> IngestionRecord:
    """Create bounded media provenance, carrying incomplete-feed coverage when a source cap is reached."""
    fields.setdefault("title", title[:500])
    fields.setdefault("media_type", "movie")
    fields.setdefault("artwork_aspect", "poster")

    fields.setdefault("attribution", MEDIA_ATTRIBUTIONS[provider])
    if canonical_url:
        fields["canonical_url"] = canonical_url
    envelope = ProviderRecordMetadata(
        provider=provider,
        identity=identity,
        timestamp_basis="provider_published" if observed_at else "collection",
        coverage=coverage,
        content_truncated=len(summary) > 20000,
        source_fields=fields,
    )
    metadata: dict[str, object] = {
        "provider": provider,
        "title": title[:500],
        "provider_record": envelope.model_dump(mode="json", exclude_none=True),
    }
    if canonical_url:
        metadata["canonical_url"] = canonical_url
    return IngestionRecord(
        provider_id=identity,
        content=(title + (f"\n\n{summary}" if summary else ""))[:20500],
        observed_at=observed_at or collected_at,
        collected_at=collected_at,
        metadata=metadata,
    )


def _sparql_value(row: Mapping[str, object], name: str) -> str | None:
    """Read a SPARQL JSON binding's scalar value without trusting provider response shapes."""
    value = row.get(name)
    raw = value.get("value") if isinstance(value, Mapping) else None
    return raw if isinstance(raw, str) else None


def _map_wikidata_row(
    row: object, collected_at: datetime
) -> tuple[IngestionRecord, str | None] | None:
    """Map one SPARQL row, returning its normalized genre separately for duplicate folding."""
    if not isinstance(row, Mapping):
        return None
    item_url, title, date_value = (_sparql_value(row, key) for key in ("item", "itemLabel", "date"))
    match = re.fullmatch(r"https://www\.wikidata\.org/entity/(Q[1-9][0-9]{0,11})", item_url or "")
    if not match or not title or not title.strip():
        return None
    date_text, year, release_dt = normalize_partial_date(date_value)
    if year is None:
        return None
    qid = match.group(1)
    external_ids = {"wikidata": qid}
    for key in ("imdb", "tmdb"):
        value = _sparql_value(row, key)
        valid_id = (
            value.startswith("tt") and value[2:].isdigit() and len(value) <= 20
            if key == "imdb" and value
            else bool(value and value.isdigit() and len(value) <= 20)
        )
        if valid_id:
            external_ids[key] = value
    fields: dict[str, object] = {
        "release_year": year,
        "release_status": "upcoming" if release_dt and release_dt > collected_at else "released",
        "external_ids": external_ids,
    }
    if date_text:
        fields["release_date"] = date_text
    country = normalize_country(_sparql_value(row, "countryCode"))
    language = _sparql_value(row, "languageCode")
    if country:
        fields["country"] = country
    if language and _ISO2.fullmatch(language.upper()):
        fields["language"] = language.lower()
    genre = _sparql_value(row, "genreLabel")
    genres = normalize_genres([genre] if genre else [])
    image = _sparql_value(row, "image")
    cover = normalize_cover_url(
        f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(image, safe='')}"
        if image and len(image) <= 240
        else None
    )
    if cover:
        fields["image_url"] = cover
    summary = _sparql_value(row, "description") or ""
    record = _movie_record(
        provider="wikidata_movies",
        identity=f"wikidata:{qid}",
        title=title.strip(),
        collected_at=collected_at,
        fields=fields,
        summary=summary,
        canonical_url=f"https://www.wikidata.org/wiki/{qid}",
    )
    return record, genres[0] if genres else None


def map_wikidata_movies(
    payload: object, *, collected_at: datetime, config: Mapping[str, object]
) -> list[IngestionRecord]:
    """Map at most 100 SPARQL results, merge genre duplicates and apply shared media filters."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    results = payload.get("results") if isinstance(payload, Mapping) else None
    bindings = results.get("bindings") if isinstance(results, Mapping) else None
    if not isinstance(bindings, list) or len(bindings) > 100:
        raise ValueError("wikidata_response_invalid")
    records: dict[str, IngestionRecord] = {}
    genres_by_identity: dict[str, set[str]] = {}
    for row in bindings:
        mapped = _map_wikidata_row(row, collected_at)
        if mapped is None:
            continue
        record, genre = mapped
        records.setdefault(record.provider_id, record)
        if genre:
            genres_by_identity.setdefault(record.provider_id, set()).add(genre)
    for identity, genres in genres_by_identity.items():
        record = records[identity]
        metadata = dict(record.metadata)
        provider_record = dict(metadata["provider_record"])
        source_fields = dict(provider_record["source_fields"])
        source_fields["genres"] = sorted(genres)
        provider_record["source_fields"] = source_fields
        metadata["provider_record"] = provider_record
        records[identity] = record.model_copy(update={"metadata": metadata})
    return filter_media_records(config, list(records.values()), keep_missing=True)


def itunes_movies_url(config: Mapping[str, object]) -> str:
    """Build a fixed iTunes movie search URL from bounded term and storefront scope."""
    term = config.get("search_terms")
    country = config.get("country", "US")
    if not isinstance(term, str) or not 1 <= len(term.strip()) <= 100:
        raise ValueError("itunes_scope_invalid")
    if not isinstance(country, str) or not _ISO2.fullmatch(country):
        raise ValueError("itunes_scope_invalid")
    return (
        ITUNES_ENDPOINT
        + "?"
        + urlencode(
            {
                "term": term.strip(),
                "country": country.lower(),
                "media": "movie",
                "entity": "movie",
                "lang": "en_us",
                "limit": 200,
            }
        )
    )


def map_itunes_movies(
    payload: object, *, collected_at: datetime, config: Mapping[str, object]
) -> list[IngestionRecord]:
    """Map bounded iTunes movie search rows, preserving stable track IDs and normalized metadata."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    if not isinstance(payload, Mapping):
        raise ValueError("itunes_response_invalid")
    rows = payload.get("results")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("itunes_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or type(row.get("trackId")) is not int
            or row["trackId"] <= 0
        ):
            continue
        title, track_url = row.get("trackName"), row.get("trackViewUrl")
        if not isinstance(title, str) or not title.strip():
            continue
        date_text, year, release_dt = normalize_partial_date(row.get("releaseDate"))
        fields: dict[str, object] = {
            "external_ids": {"itunes": str(row["trackId"])},
        }
        if date_text and year:
            fields.update(release_date=date_text, release_year=year)
            fields["release_status"] = (
                "upcoming" if release_dt and release_dt > collected_at else "released"
            )
        country = normalize_country(config.get("country"))
        if country:
            fields["country"] = country
        if isinstance(row.get("artistName"), str):
            fields["creator"] = row["artistName"][:240]
        genre = row.get("primaryGenreName")
        genres = normalize_genres([genre] if isinstance(genre, str) else [])
        if genres:
            fields["genres"] = genres
        rating = normalize_rating(row.get("averageUserRating"), scale=5)
        if rating is not None:
            fields["rating"] = rating
        cover = normalize_cover_url(row.get("artworkUrl100"))
        if cover:
            fields["image_url"] = cover
        summary = row.get("longDescription") or row.get("shortDescription") or ""
        safe_url = (
            track_url
            if isinstance(track_url, str) and track_url.startswith("https://itunes.apple.com/")
            else None
        )
        identity = f"itunes:{row['trackId']}"
        records[identity] = _movie_record(
            provider="itunes_movies",
            identity=identity,
            title=title.strip(),
            collected_at=collected_at,
            fields=fields,
            summary=summary if isinstance(summary, str) else "",
            canonical_url=safe_url,
        )
    return filter_media_records(config, list(records.values()), keep_missing=True)


def _xml_text(node: ElementTree.Element, local_name: str) -> str | None:
    """Return a trimmed first child value by local XML name, independent of its namespace prefix."""
    for child in node:
        if child.tag.rsplit("}", 1)[-1].lower() == local_name.lower() and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def letterboxd_rss_url(username: object) -> str:
    """Build only the canonical Letterboxd RSS URL for a validated account name."""
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ValueError("letterboxd_username_invalid")
    return f"{LETTERBOXD_BASE}/{quote(username, safe='')}/rss/"


def map_letterboxd_rss(
    body: object, *, collected_at: datetime, config: Mapping[str, object]
) -> list[IngestionRecord]:
    """Map the first 50 Letterboxd entries and mark every record truncated if older entries remain."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    root = safe_xml_root(body, max_bytes=1024 * 1024)
    items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "item"]
    coverage: Literal["returned_snapshot", "truncated"] = (
        "truncated" if len(items) > LETTERBOXD_MAX_ITEMS else "returned_snapshot"
    )
    records: dict[str, IngestionRecord] = {}
    for item in items[:LETTERBOXD_MAX_ITEMS]:
        title = _xml_text(item, "title")
        guid = _xml_text(item, "guid") or _xml_text(item, "link")
        link = _xml_text(item, "link")
        if not title or not guid or not link or not link.startswith("https://letterboxd.com/film/"):
            continue
        identity_token = guid.rstrip("/").rsplit("/", 1)[-1]
        if not 1 <= len(identity_token) <= 200 or any(ord(c) < 33 for c in identity_token):
            continue
        identity = f"letterboxd:{identity_token}"
        year_text = _xml_text(item, "filmYear")
        fields: dict[str, object] = {"release_status": "released"}
        if year_text and year_text.isdigit() and 1800 <= int(year_text) <= 2100:
            fields["release_year"] = int(year_text)
        raw_rating = _xml_text(item, "memberRating")
        rating = normalize_rating(raw_rating, scale=5)
        if rating is not None:
            fields["rating"] = rating
        pubdate = _xml_text(item, "pubDate")
        date_text, _, observed_at = normalize_partial_date(pubdate)
        desc = _xml_text(item, "description") or ""
        records[identity] = _movie_record(
            provider="letterboxd_rss",
            identity=identity,
            title=title[:500],
            collected_at=collected_at,
            fields=fields,
            summary=_plain_text(desc),
            canonical_url=link,
            observed_at=observed_at if date_text else None,
            coverage=coverage,
        )
    return filter_media_records(config, list(records.values()), keep_missing=True)


async def collect_wikidata_movies(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    config: Mapping[str, object],
    collected_at: datetime,
) -> list[IngestionRecord]:
    """Fetch Wikidata through the injected gated GET transport and map its bounded SPARQL JSON body."""
    url = wikidata_query_url(config, collected_at)
    fetched = await fetch_bytes(
        url,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": media_user_agent("Wikidata"),
        },
        max_bytes=2 * 1024 * 1024,
    )
    if fetched.body is None:
        raise ProviderHttpError(304)
    try:
        payload = json.loads(fetched.body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("wikidata_response_invalid") from exc
    return map_wikidata_movies(payload, collected_at=collected_at, config=config)


async def collect_itunes_movies(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    config: Mapping[str, object],
    collected_at: datetime,
) -> list[IngestionRecord]:
    """Fetch one bounded iTunes search result page and map it without provider-controlled URLs."""
    fetched = await fetch_bytes(
        itunes_movies_url(config), headers={"Accept": "application/json"}, max_bytes=2 * 1024 * 1024
    )
    if fetched.body is None:
        raise ProviderHttpError(304)
    try:
        payload = json.loads(fetched.body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("itunes_response_invalid") from exc
    return map_itunes_movies(payload, collected_at=collected_at, config=config)


async def collect_letterboxd_rss(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    username: str,
    config: Mapping[str, object],
    collected_at: datetime,
    etag: str | None = None,
    last_modified: str | None = None,
) -> LetterboxdPage:
    """Fetch one username's RSS with conditional validators and retain prior records on a valid 304."""
    url = letterboxd_rss_url(username)
    headers = {"Accept": "application/rss+xml, application/xml;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    fetched = await fetch_bytes(url, headers=headers, max_bytes=1024 * 1024)
    if fetched.body is None:
        if not etag and not last_modified:
            raise ProviderHttpError(304)
        return LetterboxdPage((), True, etag, last_modified)
    records = map_letterboxd_rss(fetched.body, collected_at=collected_at, config=config)
    return LetterboxdPage(
        records=tuple(records),
        not_modified=False,
        etag=fetched.etag,
        last_modified=fetched.last_modified,
    )
