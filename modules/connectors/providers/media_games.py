"""Bounded Steam and IGDB game collectors using the shared gated HTTP transport."""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from modules.connectors.provider_options import (
    filter_records,
    native_fingerprint,
    validate_igdb_options,
    validate_steam_options,
)
from modules.connectors.providers import rest
from modules.connectors.providers._media_common import (
    MediaRatePacer,
    build_external_ids,
    normalize_content_rating,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_partial_date,
    parse_media_xml,
    xml_child_text,
    xml_local,
)
from modules.connectors.providers.media_anime_manga_sources import _record
from modules.connectors.service.schemas import ConnectorConfig
from modules.ingestion.public import IngestionRecord

FetchBytes = Callable[..., Awaitable[rest.Fetched]]
_STEAM = "https://store.steampowered.com"
_TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"  # noqa: S105  # fixed public endpoint
_IGDB_GAMES = "https://api.igdb.com/v4/games"
_MAX_APPS = 8
_MAX_NEWS = 20
_MAX_IGDB = 50
_STEAM_LANGUAGES = {"english", "vietnamese", "japanese", "schinese", "koreana", "french", "german", "spanish"}
_IGDB_REGIONS = {"europe": 1, "north_america": 2, "australia": 3, "new_zealand": 4, "japan": 5, "china": 6, "asia": 7, "world": 8}
_IGDB_GENRES = {"adventure": 31, "sports": 14, "rpg": 12, "strategy": 15}
_IGDB_THEMES = {"action": 1, "sci_fi": 18, "fantasy": 17, "thriller": 20, "horror": 19, "mystery": 43}
_AGE_RATINGS = {"3": "safe", "everyone": "safe", "everyone 10+": "pg", "7": "pg", "12": "pg_13", "teen": "pg_13", "16": "r", "mature 17+": "r", "18": "mature", "adults only 18+": "explicit"}
_PACER = MediaRatePacer()


@dataclass(frozen=True)
class GamePage:
    """One complete source snapshot plus its query fingerprint and optional OAuth expiry fence."""

    records: tuple[IngestionRecord, ...]
    coverage: Literal["returned_snapshot", "truncated"]
    cursor_after: str
    token_expires_at: datetime | None = None


def _object(value: object) -> dict[str, Any]:
    """Return provider JSON objects without trusting optional nested shapes."""
    return value if isinstance(value, dict) else {}


def _json(response: rest.Fetched, code: str) -> dict[str, Any]:
    """Decode one bounded JSON object and replace decoder details with a stable provider error."""
    try:
        value = json.loads(response.body or b"")
    except (ValueError, UnicodeError):
        raise rest.RestSchemaChanged(code) from None
    if not isinstance(value, dict):
        raise rest.RestSchemaChanged(code)
    return value


def _steam_game(appid: int, details: Mapping[str, Any], config: Mapping[str, Any], now: datetime) -> IngestionRecord:
    """Normalize Steam appdetails while retaining fuzzy dates and the coming-soon null-price contract."""
    title = details.get("name")
    if not isinstance(title, str) or not title.strip():
        raise rest.RestSchemaChanged("steam_app_invalid")
    release = _object(details.get("release_date"))
    raw_date = release.get("date")
    date, year, observed_at = normalize_partial_date(raw_date)
    coming_soon = release.get("coming_soon") is True
    price = _object(details.get("price_overview"))
    final_price = price.get("final")
    fields: dict[str, object] = {
        "media_type": "game",
        "artwork_aspect": "video",
        "canonical_url": f"https://store.steampowered.com/app/{appid}/",
        "external_ids": build_external_ids({"steam": appid}),
        "release_status": "upcoming" if coming_soon or (observed_at is not None and observed_at > now) else "released",
        "final_price": None if coming_soon or type(final_price) is not int or final_price < 0 else final_price,
    }
    if not coming_soon and isinstance(price.get("currency"), str):
        fields["price_currency"] = price["currency"][:3]
    if isinstance(raw_date, str):
        fields["release_date_text"] = raw_date[:64]
    if date and year:
        fields.update(release_date=date, release_year=year)
    genres = details.get("genres")
    if isinstance(genres, list):
        names = [str(_object(item).get("description") or "") for item in genres[:32]]
        normalized = normalize_genres(names, provider="steam_releases")
        if normalized:
            fields["genres"] = normalized
    score = _object(details.get("metacritic")).get("score")
    if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100:
        fields["rating"] = float(score)
    if isinstance(details.get("required_age"), int):
        fields["content_rating"] = "safe" if details["required_age"] == 0 else "pg_13" if details["required_age"] < 17 else "mature"
    country = normalize_country(config.get("steam_country"))
    if country:
        fields["country"] = country
    if image := details.get("header_image"):
        if isinstance(image, str):
            fields["image_url"] = image
    return _record("steam_releases", f"steam:{appid}", title, str(details.get("short_description") or ""), now, fields, observed_at=observed_at)


async def _steam_details(fetch: FetchBytes, appid: int, config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Fetch exactly one appdetails record per app, never using Steam's multi-appids shortcut."""
    params = urlencode({"appids": str(appid), "cc": config.get("steam_country") or "US", "l": config.get("steam_language") or "english"})
    response = await fetch(f"{_STEAM}/api/appdetails?{params}", max_bytes=512_000, allow_redirects=False)
    root = _json(response, "steam_appdetails_invalid")
    entry = _object(root.get(str(appid)))
    data = entry.get("data")
    if entry.get("success") is not True or not isinstance(data, dict):
        raise rest.CollectionIncomplete
    return data


async def _steam_featured_appids(fetch: FetchBytes, config: Mapping[str, Any]) -> list[int]:
    """Return up to eight IDs, rejecting any malformed present featured category before enrichment."""
    params = urlencode({"cc": config.get("steam_country") or "US", "l": config.get("steam_language") or "english"})
    payload = _json(await fetch(f"{_STEAM}/api/featuredcategories?{params}", max_bytes=512_000), "steam_featured_invalid")
    categories = ("new_releases", "coming_soon")
    if not any(category in payload for category in categories):
        raise rest.RestSchemaChanged("steam_featured_invalid")
    ids: list[int] = []
    for category in categories:
        if category not in payload:
            continue
        items = _object(payload[category]).get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise rest.RestSchemaChanged("steam_featured_invalid")
        for item in items:
            appid = _object(item).get("id")
            if type(appid) is int and appid > 0 and appid not in ids:
                ids.append(appid)
    return ids[:_MAX_APPS]


async def _steam_news(fetch: FetchBytes, appid: int, config: Mapping[str, Any]) -> list[ET.Element]:
    """Fetch one bounded RSS news page for one explicitly selected Steam app."""
    params = urlencode({"appids": str(appid), "l": config.get("steam_language") or "english"})
    response = await fetch(f"{_STEAM}/feeds/news.xml?{params}", max_bytes=512_000)
    root = parse_media_xml(response.body)
    items = [node for node in root.iter() if xml_local(node.tag) == "item"]
    if len(items) > _MAX_NEWS:
        raise rest.CollectionIncomplete
    return items


async def collect_steam(config: ConnectorConfig, now: datetime, fetch: FetchBytes) -> GamePage:
    """Collect featured games or bounded per-app news and details, then commit one scope-bound snapshot."""
    cfg = config.model_dump(exclude_none=True)
    validate_steam_options(cfg)
    mode = cfg.get("steam_mode") or "featured"
    appids = list(cfg.get("steam_appids") or ()) if mode == "news" else await _steam_featured_appids(fetch, cfg)
    news_rows = {appid: await _steam_news(fetch, appid, cfg) for appid in appids} if mode == "news" else {}
    if len(appids) > _MAX_APPS:
        raise rest.CollectionIncomplete
    games = {}
    for appid in appids:
        details = await _steam_details(fetch, appid, cfg)
        games[appid] = _steam_game(appid, details, cfg, now)
    records = list(games.values())
    if mode == "news":
        for appid, items in news_rows.items():
            game = games[appid]
            source_fields = _object(_object(game.metadata.get("provider_record")).get("source_fields"))
            for item in items:
                title = xml_child_text(item, "title") or ""
                link = xml_child_text(item, "link") or ""
                guid = xml_child_text(item, "guid") or link
                published = xml_child_text(item, "pubDate")
                date, _, observed = normalize_partial_date(published)
                if not title or not guid or not link.startswith("https://steamcommunity.com/"):
                    raise rest.RestSchemaChanged("steam_news_invalid")
                fields = dict(source_fields)
                fields["canonical_url"] = link
                if date:
                    fields["release_date"] = date
                records.append(_record("steam_releases", f"steam-news:{appid}:{sha256(guid.encode()).hexdigest()[:32]}", title, xml_child_text(item, "description") or "", now, fields, observed_at=observed))
    filtered = filter_records("steam_releases", cfg, records)
    fingerprint = native_fingerprint("steam_releases", cfg)
    if fingerprint is None:
        raise ValueError("steam_fingerprint_missing")
    return GamePage(tuple(filtered), "returned_snapshot", json.dumps({"scope": fingerprint}, separators=(",", ":")))


def _igdb_apicalypse(config: Mapping[str, Any]) -> str:
    """Build a fixed-field Apicalypse query using only validated enum/numeric selectors."""
    clauses = []
    if types := config.get("igdb_game_types"):
        clauses.append(f"game_type = ({','.join(map(str, sorted(map(int, types))))})")
    if region := config.get("igdb_region"):
        clauses.append(f"release_dates.release_region = ({_IGDB_REGIONS[region]})")
    if platforms := config.get("igdb_platforms"):
        clauses.append(f"platforms = ({','.join(map(str, sorted(platforms)))})")
    if genres := config.get("media_genres"):
        genre_ids = sorted({_IGDB_GENRES[value] for value in genres if value in _IGDB_GENRES})
        theme_ids = sorted({_IGDB_THEMES[value] for value in genres if value in _IGDB_THEMES})
        alternatives = [f"genres = ({','.join(map(str, genre_ids))})"] if genre_ids else []
        alternatives.extend(f"themes = ({theme_id})" for theme_id in theme_ids)
        if alternatives:
            clauses.append("(" + " | ".join(alternatives) + ")")
    if year := config.get("release_year"):
        low = int(datetime(year, 1, 1, tzinfo=UTC).timestamp())
        high = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp()) - 1
        clauses.append(f"first_release_date >= {low} & first_release_date <= {high}")
    if rating := config.get("min_rating"):
        clauses.append(f"rating >= {float(rating):g}")
    where = " & ".join(clauses)
    return (
        "fields name,summary,url,cover.url,first_release_date,genres.id,genres.name,themes.id,themes.name,"
        "game_type.id,game_type.name,rating,age_ratings.rating.name,age_ratings.category.name,"
        "external_games.uid,external_games.category,platforms.name; "
        + (f"where {where}; " if where else "")
        + f"sort first_release_date desc; limit {_MAX_IGDB};"
    )


def _igdb_game(row: Mapping[str, Any], config: Mapping[str, Any], now: datetime) -> IngestionRecord:
    """Map IGDB taxonomy, game type, release and maturity data into canonical media metadata."""
    game_id, title = row.get("id"), row.get("name")
    if type(game_id) is not int or game_id < 1 or not isinstance(title, str) or not title.strip():
        raise rest.RestSchemaChanged("igdb_game_invalid")
    timestamp = row.get("first_release_date")
    observed: datetime | None
    if type(timestamp) is int and 0 <= timestamp <= 4_102_444_800:
        observed = datetime.fromtimestamp(timestamp, UTC)
        date, year, _ = normalize_partial_date(observed.date().isoformat())
    else:
        date, year, observed = normalize_partial_date(timestamp)
    raw_genres = row.get("genres")
    raw_themes = row.get("themes")
    genres: list[Any] = raw_genres if isinstance(raw_genres, list) else []
    themes: list[Any] = raw_themes if isinstance(raw_themes, list) else []
    genre_names = [str(_object(item).get("name") or "") for item in genres[:32]]
    theme_names = [str(_object(item).get("name") or "") for item in themes[:32]]
    normalized = normalize_genres((*genre_names, *theme_names))
    game_type = _object(row.get("game_type")).get("name")
    format_map = {"Main Game": "main_game", "DLC": "dlc", "Expansion": "expansion", "Remake": "remake", "Remaster": "remaster"}
    external_games = row.get("external_games")
    external_games = external_games if isinstance(external_games, list) else []
    external_steam = next(
        (
            str(_object(item).get("uid"))
            for item in external_games[:32]
            if _object(item).get("category") == 1 and _object(item).get("uid")
        ),
        None,
    )
    fields: dict[str, object] = {
        "media_type": "game",
        "artwork_aspect": "video",
        "canonical_url": row.get("url") if isinstance(row.get("url"), str) and row["url"].startswith("https://www.igdb.com/") else f"https://www.igdb.com/games/{game_id}",
        "external_ids": build_external_ids({"igdb": game_id, "steam": external_steam}),
    }
    cover = _object(row.get("cover")).get("url")
    if isinstance(cover, str):
        image = normalize_cover_url("https:" + cover if cover.startswith("//") else cover)
        if image:
            fields["image_url"] = image.replace("t_thumb", "t_cover_big")
    if date and year:
        fields.update(release_date=date, release_year=year)
    if normalized:
        fields["genres"] = normalized
    if isinstance(game_type, str) and game_type in format_map:
        fields["format"] = format_map[game_type]
    rating = row.get("rating")
    if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 100:
        fields["rating"] = float(rating)
    age_rows = row.get("age_ratings")
    age_rows = age_rows if isinstance(age_rows, list) else []
    age_names = [str(_object(_object(item).get("rating")).get("name") or "").casefold() for item in age_rows[:16]]
    ratings = [normalize_content_rating(_AGE_RATINGS[name]) for name in age_names if name in _AGE_RATINGS]
    if ratings:
        fields["content_rating"] = max(ratings, key=("safe", "pg", "pg_13", "r", "mature", "explicit").index)
    if summary := row.get("summary"):
        fields["summary"] = str(summary)[:8000]
    return _record("igdb_games", f"igdb:{game_id}", title, str(row.get("summary") or ""), now, fields, observed_at=observed)


async def collect_igdb(
    config: ConnectorConfig,
    client_id: str,
    client_secret: str,
    now: datetime,
    fetch: FetchBytes,
) -> GamePage:
    """Exchange Twitch client credentials and issue one paced, bounded Apicalypse request."""
    cfg = config.model_dump(exclude_none=True)
    validate_igdb_options(cfg)
    fingerprint = native_fingerprint("igdb_games", cfg)
    if fingerprint is None:
        raise ValueError("igdb_fingerprint_missing")
    oauth_body = urlencode({"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}).encode()
    oauth = await fetch(
        _TWITCH_TOKEN,
        request_body=oauth_body,
        request_content_type="application/x-www-form-urlencoded",
        max_bytes=64_000,
        allow_redirects=False,
    )
    token_data = _json(oauth, "igdb_oauth_invalid")
    token = token_data.get("access_token")
    expires_in = token_data.get("expires_in")
    if not isinstance(token, str) or not token or len(token) > 4096 or type(expires_in) is not int or expires_in <= 30:
        raise rest.RestSchemaChanged("igdb_oauth_invalid")
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    await _PACER.pace("api.igdb.com", 0.25)
    response = await fetch(
        _IGDB_GAMES,
        headers={"Accept": "application/json", "Client-ID": client_id, "Authorization": f"Bearer {token}"},
        request_body=_igdb_apicalypse(cfg).encode("ascii"),
        request_content_type="text/plain",
        max_bytes=1024_000,
        allow_redirects=False,
    )
    if datetime.now(UTC) >= expires_at:
        raise rest.CollectionIncomplete
    try:
        rows = json.loads(response.body or b"")
    except (ValueError, UnicodeError):
        raise rest.RestSchemaChanged("igdb_response_invalid") from None
    if not isinstance(rows, list) or len(rows) > _MAX_IGDB or any(not isinstance(row, dict) for row in rows):
        raise rest.CollectionIncomplete if isinstance(rows, list) else rest.RestSchemaChanged("igdb_response_invalid")
    records = filter_records("igdb_games", cfg, [_igdb_game(row, cfg, now) for row in rows])
    cursor = json.dumps({"scope": fingerprint}, separators=(",", ":"))
    return GamePage(tuple(records), "returned_snapshot", cursor, expires_at)
