"""Media filter validation, JSONB provenance predicates and release-window SQL for news projections.

Split out of ``_news`` (pure SQL assembly plus validation; no I/O). ``_news`` imports these names and
re-exports ``DEFAULT_CALENDAR_WINDOW_DAYS``.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, get_args
from uuid import UUID

from sqlalchemy import DateTime, Numeric, Select, and_, case, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.orm import aliased

from modules.knowledge.documents.models import (
    Document,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.sources.public import SourceReadProjection as Source

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")
_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")


# Default release window (days) for sort=release_date_asc; mirrors the media calendar gadget default.
DEFAULT_CALENDAR_WINDOW_DAYS = 30

# Dashboard projection sort orders. ``published_desc`` orders by the post time (``published_at``,
# falling back to ``observed_at`` then ``created_at``) newest first; it shares the non-release
# filter/window path with ``created_at_desc``.
FeedSort = Literal["created_at_desc", "release_date_asc", "published_desc"]


def _validate_token_tuple(
    values: tuple[str, ...] | None,
    *,
    name: str,
    max_len: int,
    pattern: re.Pattern[str],
    max_item_len: int = 32,
) -> tuple[str, ...] | None:
    """Validate an optional bounded tuple of unique regex-matched filter tokens."""
    if values is None:
        return None
    if not values:
        return None
    if (
        len(values) > max_len
        or len(set(values)) != len(values)
        or any(
            not isinstance(item, str) or len(item) > max_item_len or pattern.fullmatch(item) is None
            for item in values
        )
    ):
        raise ValueError(f"Invalid {name} filter")
    return values


def _validate_media_filter_params(
    *,
    countries: tuple[str, ...] | None,
    genres: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
    media_types: tuple[str, ...] | None,
    formats: tuple[str, ...] | None,
    content_ratings: tuple[str, ...] | None,
    release_statuses: tuple[str, ...] | None,
    release_year: int | None,
    release_year_from: int | None,
    release_year_to: int | None,
    release_season: str | None,
    min_rating: float | None,
    release_window_days: int | None,
    sort: str,
) -> None:
    """Validate multi-dimensional media projection filters and sort bounds before SQL assembly."""
    _validate_token_tuple(
        countries, name="countries", max_len=24, pattern=_COUNTRY_CODE_RE, max_item_len=2
    )
    _validate_token_tuple(genres, name="genres", max_len=16, pattern=_SLUG_RE, max_item_len=48)
    _validate_token_tuple(
        languages, name="languages", max_len=16, pattern=_LANG_TAG_RE, max_item_len=16
    )
    _validate_token_tuple(media_types, name="media_types", max_len=13, pattern=_SLUG_RE)
    _validate_token_tuple(formats, name="formats", max_len=16, pattern=_SLUG_RE)
    _validate_token_tuple(content_ratings, name="content_ratings", max_len=8, pattern=_SLUG_RE)
    _validate_token_tuple(release_statuses, name="release_statuses", max_len=8, pattern=_SLUG_RE)
    for label, yr in (
        ("release_year", release_year),
        ("release_year_from", release_year_from),
        ("release_year_to", release_year_to),
    ):
        if yr is not None and (type(yr) is not int or not 1800 <= yr <= 2100):
            raise ValueError(f"Invalid {label} filter")
    if (
        release_year_from is not None
        and release_year_to is not None
        and release_year_from > release_year_to
    ):
        raise ValueError("release_year_from must not exceed release_year_to")
    # Late import: connectors.public -> provider_specs imports documents.public, which loads this module.
    from modules.connectors.public import AnimeSeason

    if release_season is not None and release_season not in get_args(AnimeSeason):
        raise ValueError("Invalid release_season filter")
    if min_rating is not None and (
        isinstance(min_rating, bool)
        or not isinstance(min_rating, (int, float))
        or not math.isfinite(float(min_rating))
        or not 0.0 <= float(min_rating) <= 100.0
    ):
        raise ValueError("Invalid min_rating filter")
    if release_window_days is not None and (
        type(release_window_days) is not int or not 1 <= release_window_days <= 90
    ):
        raise ValueError("Invalid release_window_days filter")
    if sort not in {"created_at_desc", "release_date_asc", "published_desc"}:
        raise ValueError("Invalid sort order")


def _parsed_release_ts_expr(sf: Any) -> Any:
    """Parse ``source_fields.release_date`` to a timestamptz, or SQL NULL when absent or invalid.

    Reads only provenance columns, so it can be evaluated on the provenance side before any join.
    """
    rd = sf["release_date"].astext
    # Legacy values may match the shape but still contain invalid calendar dates.
    normalized = case(
        (
            rd.op("~")(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"),
            rd,
        ),
        (rd.op("~")(r"^\d{4}-\d{2}-\d{2}$"), rd + "T00:00:00Z"),
        (rd.op("~")(r"^\d{4}-\d{2}$"), rd + "-01T00:00:00Z"),
        (rd.op("~")(r"^\d{4}$"), rd + "-01-01T00:00:00Z"),
        else_=None,
    )
    return case(
        (
            func.pg_input_is_valid(normalized, "timestamp with time zone"),
            sa_cast(normalized, DateTime(timezone=True)),
        ),
        else_=None,
    )


def _coalesced_release_ts(parsed: Any) -> Any:
    """Order timestamp: parsed release date, else the document's publication, else creation time."""
    return func.coalesce(parsed, Document.published_at, Document.created_at)


def _release_date_ts_expr(sf: Any) -> Any:
    """Parse supported release dates safely, falling back to publication or creation time."""
    return _coalesced_release_ts(_parsed_release_ts_expr(sf))


def _release_window_bounds(
    sort: FeedSort, release_window_days: int | None
) -> tuple[datetime, datetime] | None:
    """Return the inclusive ``(lower, upper)`` release window, or ``None`` when unbounded.

    ``release_date_asc`` always has a window (caller's days or ``DEFAULT_CALENDAR_WINDOW_DAYS``) so the
    ascending sort never scans a whole source; the lower edge looks back at most 7 days.
    """
    if sort == "release_date_asc" and release_window_days is None:
        release_window_days = DEFAULT_CALENDAR_WINDOW_DAYS
    if release_window_days is None:
        return None
    now = datetime.now(UTC)
    return (
        now - timedelta(days=min(release_window_days, 7)),
        now + timedelta(days=release_window_days),
    )


def _release_candidates_cte(
    sf: Any,
    *,
    source_ids: tuple[UUID, ...],
    media_preds: list[Any],
    window_bounds: tuple[datetime, datetime] | None,
) -> Any:
    """Materialize provenance rows that can possibly fall in the release window (a safe superset).

    Why a MATERIALIZED CTE: the planner cannot estimate the regex/CASE date expression (it guesses one
    row) and used to drive a nested loop of the whole provenance table per eligible source. Forcing
    this set to be computed once, on the provenance side and bounded to the requested sources'
    documents, lets the outer query probe documents/versions by key for only the in-window rows.

    The window check here is only a prefilter: rows with an unparseable date (NULL) are kept because
    their order timestamp falls back to ``Document`` columns the CTE cannot see; the exact window
    predicate is re-applied on the coalesced timestamp in the outer statement. ``release_ts`` is the
    parsed (non-coalesced) date.
    """
    stmt = select(
        NormalizedVersionProvenance.id.label("provenance_id"),
        NormalizedVersionProvenance.document_version_id.label("document_version_id"),
        NormalizedVersionProvenance.source_generation.label("source_generation"),
        _parsed_release_ts_expr(sf).label("release_ts"),
    ).where(
        NormalizedVersionProvenance.document_id.in_(
            select(Document.id).where(Document.source_id.in_(source_ids))
        ),
        *media_preds,
    )
    if window_bounds is not None:
        ts = _parsed_release_ts_expr(sf)
        stmt = stmt.where(or_(ts.is_(None), and_(ts >= window_bounds[0], ts <= window_bounds[1])))
    return stmt.cte("release_candidates").prefix_with("MATERIALIZED")


def _media_provenance_predicates(
    *,
    countries: tuple[str, ...] | None,
    genres: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
    media_types: tuple[str, ...] | None,
    formats: tuple[str, ...] | None,
    content_ratings: tuple[str, ...] | None,
    release_statuses: tuple[str, ...] | None,
    release_year: int | None,
    release_year_from: int | None,
    release_year_to: int | None,
    release_season: str | None,
    min_rating: float | None,
) -> list[Any]:
    """Assemble JSONB ``source_fields`` predicates on ``NormalizedVersionProvenance``."""
    sf = NormalizedVersionProvenance.provenance_json["provider_record"]["source_fields"]
    preds: list[Any] = []
    for key, values in (
        ("country", countries),
        ("language", languages),
        ("format", formats),
        ("content_rating", content_ratings),
        ("release_status", release_statuses),
    ):
        if values:
            preds.append(sf[key].astext.in_(values))
    if media_types:
        # Older YouTube records have no media_type; match the projection's fallback.
        media_type = case(
            (
                and_(
                    func.jsonb_typeof(sf["media_type"]) == "string", sf["media_type"].astext != ""
                ),
                sf["media_type"].astext,
            ),
            (
                NormalizedVersionProvenance.provenance_json["provider_record"]["provider"].astext
                == "youtube",
                "youtube",
            ),
            else_=None,
        )
        preds.append(media_type.in_(media_types))
    if release_season:
        # Spec: IN over the AnimeSeason enum (no LIKE, so no wildcard injection).
        preds.append(func.lower(sf["release_season"].astext).in_([release_season]))
    if genres:
        # Use @> containment OR instead of ?| to avoid asyncpg bind-placeholder conflicts.
        preds.append(or_(*(sf["genres"].contains([g]) for g in genres)))
    year_col = sf["release_year"]
    # CASE prevents PostgreSQL from casting non-numeric legacy JSON regardless of predicate order.
    year_num = case(
        (func.jsonb_typeof(year_col) == "number", sa_cast(year_col.astext, Numeric)), else_=None
    )
    if release_year is not None:
        preds.append(year_num == release_year)
    if release_year_from is not None:
        preds.append(year_num >= release_year_from)
    if release_year_to is not None:
        preds.append(year_num <= release_year_to)
    if min_rating is not None:
        rating_col = sf["rating"]
        rating_num = case(
            (func.jsonb_typeof(rating_col) == "number", sa_cast(rating_col.astext, Numeric)),
            else_=None,
        )
        preds.append(
            rating_num >= Decimal(str(min_rating))
        )  # str() avoids float->Decimal binary expansion
    return preds


def _apply_media_statement_filters(
    statement: Select[Any],
    order_ts_expr: Any,
    sf: Any,
    *,
    sort: FeedSort,
    release_window_days: int | None,
    media_preds: list[Any],
    candidates: Any = None,
) -> Select[Any]:
    """Attach Provenance JSONB predicates and release-date window bounds to ``statement``.

    ``release_date_asc`` joins the ``candidates`` CTE (see ``_release_candidates_cte``; it already
    carries ``media_preds``) and only admits versions with exactly one provenance row (ambiguous
    versions hydrate to ``None`` in the projection anyway); that keeps one row and one order timestamp
    per document. The guard is a NOT EXISTS probe of ``ix_normalized_version_provenance_version`` for a
    second row, run only on the few window survivors, instead of a per-row ``count(*)``. The exact
    window is applied on ``order_ts_expr`` (coalesced with document dates). The window is always set
    for this sort: ``release_window_days`` or ``DEFAULT_CALENDAR_WINDOW_DAYS``.
    """
    window_bounds = _release_window_bounds(sort, release_window_days)
    if sort == "release_date_asc":
        statement = statement.join(
            candidates,
            and_(
                candidates.c.document_version_id == DocumentVersion.id,
                candidates.c.source_generation == Source.generation,
            ),
        )
        sibling = aliased(NormalizedVersionProvenance)
        statement = statement.where(
            ~select(sibling.id)
            .where(
                sibling.document_version_id == candidates.c.document_version_id,
                sibling.id != candidates.c.provenance_id,
            )
            .exists()
        )
        if window_bounds is not None:
            statement = statement.where(
                order_ts_expr >= window_bounds[0], order_ts_expr <= window_bounds[1]
            )
        return statement
    if not media_preds and window_bounds is None:
        return statement
    sub_preds: list[Any] = list(media_preds)
    if window_bounds is not None:
        sub_ts = _release_date_ts_expr(sf)
        sub_preds.extend((sub_ts >= window_bounds[0], sub_ts <= window_bounds[1]))
    return statement.where(
        select(NormalizedVersionProvenance.id)
        .where(
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
            NormalizedVersionProvenance.source_generation == Source.generation,
            *sub_preds,
        )
        .exists()
    )
