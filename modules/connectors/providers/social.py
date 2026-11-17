"""Bounded unauthenticated GitHub public Releases REST snapshots."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from core.heavy_work import to_thread_joined
from modules.connectors.providers.feed_catalog import (
    _plain_text,
    _retry_deadline,
    _selected_hash,
    send_fence_trace,
)
from modules.connectors.public import ConnectorConfig, ProviderCollectionPage, ProviderRateLimited
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata
from modules.sources.public import ConnectorSource

_MAX_PAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 25 * 1024 * 1024
_MAX_PAGES = 5
_NEXT = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?", re.IGNORECASE)
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _api_url(owner: str, repository: str, page: int) -> str:
    """Construct the sole configured public Releases endpoint for one page number."""
    return (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/releases"
        f"?per_page=100&page={page}"
    )


def _validated_next(value: str, owner: str, repository: str) -> tuple[str, int]:
    """Accept only the fixed HTTPS host/repository route and bounded pagination query."""
    target = urljoin("https://api.github.com/", value)
    parsed = urlsplit(target)
    expected_path = f"/repos/{owner}/{repository}/releases"
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    # GitHub's Link header names the repository by numeric id (/repositories/<id>/releases) rather than owner/name.
    # Only the page number is taken from it; the returned URL is rebuilt from the configured owner/repository.
    path_ok = (
        parsed.path.casefold() == expected_path.casefold()
        or re.fullmatch(r"/repositories/\d+/releases", parsed.path) is not None
    )
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.github.com"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not path_ok
        or set(query) != {"per_page", "page"}
        or query["per_page"] != ["100"]
        or len(query["page"]) != 1
        or not query["page"][0].isdigit()
    ):
        raise ValueError("github_pagination_invalid")
    page = int(query["page"][0])
    if not 1 <= page <= _MAX_PAGES:
        raise ValueError("github_pagination_invalid")
    return _api_url(owner, repository, page), page


def _release_canonical(
    value: str | None, owner: str, repository: str, tag_name: str | None
) -> str | None:
    """Keep actual release links only on the configured public github.com repository."""
    if not value:
        return None
    parsed = urlsplit(value)
    prefix = f"/{owner}/{repository}/releases"
    path = parsed.path
    tag_path = (
        path[len(prefix) + len("/tag/") :]
        if path.casefold().startswith(prefix.casefold() + "/tag/")
        else None
    )
    if (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and len(value) <= 2048
        and tag_name is not None
        and tag_path is not None
        and unquote(tag_path) == tag_name
    ):
        return value
    return None


async def _get_page(
    url: str,
    byte_limit: int,
    timeout_seconds: float,
    before_request: Callable[[], Awaitable[None]],
) -> tuple[list[object], str | None, int]:
    """Fetch one public releases page without following redirects or reading unbounded JSON.

    ``before_request`` runs immediately before every page send and aborts it by raising.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=False,
                follow_redirects=False,
                verify=True,
            ) as client:
                await before_request()
                async with client.stream(
                    "GET",
                    url,
                    headers=_API_HEADERS,
                    extensions={"trace": send_fence_trace(before_request)},
                ) as response:
                    now = datetime.now(UTC)
                    if response.status_code == 429 or (
                        response.status_code == 403
                        and (
                            response.headers.get("x-ratelimit-remaining") == "0"
                            or response.headers.get("retry-after")
                        )
                    ):
                        raise ProviderRateLimited(_retry_deadline(response.headers, now))
                    if response.status_code >= 400 or 300 <= response.status_code < 400:
                        raise ValueError("github_provider_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > min(_MAX_PAGE_BYTES, byte_limit):
                            raise ValueError("github_response_too_large")
                    parsed = await to_thread_joined(json.loads, body)
                    next_match = _NEXT.search(response.headers.get("link", ""))
                    next_link = next_match.group(1) if next_match else None
                    return parsed, next_link, len(body)
    except ProviderRateLimited:
        raise
    except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("github_provider_unavailable") from None


def _release_source_fields(
    *,
    draft: bool,
    prerelease: bool,
    node_id: str | None,
    name_value: str | None,
    name: str,
    body_value: str | None,
    body_text: str,
    html_url: str | None,
    tag_value: str | None,
    tag: str,
    author: object,
    created_at: str | None,
    published_at: str | None,
) -> tuple[dict[str, object], bool]:
    """Build the bounded provenance ``source_fields`` of a release and whether any field was truncated.

    Only fields the provider actually supplied are included; text fields are clipped to their caps.
    """
    source_fields: dict[str, object] = {
        "draft": draft,
        "prerelease": prerelease,
    }
    source_fields_truncated = (
        len(name) > 500
        or len(body_text) > 4000
        or len(tag) > 255
        or (node_id is not None and len(node_id) > 256)
    )
    if isinstance(node_id, str):
        source_fields["node_id"] = node_id[:256]
        source_fields_truncated = source_fields_truncated or len(node_id) > 256
    if name_value is not None:
        source_fields["name"] = name[:500]
    if body_value is not None:
        source_fields["body"] = body_text[:4000]
    if isinstance(html_url, str) and html_url.startswith("https://") and len(html_url) <= 2048:
        source_fields["html_url"] = html_url
    if tag_value is not None:
        source_fields["tag_name"] = tag[:255]
    if isinstance(author, str):
        source_fields["author"] = author[:1000]
        source_fields_truncated = source_fields_truncated or len(author) > 1000
    if created_at is not None:
        source_fields["created_at"] = created_at
    if published_at is not None:
        source_fields["published_at"] = published_at
    return source_fields, source_fields_truncated


def _release_metadata(
    provider_record: dict[str, object],
    *,
    node_id: str | None,
    name: str,
    title: str,
    body_text: str,
    html_url: str | None,
    canonical_url: str | None,
    tag: str,
    draft: bool,
    prerelease: bool,
    author: object,
    created_at: str | None,
    published_at: str | None,
) -> dict[str, object]:
    """Build the bounded record metadata for a GitHub release.

    Truncates every provider string to its storage limit and includes ``canonical_url`` and the
    timestamps only when present. Pure; ``html_url`` is exposed only when it canonicalized.
    """
    metadata: dict[str, object] = {
        "provider_record": provider_record,
        "node_id": node_id[:256] if isinstance(node_id, str) else None,
        "name": name[:500],
        "title": title[:500],
        "body": body_text[:4000],
        "html_url": html_url if canonical_url is not None else None,
        "tag_name": tag[:255],
        "draft": draft,
        "prerelease": prerelease,
        "author": author[:1000] if isinstance(author, str) else None,
    }
    if canonical_url is not None:
        metadata["canonical_url"] = canonical_url
    if created_at is not None:
        metadata["created_at"] = created_at
    if published_at is not None:
        metadata["published_at"] = published_at
    return metadata


def _str_field(payload: dict[str, Any], key: str) -> str | None:
    """Return ``payload[key]`` when it is a string, otherwise ``None``."""
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _release_fields(release: dict[str, Any], owner: str, repository: str) -> tuple[Any, ...]:
    """Extract and validate the string fields of a REST release.

    Returns ``(author, name, body, tag_name, node_id, html_url, canonical_url, created_at,
    published_at)``; non-string provider values become ``None``.

    Raises:
        ValueError: ``github_response_invalid`` for an over-64-character timestamp or an
            ``html_url`` that does not canonicalize to the configured repository.
    """
    release_owner = release.get("author")
    author = release_owner.get("login") if isinstance(release_owner, dict) else None
    name_value = _str_field(release, "name")
    body_value = _str_field(release, "body")
    tag_value = _str_field(release, "tag_name")
    node_id = _str_field(release, "node_id")
    html_url = _str_field(release, "html_url")
    canonical_url = _release_canonical(html_url, owner, repository, tag_value)
    created_at = _str_field(release, "created_at")
    published_at = _str_field(release, "published_at")
    if (
        (created_at is not None and len(created_at) > 64)
        or (published_at is not None and len(published_at) > 64)
        or (html_url is not None and canonical_url is None)
    ):
        raise ValueError("github_response_invalid")
    return (
        author,
        name_value,
        body_value,
        tag_value,
        node_id,
        html_url,
        canonical_url,
        created_at,
        published_at,
    )


def _release_flags(release: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(draft, prerelease)``, defaulting absent flags to ``False``.

    Raises:
        ValueError: ``github_response_invalid`` when either flag is present but not a boolean.
    """
    draft = release.get("draft", False)
    prerelease = release.get("prerelease", False)
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise ValueError("github_response_invalid")
    return draft, prerelease


def _release_record(
    release: object,
    owner: str,
    repository: str,
    collected_at: datetime,
) -> IngestionRecord | None:
    """Validate one REST release object and map it to a bounded ingestion record.

    Returns ``None`` for draft releases (never ingested).

    Raises:
        ValueError: ``github_response_invalid`` for a malformed release, ID, timestamp or URL.
    """
    if not isinstance(release, dict):
        raise ValueError("github_response_invalid")
    release_id = release.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise ValueError("github_response_invalid")
    if release.get("draft") is True:
        return None
    (
        author,
        name_value,
        body_value,
        tag_value,
        node_id,
        html_url,
        canonical_url,
        created_at,
        published_at,
    ) = _release_fields(release, owner, repository)
    name, body, tag = name_value or "", body_value or "", tag_value or ""
    body_text = _plain_text(body)
    selected = {
        "id": release_id,
        "node_id": node_id,
        "name": name,
        "body": body,
        "html_url": html_url,
        "tag_name": tag,
        "draft": bool(release.get("draft", False)),
        "prerelease": bool(release.get("prerelease", False)),
        "created_at": created_at,
        "published_at": published_at,
        "author": author,
    }
    version = _selected_hash(selected)
    identity = f"github_releases:{owner.casefold()}/{repository.casefold()}:{release_id}"
    content = "\n".join(part for part in (name, f"Tag: {tag}" if tag else "", body_text) if part)
    draft, prerelease = _release_flags(release)
    title = name or tag or f"Release {release_id}"
    source_fields, source_fields_truncated = _release_source_fields(
        draft=draft,
        prerelease=prerelease,
        node_id=node_id,
        name_value=name_value,
        name=name,
        body_value=body_value,
        body_text=body_text,
        html_url=html_url,
        tag_value=tag_value,
        tag=tag,
        author=author,
        created_at=created_at,
        published_at=published_at,
    )
    content_truncated = len(content) > 4000 or source_fields_truncated
    provider_record = ProviderRecordMetadata(
        provider="github_releases",
        identity=identity,
        provider_version=version,
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=content_truncated,
        source_fields=source_fields,
    )
    metadata = _release_metadata(
        provider_record.model_dump(mode="json", exclude_none=True),
        node_id=node_id,
        name=name,
        title=title,
        body_text=body_text,
        html_url=html_url,
        canonical_url=canonical_url,
        tag=tag,
        draft=draft,
        prerelease=prerelease,
        author=author,
        created_at=created_at,
        published_at=published_at,
    )
    return IngestionRecord(
        provider_id=identity,
        content=content[:4000],
        observed_at=collected_at,
        version=version,
        metadata=metadata,
        collected_at=collected_at,
    )


def _mark_records_truncated(records: list[IngestionRecord]) -> None:
    """Rewrite each record's provider coverage to ``truncated`` in place (the sweep hit a page/record cap)."""
    for index, record in enumerate(records):
        record_metadata = dict(record.metadata)
        provider_metadata = ProviderRecordMetadata.model_validate(
            record_metadata["provider_record"]
        ).model_copy(update={"coverage": "truncated"})
        record_metadata["provider_record"] = provider_metadata.model_dump(
            mode="json", exclude_none=True
        )
        records[index] = record.model_copy(update={"metadata": record_metadata})


def _next_release_page(
    link: str | None,
    page: int,
    record_count: int,
    owner: str,
    repository: str,
    seen_pages: set[int],
) -> tuple[str | None, bool]:
    """Decide the next page URL from the ``Link`` header and whether a cap truncated the sweep.

    Returns ``(next_url, truncated)``; no link ends the sweep cleanly, hitting the page or record
    cap ends it as truncated.

    Raises:
        ValueError: ``github_pagination_invalid`` for a repeated or off-scope next link.
    """
    if link is None:
        return None, False
    if page >= _MAX_PAGES or record_count >= 500:
        return None, True
    next_url, next_page = _validated_next(link, owner, repository)
    if next_page in seen_pages:
        raise ValueError("github_pagination_invalid")
    return next_url, False


async def collect_github_releases(
    source: ConnectorSource,
    *,
    collected_at: datetime,
    before_request: Callable[[], Awaitable[None]],
) -> ProviderCollectionPage:
    """Collect at most five validated REST pages of public releases, preserving only metadata and body text."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    config = ConnectorConfig.model_validate(source.configuration)
    owner, repository = config.github_owner, config.github_repository
    if (
        source.provider != "github_releases"
        or source.type != "api"
        or not owner
        or not repository
        or config.history_mode != "returned_snapshot"
    ):
        raise ValueError("provider_scope_invalid")
    records: list[IngestionRecord] = []
    next_url: str | None = _api_url(owner, repository, 1)
    seen_pages: set[int] = set()
    total_bytes = 0
    truncated = False
    started = time.monotonic()
    while next_url is not None and len(seen_pages) < _MAX_PAGES and len(records) < 500:
        remaining = 60.0 - (time.monotonic() - started)
        if remaining <= 0:
            raise ValueError("github_collection_timeout")
        url, page = _validated_next(next_url, owner, repository)
        if page in seen_pages:
            raise ValueError("github_pagination_invalid")
        seen_pages.add(page)
        try:
            values, link, page_bytes = await _get_page(
                url, _MAX_TOTAL_BYTES - total_bytes, min(30.0, remaining), before_request
            )
        except ProviderRateLimited as exc:
            return ProviderCollectionPage(
                records=(), coverage="returned_snapshot", next_eligible_at=exc.next_eligible_at
            )
        total_bytes += page_bytes
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("github_response_too_large")
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("github_response_invalid")
        for release in values:
            record = _release_record(release, owner, repository, collected_at)
            if record is None:
                continue
            records.append(record)
            if len(records) >= 500:
                break
        next_url, capped = _next_release_page(
            link, page, len(records), owner, repository, seen_pages
        )
        truncated = truncated or capped
    if truncated:
        _mark_records_truncated(records)
    return ProviderCollectionPage(
        records=tuple(records),
        coverage="truncated" if truncated else "returned_snapshot",
        next_eligible_at=None,
    )
