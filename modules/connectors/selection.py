"""Pure incremental selection for built-in news/event feeds.

No I/O. A runner hands in mapped records, the stored ISO cursor and a read-only ``seen`` predicate; this module
decides which records to accept this sync (oldest first, capped) and the cursor to commit with them.
Cap, backlog and gap information stays in run stats and never enters ``IngestionRecord.metadata``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only: a runtime import would close an import cycle between public contracts.
    from modules.ingestion.public import IngestionRecord

DEFAULT_MAX_ITEMS = 100
SKEW = timedelta(minutes=10)  # clock-skew margin below a stored cursor; dedup absorbs the re-read


def fold(text: str) -> tuple[str, ...]:
    """Return lowercase, diacritic-free alphanumeric tokens (NFKD, combining marks dropped, ``đ`` -> ``d``)."""
    lowered = text.lower().replace("đ", "d")
    stripped = "".join(c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c))
    tokens: list[str] = []
    current: list[str] = []
    for char in stripped:
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    """Return whether ``phrase`` occurs as a contiguous token run inside ``tokens``."""
    n = len(phrase)
    return n > 0 and any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def matches_keywords(text: str, include: Sequence[str] | None, exclude: Sequence[str] | None) -> bool:
    """Whole-word/phrase match on folded tokens: any-of ``include`` (none = all pass); any ``exclude`` hit rejects."""
    tokens = fold(text)
    if any(_contains_phrase(tokens, fold(term)) for term in exclude or ()):
        return False
    if not include:
        return True
    return any(_contains_phrase(tokens, fold(term)) for term in include)


@dataclass(frozen=True)
class SelectionFilters:
    """Owner-configured limits for one sync; defaults reproduce the pre-option behaviour (cap 100, no filters)."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_age_days: int | None = None
    max_items: int = DEFAULT_MAX_ITEMS


@dataclass(frozen=True)
class Selection:
    """Result of one selection: records to accept, leftover count, cursor to commit, gap window and dedup count."""

    taken: tuple[IngestionRecord, ...]
    backlog: int
    cursor_after: str | None
    gap: tuple[str, str] | None
    duplicates: int


def record_time(record: IngestionRecord) -> datetime | None:
    """Return the record's provider time (newest of observed and modified), or ``None`` when undated.

    A record is undated when its ``timestamp_basis`` is ``"collection"``. Using the modified time lets an
    edited item (older published, newer ``updated``) pass the cursor floor as a new version.
    """
    envelope = record.metadata.get("provider_record") or {}
    if envelope.get("timestamp_basis") == "collection":
        return None
    moment = record.observed_at
    modified = envelope.get("provider_modified_at")
    if isinstance(modified, str):
        try:
            parsed = datetime.fromisoformat(modified)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and parsed > moment:
            moment = parsed
    return moment


def _parse_cursor(cursor: str | None) -> datetime | None:
    """Parse the stored ISO cursor; an unparseable value (foreign format) means "no cursor" rather than a crash."""
    if not cursor:
        return None
    try:
        parsed = datetime.fromisoformat(cursor)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def selection_filters(configuration: Mapping[str, Any]) -> SelectionFilters:
    """Build ``SelectionFilters`` from a source configuration; missing fields mean today's behaviour."""
    return SelectionFilters(
        include=tuple(configuration.get("include_keywords") or ()),
        exclude=tuple(configuration.get("exclude_keywords") or ()),
        max_age_days=configuration.get("max_age_days"),
        max_items=configuration.get("max_items") or DEFAULT_MAX_ITEMS,
    )


def select_incremental(
    records: Sequence[IngestionRecord], *, cursor: str | None, seen: Callable[[IngestionRecord], bool],
    filters: SelectionFilters, now: datetime, skew: timedelta = SKEW,
) -> Selection:
    """Pick the records to accept this sync.

    Order: cursor floor (cursor - skew), drop seen (before the cap so a run of seen items cannot stall the
    cap), age limit, keyword filter, oldest-first sort (undated last, input order), cap. ``cursor_after`` is
    the newest dated time taken (or, with no backlog, the newest dated record past floor and dedup), never
    older than the stored ``cursor``. ``gap`` is reported from the raw input when
    the oldest dated record is newer than the cursor. Undated records never move the cursor.
    """
    cursor_dt = _parse_cursor(cursor)
    timed = [(r, record_time(r)) for r in records]
    dated_all = [t for _, t in timed if t is not None]
    gap = None
    if cursor_dt is not None and dated_all and min(dated_all) > cursor_dt:
        gap = (cursor_dt.isoformat(), min(dated_all).isoformat())
    floor = cursor_dt - skew if cursor_dt else None
    age_floor = now - timedelta(days=filters.max_age_days) if filters.max_age_days else None
    duplicates = 0
    kept: list[tuple[IngestionRecord, datetime | None]] = []
    newest_passed: datetime | None = None  # newest dated record past floor+dedup, even if a filter drops it
    for rec, when in timed:
        if when is not None and floor is not None and when < floor:
            continue
        if seen(rec):
            duplicates += 1
            continue
        if when is not None and age_floor is not None and when < age_floor:
            continue
        if when is not None and (newest_passed is None or when > newest_passed):
            newest_passed = when
        if (filters.include or filters.exclude) and not matches_keywords(
            f"{rec.metadata.get('title', '')} {rec.content}", filters.include, filters.exclude,
        ):
            continue
        kept.append((rec, when))
    dated = sorted((p for p in kept if p[1] is not None), key=lambda p: (p[1], p[0].provider_id))
    ordered = dated + [p for p in kept if p[1] is None]
    taken = ordered[:filters.max_items]
    backlog = len(ordered) - len(taken)
    # With a backlog the cursor stops at the last taken item so the rest drains next sync; with none, filter-
    # rejected items are not "missed", so the cursor may pass them (clears gap warnings). Never regress.
    candidates = [t for _, t in taken if t is not None]
    if backlog == 0 and newest_passed is not None:
        candidates.append(newest_passed)
    if cursor_dt is not None:
        candidates.append(cursor_dt)
    cursor_after = max(candidates).isoformat() if candidates else cursor
    if cursor_dt is not None and cursor_after == cursor_dt.isoformat():
        cursor_after = cursor  # unchanged: keep the stored string verbatim
    return Selection(
        taken=tuple(r for r, _ in taken), backlog=backlog, cursor_after=cursor_after, gap=gap, duplicates=duplicates,
    )
