"""Pure scholarly adapter: OpenAlex works search (CC0 data, no key needed for basic use).

No network and no database. The request URL is built here from a validated query only (the owner never
supplies a URL); the mapper turns an already-fetched JSON body into one typed ``IngestionRecord`` per work.

Conventions
- Identity is the OpenAlex work id (an https URL); never the position in the result page.
- ``observed_at`` is the work's ``publication_date`` (UTC midnight); a missing, malformed or future date
  falls back to ``collected_at`` with ``timestamp_basis="collection"``.
- The version covers selected content only; ``cited_by_count`` is volatile and kept out of it.
- The abstract is rebuilt from ``abstract_inverted_index`` under hard word/position bounds.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    content_version,
    decode_json,
)
from modules.connectors.providers.news import _record, https_link, plain_text
from modules.ingestion.schemas import IngestionRecord

OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_MAX_BODY = 2 * 1024 * 1024
MAX_WORKS = 25
MAX_AUTHORS = 10
_MAX_QUERY = 200
_MAX_TITLE = 500
_MAX_ABSTRACT = 3500
_MAX_ABSTRACT_POSITION = 5000  # positions beyond this are ignored: bounds memory for hostile indexes
_MAX_INDEX_WORDS = 5000
_SELECT = "id,doi,title,publication_date,authorships,primary_location,cited_by_count,abstract_inverted_index"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def openalex_url(query: object) -> str:
    """Escaped OpenAlex works-search URL from a validated query; anything else is ``provider_scope_invalid``.

    Control characters are folded to spaces and the query is bounded to 1-200 characters; every parameter
    value is percent-encoded so the query cannot add parameters or change the host/path.
    """
    if not isinstance(query, str):
        raise ProviderPayloadError("provider_scope_invalid")
    cleaned = " ".join(_CONTROL_RE.sub(" ", query).split())
    if not 1 <= len(cleaned) <= _MAX_QUERY:
        raise ProviderPayloadError("provider_scope_invalid")
    params = {"search": cleaned, "sort": "publication_date:desc", "per-page": MAX_WORKS, "select": _SELECT}
    return f"{OPENALEX_URL}?" + urlencode(params, quote_via=quote)


def openalex_request(query: object) -> ProviderRequest:
    """Unconditional JSON GET for the server-built search URL (no key; the optional free key is unused)."""
    return ProviderRequest("GET", openalex_url(query), {"Accept": "application/json"})


def rebuild_abstract(index: object) -> str:
    """Join an inverted index (word -> positions) into plain text; invalid or oversized entries are skipped."""
    if not isinstance(index, dict):
        return ""
    slots: dict[int, str] = {}
    for scanned, (word, positions) in enumerate(index.items()):
        if scanned >= _MAX_INDEX_WORDS:
            break
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions[:_MAX_INDEX_WORDS]:
            if type(pos) is int and 0 <= pos < _MAX_ABSTRACT_POSITION:
                slots.setdefault(pos, word)
    return plain_text(" ".join(slots[p] for p in sorted(slots)), _MAX_ABSTRACT)


def _publication_date(raw: object, collected_at: datetime) -> datetime | None:
    """``YYYY-MM-DD`` -> UTC midnight; malformed, implausibly old or future (>10 min) -> None."""
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        moment = datetime.fromisoformat(raw + "T00:00:00+00:00").astimezone(UTC)
    except ValueError:
        return None
    if moment.year < 1000 or (moment - collected_at).total_seconds() > 600:
        return None
    return moment


def _name(node: object) -> str:
    """Bounded display name of an OpenAlex author/source node, or an empty string."""
    value = node.get("display_name") if isinstance(node, dict) else None
    return plain_text(value, 255) if isinstance(value, str) else ""


def _authors(authorships: object) -> list[str]:
    """First ``MAX_AUTHORS`` non-empty author display names, in provider order."""
    names: list[str] = []
    for item in authorships[: MAX_AUTHORS * 4] if isinstance(authorships, list) else []:
        name = _name(item.get("author")) if isinstance(item, dict) else ""
        if name:
            names.append(name)
        if len(names) == MAX_AUTHORS:
            break
    return names


def map_openalex(body: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Map one works-search page to records; works without a usable id or title are skipped.

    Raises ``ProviderPayloadError`` for an oversized/invalid body, and ``kind="incomplete"`` when no work
    is usable so the last good records stay.
    """
    if len(body) > OPENALEX_MAX_BODY:
        raise ProviderPayloadError("provider_response_too_large")
    payload = decode_json(body)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ProviderPayloadError("openalex_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for work in results[:MAX_WORKS]:
        if not isinstance(work, dict):
            continue
        link = https_link(work.get("id"))
        title = plain_text(work.get("title") if isinstance(work.get("title"), str) else None, _MAX_TITLE)
        if link is None or not title or len(link) > 512:
            continue
        abstract = rebuild_abstract(work.get("abstract_inverted_index"))
        published = _publication_date(work.get("publication_date"), collected_at)
        pub_iso = published.isoformat() if published else None
        doi = https_link(work.get("doi"))
        doi = doi if doi and len(doi) <= 255 else None
        location = work.get("primary_location")
        source = _name(location.get("source")) if isinstance(location, dict) else ""
        cited = work.get("cited_by_count")
        fields: dict[str, Any] = {"title": title, "canonical_url": link}
        optional = {
            "summary": abstract, "published_at": pub_iso, "authors": _authors(work.get("authorships")),
            "doi": doi, "source": source,
            "cited_by_count": cited if type(cited) is int and cited >= 0 else None,
        }
        fields.update({k: v for k, v in optional.items() if v is not None and v != "" and v != []})
        basis: Literal["provider_published", "collection"] = "provider_published" if published else "collection"
        records[link] = _record(
            provider="openalex", identity=link, title=title, summary=abstract, observed_at=published or collected_at,
            collected_at=collected_at, version=content_version("openalex", link, title, abstract, doi, pub_iso),
            basis=basis, modified_at=None, coverage="returned_snapshot", source_fields=fields,
            canonical_url=link, published_at=pub_iso,
        )
    if not records:
        raise ProviderPayloadError("openalex_no_results", "incomplete")
    return list(records.values())
