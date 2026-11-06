"""Story constants, admission fence, markdown/URL normalisation and attribution helpers shared by clustering and reads."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, Scope
from modules.news.models import NewsObservation, NewsStory
from modules.news.schemas import (
    StoryEvidence,
)

ALGORITHM_VERSION = 1


MAX_CANDIDATES = 100


FUZZY_WINDOW = timedelta(hours=72)


FUZZY_THRESHOLD = 0.92


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> AccessFence:
    """Admit story reads (owner or member) before source selection, counts, or enrichment."""
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        owner_only=False,
    )


class LiveStoryRows(list[tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]]):
    """Carry whether the bounded database scan omitted additional observations."""

    def __init__(
        self,
        values: list[tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]],
        truncated: bool,
        incomplete_reasons: set[str] | None = None,
        next_candidate: tuple[datetime, UUID] | None = None,
        candidate_keys: dict[UUID, tuple[datetime, UUID]] | None = None,
        next_evidence: tuple[UUID, UUID, UUID] | None = None,
    ):
        """Retain bounded live groups with scan truncation and omission reasons."""
        super().__init__(values)
        self.truncated = truncated
        self.incomplete_reasons = incomplete_reasons or set()
        self.next_candidate = next_candidate
        self.candidate_keys = candidate_keys or {}
        self.next_evidence = next_evidence


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


_MD_LINE_MARK = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|>[ \t]?)", re.MULTILINE)


_MD_INLINE_MARK = re.compile(r"[*_`]+")


_MD_INLINE_HEADING = re.compile(r"(?:^|\s)#{1,6}\s+")


_RELEASE_PREFIX = re.compile(r"^(?:tag|release|version)\s*:\s*", re.IGNORECASE)


def _plain_excerpt(title: str, text_value: str, limit: int = 1000) -> str:
    """Return bounded plain text for a story excerpt.

    Strips Markdown markers (headings, bullets, emphasis, backticks, links, images), collapses
    whitespace, drops a leading copy of ``title`` (case/whitespace-insensitive, ignoring trailing
    punctuation) and truncates at a word boundary to at most ``limit`` characters. Pure; never raises.
    """
    plain = _MD_IMAGE.sub("", text_value)
    plain = _MD_LINK.sub(lambda m: m.group(1), plain)
    plain = _MD_LINE_MARK.sub("", plain)
    plain = " ".join(_MD_INLINE_HEADING.sub(" ", _MD_INLINE_MARK.sub("", plain)).split())
    # Release feeds prefix the body with "Tag: <title>"; drop the label so the title copy below matches.
    plain = _RELEASE_PREFIX.sub("", plain)
    head = " ".join(title.split()).rstrip(" .:!?-–—…")
    if head and plain.casefold().startswith(head.casefold()):
        plain = plain[len(head) :].lstrip(" .:!?-–—…")
    if len(plain) > limit:
        cut = plain[:limit]
        plain = (cut.rsplit(" ", 1)[0] if " " in cut and plain[limit] != " " else cut).rstrip()
    return plain


def _canonical_url(value: str | None) -> str | None:
    """Normalize only URL host/default port/fragment while preserving query semantics."""
    if not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        host = parsed.hostname.lower()
        port = parsed.port
        netloc = (
            host
            if port is None or (parsed.scheme.lower(), port) in (("http", 80), ("https", 443))
            else f"{host}:{port}"
        )
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def _identity_keys(url: str | None, content_hash: str) -> list[tuple[str, str]]:
    """Return canonical URL first and exact content hash second for deterministic grouping."""
    canonical = _canonical_url(url)
    values = ([("url", canonical)] if canonical else []) + [("hash", content_hash.lower())]
    return [
        (kind, hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()) for kind, value in values
    ]


def _tokens(value: str) -> set[str]:
    """Extract bounded lowercase word tokens for transparent lexical comparisons."""
    return {token for token in re.findall(r"[\w]{2,64}", value.casefold()) if len(token) <= 64}


def _initial_signal_provenance() -> dict[str, object]:
    """Initialize all seven signals as explicitly unevaluated pending live profiles."""
    names = ("topic", "entity", "goal", "project", "recency", "importance", "novelty")
    return {
        "formula_version": 1,
        "weights": {name: 1.0 / 7.0 for name in names},
        "signals": {
            name: {
                "value": 0.0,
                "available": False,
                "method": "not_evaluated_at_ingest",
                "evidence_ids": [],
            }
            for name in names
        },
    }


# --- Member projection: grant-first evidence; no topics, relevance, entities or trends -------------

_LIVE_STATUS = ("ready", "succeeded")


def _translation_revision(story_id: UUID, representative: StoryEvidence) -> str:
    """Hash the story, its representative version and its title/excerpt text (32 hex chars)."""
    text_hash = hashlib.sha256(
        f"{representative.title}\0{representative.excerpt}".encode()
    ).hexdigest()
    raw = f"{story_id}:{representative.document_version_id}:{text_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _attribution(metadata: Any) -> dict[str, str]:
    """Publisher and license text the provider reported, if any; nothing is inferred."""
    if metadata is None:
        return {}
    result: dict[str, str] = {}
    publisher = metadata.source_fields.get("publisher")
    if isinstance(publisher, str) and publisher.strip():
        result["publisher"] = publisher.strip()[:200]
    if metadata.license_label:
        result["license_label"] = metadata.license_label
    return result


def _stored_attribution(observation: NewsObservation) -> dict[str, str | None]:
    """Read attribution captured at observation time (older rows have none)."""
    stored = observation.match_evidence.get("attribution")
    stored = stored if isinstance(stored, dict) else {}
    return {
        key: stored.get(key)
        for key in ("publisher", "license_label")
        if isinstance(stored.get(key), str)
    }
