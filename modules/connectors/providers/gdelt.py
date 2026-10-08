"""Experimental, bounded GDELT DOC 2.0 article-list adapter (pure; no network).

Fixed query, <= 50 records, 8 s request timeout owned by the transport. GDELT lists articles it saw; this is
not complete news coverage. ``seendate`` is when GDELT saw the URL (not publication): it is kept as a source
field and used as a stable ``observed_at`` with ``timestamp_basis="collection"``. On timeout or error the
controller keeps the last good page and schedules the next retry; there is no proxy rotation and no fallback
scraping of other publishers.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    content_version,
)
from modules.connectors.providers.news import _record, https_link, plain_text
from modules.ingestion.schemas import IngestionRecord

EXPERIMENTAL = True
GDELT_TIMEOUT_SECONDS = 8
GDELT_DEFAULT_MAX_RECORDS = 5
GDELT_HARD_MAX_RECORDS = 50
_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
_SEEN_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")


def gdelt_request(max_records: int = GDELT_DEFAULT_MAX_RECORDS) -> ProviderRequest:
    """Fixed economy query; ``max_records`` is clamped by refusal (1..50), never silently raised."""
    if type(max_records) is not int or not 1 <= max_records <= GDELT_HARD_MAX_RECORDS:
        raise ProviderPayloadError("gdelt_max_records_invalid")
    query = urlencode({"query": "economy", "mode": "artlist", "format": "json",
                       "maxrecords": max_records, "timespan": "24h"})
    return ProviderRequest("GET", f"{_BASE}?{query}")


def _seen(raw: object) -> datetime | None:
    match = _SEEN_RE.match(raw) if isinstance(raw, str) else None
    if not match:
        return None
    try:
        y, mo, d, h, mi, sec = (int(g) for g in match.groups())
        return datetime(y, mo, d, h, mi, sec, tzinfo=UTC)
    except ValueError:
        return None


def map_gdelt(payload: object, collected_at: datetime, max_records: int = GDELT_DEFAULT_MAX_RECORDS) -> list[IngestionRecord]:
    """Map an artlist payload; non-object bodies, error text and a missing ``articles`` key are rejected."""
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        raise ProviderPayloadError("gdelt_response_invalid")
    max_records = min(max(max_records, 1), GDELT_HARD_MAX_RECORDS)
    articles = payload["articles"]
    by_id: dict[str, IngestionRecord] = {}
    for art in articles:
        if not isinstance(art, dict):
            continue
        url = https_link(art.get("url"))
        title = plain_text(art.get("title") if isinstance(art.get("title"), str) else None, 500)
        if url is None or not title:
            continue
        identity = "gdelt:" + hashlib.sha256(url.encode()).hexdigest()[:32]
        seen = _seen(art.get("seendate"))
        fields: dict[str, Any] = {"title": title, "url": url}
        for key, limit in (("domain", 255), ("language", 64), ("sourcecountry", 64)):
            val = plain_text(art.get(key) if isinstance(art.get(key), str) else None, limit)
            if val:
                fields[key] = val
        if isinstance(art.get("seendate"), str) and seen:
            fields["seendate"] = art["seendate"]
        version = content_version("gdelt_economy", url, title, fields.get("domain"), fields.get("language"))
        basis: Literal["collection"] = "collection"
        record = _record(
            provider="gdelt_economy", identity=identity, title=title, summary="", observed_at=seen or collected_at,
            collected_at=collected_at, version=version, basis=basis, modified_at=None,
            coverage="returned_snapshot", source_fields=fields, canonical_url=url,
        )
        old = by_id.get(identity)
        if old is None or (record.version or "") < (old.version or ""):
            by_id[identity] = record
    ranked = sorted(by_id.values(), key=lambda r: (r.observed_at, r.provider_id), reverse=True)
    if len(ranked) > max_records:
        ranked = [r.model_copy(update={"metadata": _truncated(r.metadata)}) for r in ranked[:max_records]]
    return ranked


def _truncated(metadata: dict[str, Any]) -> dict[str, Any]:
    out = dict(metadata)
    out["provider_record"] = {**out["provider_record"], "coverage": "truncated"}
    return out


GDELT_ADAPTER = PureAdapter(gdelt_request, map_gdelt)
