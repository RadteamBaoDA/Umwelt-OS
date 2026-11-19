"""Client-side record filters split out of ``provider_options`` (pure, I/O-free).

Region, severity-band and media-metadata filters applied to mapped ``IngestionRecord`` items after a fetch.
``provider_options`` re-exports ``in_region``, ``filter_media_records`` and ``filter_records``. Shared
tables (``REGION_BOXES``, ``OPTION_FIELDS``, ``MEDIA_OPTION_NAMES``, ``_COORDS``, ``_LEVEL``) are imported
lazily inside the functions that need them, because ``provider_options`` imports this module at load time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only: a runtime import would close an import cycle between public contracts.
    from modules.ingestion.public import IngestionRecord


def _coordinates(record: IngestionRecord) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` from the typed measurement, else from the mapped summary text, else ``None``."""
    world = (record.metadata.get("provider_record") or {}).get("world_data") or {}
    if isinstance(world.get("latitude"), (int, float)) and isinstance(
        world.get("longitude"), (int, float)
    ):
        return float(world["latitude"]), float(world["longitude"])
    from modules.connectors.provider_options import _COORDS

    for pattern in _COORDS:
        if match := pattern.search(record.content):
            return float(match.group(1)), float(match.group(2))
    return None


def in_region(record: IngestionRecord, region: str) -> bool:
    """Whether the record lies inside ``region``'s box. Records without coordinates are kept (ruling).

    Dropping them would hide events whose location the provider omitted; the owner narrowed by area, not by
    "must have a location", so an unlocated record is shown rather than silently lost.
    """
    from modules.connectors.provider_options import REGION_BOXES

    box = REGION_BOXES[region]
    where = _coordinates(record)
    if box is None or where is None:
        return True
    lat, lon = where
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def _severity_band(score: float | None) -> str | None:
    """CVSS v3 qualitative band for a base score (``None`` when unscored)."""
    if score is None or score <= 0:
        return None
    return "LOW" if score < 4 else "MEDIUM" if score < 7 else "HIGH" if score < 9 else "CRITICAL"


def _match_media_dimension(
    value: object, expected: object, *, keep_missing: bool, multi: bool = False
) -> bool:
    """Match one scalar or set-membership media metadata dimension against ``expected``."""
    if expected is None:
        return True
    if value is None:
        return keep_missing
    if multi:
        return isinstance(expected, (list, tuple, set, frozenset)) and value in set(expected)
    return value == expected


def _match_media_genres(
    raw_genres: object, wanted: Sequence[str] | None, *, keep_missing: bool
) -> bool:
    """Return True when ``raw_genres`` intersects ``wanted`` (or when missing if ``keep_missing``)."""
    if not wanted:
        return True
    if not isinstance(raw_genres, (list, tuple, set)) or not raw_genres:
        return keep_missing
    return bool(set(raw_genres) & set(wanted))


def _match_media_numeric_and_season(
    sf: Mapping[str, Any], cfg: Mapping[str, Any], *, keep_missing: bool
) -> bool:
    """Check ``release_year``, ``anime_season``, and ``min_rating`` on one ``source_fields`` mapping."""
    release_year = cfg.get("release_year")
    if release_year is not None:
        actual_year = sf.get("release_year")
        if actual_year is None:
            if not keep_missing:
                return False
        elif type(actual_year) is not int or actual_year != release_year:
            return False
    anime_season = cfg.get("anime_season")
    if anime_season is not None:
        raw_season = sf.get("release_season")
        if not isinstance(raw_season, str) or not raw_season.strip():
            if not keep_missing:
                return False
        elif raw_season.strip().lower().split()[0] != str(anime_season).lower():
            return False
    min_rating = cfg.get("min_rating")
    if min_rating is not None:
        rating = sf.get("rating")
        if rating is None:
            return keep_missing
        if (
            isinstance(rating, bool)
            or not isinstance(rating, (int, float))
            or not math.isfinite(float(rating))
            or float(rating) < float(min_rating)
        ):
            return False
    return True


def _record_matches_media_filters(
    record: IngestionRecord, cfg: Mapping[str, Any], *, keep_missing: bool
) -> bool:
    """Evaluate all configured media filter options against ``record``'s ``source_fields``."""
    provider_record = record.metadata.get("provider_record")
    raw_sf = provider_record.get("source_fields") if isinstance(provider_record, Mapping) else None
    sf: Mapping[str, Any] = raw_sf if isinstance(raw_sf, Mapping) else {}
    if not _match_media_dimension(
        sf.get("country"), cfg.get("media_country"), keep_missing=keep_missing
    ):
        return False
    if not _match_media_dimension(
        sf.get("country"), cfg.get("media_countries"), keep_missing=keep_missing, multi=True
    ):
        return False
    if not _match_media_genres(
        sf.get("genres"), cfg.get("media_genres"), keep_missing=keep_missing
    ):
        return False
    if not _match_media_dimension(
        sf.get("language"), cfg.get("media_languages"), keep_missing=keep_missing, multi=True
    ):
        return False
    if not _match_media_dimension(
        sf.get("format"), cfg.get("media_formats"), keep_missing=keep_missing, multi=True
    ):
        return False
    if not _match_media_dimension(
        sf.get("content_rating"), cfg.get("content_ratings"), keep_missing=keep_missing, multi=True
    ):
        return False
    if cfg.get("exclude_adult") is True and sf.get("content_rating") in {"mature", "explicit"}:
        return False
    return _match_media_numeric_and_season(sf, cfg, keep_missing=keep_missing)


def filter_media_records(
    cfg: Mapping[str, Any],
    records: Sequence[IngestionRecord],
    *,
    keep_missing: bool = False,
) -> list[IngestionRecord]:
    """Filter media ``IngestionRecord`` items post-fetch using configured ``MEDIA_OPTION_NAMES``.

    Reads ``record.metadata["provider_record"]["source_fields"]`` without mutating records.
    When ``keep_missing`` is False (default), records missing a filtered dimension are excluded;
    sparse RSS adapters may opt into ``keep_missing=True`` so unannotated items are preserved.
    """
    from modules.connectors.provider_options import MEDIA_OPTION_NAMES

    if not any(cfg.get(k) is not None for k in MEDIA_OPTION_NAMES):
        return list(records)
    return [
        record
        for record in records
        if _record_matches_media_filters(record, cfg, keep_missing=keep_missing)
    ]


def filter_records(
    provider: str, cfg: Mapping[str, Any], records: Sequence[IngestionRecord]
) -> list[IngestionRecord]:
    """Apply the client-side per-type filters (region, KEV flag, NVD bands, FX quote subset, Kp, advisory, and media options).

    Runs on mapped records before incremental selection. With no relevant option the input is returned as is.
    """
    from modules.connectors.provider_options import _LEVEL, MEDIA_OPTION_NAMES, OPTION_FIELDS

    out = list(records)
    region = cfg.get("region")
    if region and provider in OPTION_FIELDS and "region" in OPTION_FIELDS[provider]:
        out = [r for r in out if in_region(r, region)]
    if provider == "cisa_kev" and cfg.get("kev_ransomware_only"):
        out = [
            r
            for r in out
            if (
                ((r.metadata.get("provider_record") or {}).get("world_data") or {}).get(
                    "provider_fields"
                )
                or {}
            ).get("knownRansomwareCampaignUse")
            == "Known"
        ]
    if provider in ("ecb", "open_er_api") and cfg.get("fx_quotes"):
        wanted_quotes = set(cfg["fx_quotes"])
        out = [
            r
            for r in out
            if (
                ((r.metadata.get("provider_record") or {}).get("world_data") or {}).get(
                    "provider_fields"
                )
                or {}
            ).get("quote")
            in wanted_quotes
        ]
    if provider == "noaa_kp" and cfg.get("kp_min"):
        out = [
            r
            for r in out
            if (
                ((r.metadata.get("provider_record") or {}).get("world_data") or {}).get("value")
                or 0
            )
            >= cfg["kp_min"]
        ]
    if provider == "us_travel_advisories" and cfg.get("advisory_min_level"):
        # A title without "Level N" (e.g. "Worldwide Caution") cannot be ranked, so it is kept rather than hidden.
        out = [
            r
            for r in out
            if (m := _LEVEL.search(str(r.metadata.get("title", "")))) is None
            or int(m.group(1)) >= cfg["advisory_min_level"]
        ]
    if provider == "nvd" and cfg.get("nvd_severity"):
        wanted = set(cfg["nvd_severity"])
        out = [
            r
            for r in out
            if _severity_band(
                ((r.metadata.get("provider_record") or {}).get("world_data") or {}).get("value")
            )
            in wanted
        ]
    if any(cfg.get(k) is not None for k in MEDIA_OPTION_NAMES):
        out = filter_media_records(cfg, out)
    return out
