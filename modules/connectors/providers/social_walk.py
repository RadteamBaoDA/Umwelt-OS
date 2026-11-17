"""Catch-up walk state (cursor v2) shared by the Bluesky, Reddit, Mastodon and X adapters.

A walk pages newest -> older until it reaches ``high_water`` (newest post time fully ingested), then
the next sync starts a fresh walk from the top so posts that arrived during a long backlog walk are
picked up. The cursor is a JSON string committed with the ingestion receipt:
``{"v":2, <scope>, "high_water"?, "page"?, "top"?}`` where ``top`` is the newest post seen when the
current walk started and becomes ``high_water`` once the walk is caught up.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from modules.ingestion.public import IngestionRecord


@dataclass(frozen=True)
class Walk:
    """Decoded walk position; ``page`` set means a backlog walk is still in progress."""

    high_water: datetime | None = None
    page: str | None = None
    top: datetime | None = None


def _when(value: object) -> datetime | None:
    """Parse an aware ISO timestamp from a cursor field; anything else is treated as absent-invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def decode(raw: str | None, scope: dict[str, str], max_bytes: int) -> Walk:
    """Decode a stored cursor; never raises.

    v2 cursors for this exact ``scope`` resume. v1 cursors (page only) and anything malformed,
    oversized or scoped to another provider/actor decode to a fresh walk from the top: restarting is
    safe because the ingestion ``seen_record_keys`` dedup absorbs re-fetched posts.
    """
    if not raw or len(raw.encode("utf-8")) > max_bytes:
        return Walk()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return Walk()
    if (
        not isinstance(value, dict)
        or value.get("v") != 2
        or any(value.get(k) != v for k, v in scope.items())
    ):
        return Walk()
    page, high, top = value.get("page"), _when(value.get("high_water")), _when(value.get("top"))
    if (
        (page is not None and not isinstance(page, str))
        or (value.get("high_water") is not None and high is None)
        or (value.get("top") is not None and top is None)
    ):
        return Walk()
    return Walk(high, page or None, top)


def encode(walk: Walk, scope: dict[str, str], max_bytes: int) -> str:
    """Serialize a walk to the bounded v2 cursor string.

    Raises:
        ValueError: ``social_cursor_invalid`` when the cursor would exceed ``max_bytes``.
    """
    body: dict[str, object] = {"v": 2, **scope}
    if walk.high_water:
        body["high_water"] = walk.high_water.isoformat()
    if walk.page:
        body["page"] = walk.page
    if walk.top:
        body["top"] = walk.top.isoformat()
    encoded = json.dumps(body, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("social_cursor_invalid")
    return encoded


def step(
    walk: Walk,
    records: list[IngestionRecord],
    next_page: str | None,
) -> tuple[list[IngestionRecord], Walk]:
    """Apply one fetched page to the walk.

    Returns the records to ingest (everything at or after ``high_water``; equal times are kept and
    deduplicated downstream) and the next walk state. The walk is caught up when the provider has
    no further page or this page already contains a post older than ``high_water``; then
    ``high_water`` becomes the newest post seen at walk start and ``page`` is cleared.
    """
    high = walk.high_water
    kept = [r for r in records if high is None or r.observed_at >= high]
    reached = high is not None and len(kept) < len(records)
    newest = max((r.observed_at for r in records), default=None)
    top = (
        walk.top or newest
    )  # fixed at walk start so late pages cannot lower the new high-water mark
    if reached or not next_page:
        done = max((t for t in (high, top) if t is not None), default=None)
        return kept, Walk(done)
    return kept, Walk(high, next_page, top)
