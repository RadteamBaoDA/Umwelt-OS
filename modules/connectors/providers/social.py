"""Bounded unauthenticated GitHub public Releases REST snapshots."""

import asyncio
from datetime import UTC, datetime
import json
import re
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from modules.connectors.public import ConnectorConfig, ProviderCollectionPage, ProviderRateLimited
from modules.connectors.providers.feed_catalog import _plain_text, _retry_deadline, _selected_hash
from modules.knowledge.documents.public import ProviderRecordMetadata
from modules.ingestion.schemas import IngestionRecord
from modules.sources.schemas import ConnectorSource

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
    if (
        parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.username
        or parsed.password or parsed.fragment or parsed.path.casefold() != expected_path.casefold()
        or set(query) != {"per_page", "page"} or query["per_page"] != ["100"]
        or len(query["page"]) != 1 or not query["page"][0].isdigit()
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
    tag_path = path[len(prefix) + len("/tag/"):] if path.casefold().startswith(prefix.casefold() + "/tag/") else None
    if (
        parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.username
        and not parsed.password and not parsed.query and not parsed.fragment and len(value) <= 2048
        and tag_name is not None and tag_path is not None and unquote(tag_path) == tag_name
    ):
        return value
    return None


async def _get_page(
    url: str, byte_limit: int, timeout_seconds: float
) -> tuple[list[object], str | None, int]:
    """Fetch one public releases page without following redirects or reading unbounded JSON."""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds), trust_env=False, follow_redirects=False, verify=True
            ) as client:
                async with client.stream("GET", url, headers=_API_HEADERS) as response:
                    now = datetime.now(UTC)
                    if response.status_code == 429 or (
                        response.status_code == 403
                        and (response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after"))
                    ):
                        raise ProviderRateLimited(_retry_deadline(response.headers, now))
                    if response.status_code >= 400 or 300 <= response.status_code < 400:
                        raise ValueError("github_provider_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > min(_MAX_PAGE_BYTES, byte_limit):
                            raise ValueError("github_response_too_large")
                    parsed = json.loads(body)
                    next_match = _NEXT.search(response.headers.get("link", ""))
                    next_link = next_match.group(1) if next_match else None
                    return parsed, next_link, len(body)
    except ProviderRateLimited:
        raise
    except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("github_provider_unavailable") from None


async def collect_github_releases(
    source: ConnectorSource, *, collected_at: datetime
) -> ProviderCollectionPage:
    """Collect at most five validated REST pages of public releases, preserving only metadata and body text."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    config = ConnectorConfig.model_validate(source.configuration)
    owner, repository = config.github_owner, config.github_repository
    if (
        source.provider != "github_releases" or source.type != "api" or not owner or not repository
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
                url, _MAX_TOTAL_BYTES - total_bytes, min(30.0, remaining)
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
            if not isinstance(release, dict):
                raise ValueError("github_response_invalid")
            release_id = release.get("id")
            if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
                raise ValueError("github_response_invalid")
            if release.get("draft") is True:
                continue
            release_owner = release.get("author")
            author = release_owner.get("login") if isinstance(release_owner, dict) else None
            name_value = release.get("name") if isinstance(release.get("name"), str) else None
            name = name_value or ""
            body_value = release.get("body") if isinstance(release.get("body"), str) else None
            body = body_value or ""
            body_text = _plain_text(body)
            tag_value = release.get("tag_name") if isinstance(release.get("tag_name"), str) else None
            tag = tag_value or ""
            node_id = release.get("node_id") if isinstance(release.get("node_id"), str) else None
            html_url = release.get("html_url") if isinstance(release.get("html_url"), str) else None
            tag_candidate = release.get("tag_name") if isinstance(release.get("tag_name"), str) else None
            canonical_url = _release_canonical(html_url, owner, repository, tag_candidate)
            created_at = release.get("created_at") if isinstance(release.get("created_at"), str) else None
            published_at = release.get("published_at") if isinstance(release.get("published_at"), str) else None
            if (
                (created_at is not None and len(created_at) > 64)
                or (published_at is not None and len(published_at) > 64)
                or (html_url is not None and canonical_url is None)
            ):
                raise ValueError("github_response_invalid")
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
            draft = release.get("draft", False)
            prerelease = release.get("prerelease", False)
            if not isinstance(draft, bool) or not isinstance(prerelease, bool):
                raise ValueError("github_response_invalid")
            title = name or tag or f"Release {release_id}"
            source_fields: dict[str, object] = {
                "draft": draft,
                "prerelease": prerelease,
            }
            source_fields_truncated = (
                len(name) > 500 or len(body_text) > 4000 or len(tag) > 255
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
            metadata: dict[str, object] = {
                "provider_record": provider_record.model_dump(mode="json", exclude_none=True),
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
            records.append(
                IngestionRecord(
                    provider_id=identity,
                    content=content[:4000],
                    observed_at=collected_at,
                    version=version,
                    metadata=metadata,
                    collected_at=collected_at,
                )
            )
            if len(records) >= 500:
                break
        if link is None:
            next_url = None
        elif page >= _MAX_PAGES or len(records) >= 500:
            truncated = True
            next_url = None
        else:
            next_url, next_page = _validated_next(link, owner, repository)
            if next_page in seen_pages:
                raise ValueError("github_pagination_invalid")
    if truncated:
        for index, record in enumerate(records):
            record_metadata = dict(record.metadata)
            provider_metadata = ProviderRecordMetadata.model_validate(
                record_metadata["provider_record"]
            ).model_copy(update={"coverage": "truncated"})
            record_metadata["provider_record"] = provider_metadata.model_dump(
                mode="json", exclude_none=True
            )
            records[index] = record.model_copy(update={"metadata": record_metadata})
    return ProviderCollectionPage(
        records=tuple(records),
        coverage="truncated" if truncated else "returned_snapshot",
        next_eligible_at=None,
    )
