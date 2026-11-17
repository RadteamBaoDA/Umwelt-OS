"""Fetch one bounded read-only snapshot from a verified GitHub repository."""

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from modules.connectors.github.normalization import normalize_github_record
from modules.connectors.github.schemas import (
    MAX_GITHUB_PAGE_BYTES,
    GitHubBindingFence,
    GitHubHintClaimProof,
    GitHubSegmentProof,
    GitHubSourceConfig,
)
from modules.connectors.github.sync import (
    _next_segment,
    decode_github_cursor,
    github_segment_path,
)
from modules.connectors.public import ProviderCollectionPage, ProviderRateLimited

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _github_next_page(link_header: str | None, *, path: str, page: int) -> int | None:
    """Accept only a single GitHub next link for the same fixed endpoint and exact next page."""
    next_urls = []
    for part in (link_header or "").split(","):
        sections = [item.strip() for item in part.split(";")]
        if not part.strip():
            continue
        if len(sections) < 2 or sections[0][:1] != "<" or sections[0][-1:] != ">":
            raise ValueError("github_link_invalid")
        rel = next(
            (item[4:].strip().strip('"') for item in sections[1:] if item.startswith("rel=")), None
        )
        if rel not in {"first", "last", "next", "prev"}:
            raise ValueError("github_link_invalid")
        if rel == "next":
            next_urls.append(sections[0][1:-1])
    if len(next_urls) > 1:
        raise ValueError("github_link_invalid")
    if not next_urls:
        return None
    target = urlsplit(next_urls[0])
    current = urlsplit(f"https://api.github.com{path}")
    if (
        target.scheme != "https"
        or target.netloc != "api.github.com"
        or target.path != current.path
        or target.fragment
        or target.username
    ):
        raise ValueError("github_link_invalid")
    params = parse_qs(target.query, keep_blank_values=True)
    current_params = parse_qs(current.query, keep_blank_values=True)
    next_page = params.pop("page", None)
    current_params.pop("page", None)
    if params != current_params or next_page != [str(page + 1)]:
        raise ValueError("github_link_invalid")
    return page + 1


def _encode_page_items(raw_items: object) -> bytes:
    """Validate a decoded GitHub page and return its canonical JSON bytes for hashing.

    Requires a list of at most 100 objects and canonical encoding within ``MAX_GITHUB_PAGE_BYTES``.

    Raises:
        ValueError: ``github_response_invalid`` or ``github_response_too_large``.
    """
    if (
        not isinstance(raw_items, list)
        or len(raw_items) > 100
        or any(not isinstance(item, dict) for item in raw_items)
    ):
        raise ValueError("github_response_invalid")
    encoded = json.dumps(
        raw_items, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_GITHUB_PAGE_BYTES:
        raise ValueError("github_response_too_large")
    return encoded


async def collect_github_segment(
    config: GitHubSourceConfig,
    access_token: str,
    *,
    fence: GitHubBindingFence,
    cursor_before: str | None,
    collected_at: datetime,
    before_request: Callable[[], Awaitable[None]],
    hint_claim: GitHubHintClaimProof | None = None,
) -> GitHubSegmentProof:
    """Fetch one server-selected resource page and return bounded raw proof for transactional owner validation.

    The cursor and numeric repository endpoint come from the active source lease and verified grant.
    The callback rechecks current database fences and shared admission immediately before the only GET.
    """
    if hint_claim is None:
        cursor = decode_github_cursor(cursor_before, fence, collected_at)
        selected = _next_segment(cursor, collected_at)
        if selected is None:
            raise ValueError("github_scope_exhausted")
        _, _, state, page = selected
        resource = state.resource
        path = github_segment_path(fence, state.resource, state, page)
        scan_floor, scan_upper, sweep_revision = state.floor, state.upper, state.sweep_revision
    else:
        resource = hint_claim.resource
        page = hint_claim.reconcile_page if hint_claim.intent == "reconcile" else 1
        path = github_target_path(config, fence, hint_claim)
        scan_floor = scan_upper = collected_at
        sweep_revision = hint_claim.dirty_revision
    await before_request()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            "GET", f"{API}{path}", headers={**HEADERS, "Authorization": f"Bearer {access_token}"}
        ) as response:
            try:
                await _raise_for_provider_limit(response)
            except httpx.HTTPStatusError:
                if hint_claim is None or response.status_code != 403:
                    raise
                return _target_proof(
                    fence,
                    hint_claim,
                    response.status_code,
                    b"",
                    collected_at,
                )
            if hint_claim is not None and response.status_code == 404:
                return _target_proof(
                    fence,
                    hint_claim,
                    response.status_code,
                    b"",
                    collected_at,
                )
            if response.status_code != 200:
                response.raise_for_status()
                raise ValueError("github_response_invalid")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_GITHUB_PAGE_BYTES:
                    raise ValueError("github_response_too_large")
            try:
                raw_items = httpx.Response(200, content=bytes(body)).json()
            except ValueError as exc:
                raise ValueError("github_response_invalid") from exc
            if hint_claim is not None:
                next_page = _github_next_page(response.headers.get("Link"), path=path, page=page)
                raw_items, target_outcome = _target_items(hint_claim, raw_items, next_page)
                if hint_claim.intent == "reconcile" and hint_claim.locator_kind != "repository":
                    # Only fixed resource sweeps own a durable page pointer; a locator read stays single-target.
                    next_page = None
            else:
                next_page = _github_next_page(response.headers.get("Link"), path=path, page=page)
                target_outcome = None
    encoded = _encode_page_items(raw_items)
    return GitHubSegmentProof(
        fence=fence,
        resource=resource,
        page=page,
        sweep_revision=sweep_revision,
        scan_floor=scan_floor,
        scan_upper=scan_upper,
        raw_items=tuple(raw_items),
        raw_sha256=sha256(encoded).hexdigest(),
        transport_bytes=len(body),
        next_page=next_page,
        has_next=next_page is not None,
        collected_at=collected_at,
        hint_claim=hint_claim,
        target_outcome=target_outcome,
    )


def github_target_path(
    config: GitHubSourceConfig,
    fence: GitHubBindingFence,
    claim: GitHubHintClaimProof,
) -> str:
    """Build one numeric-repository target or resource-specific bounded reconcile page.

    Mutable owner/name fields never select the repository. Overflow reconciliation carries its
    page in the durable hint and uses the fixed enabled resource endpoint.
    """
    repo_id = int(fence.repository_id)
    if (
        claim.resource in {"issue", "pull"}
        and claim.locator_kind == "number"
        and claim.locator.isdecimal()
    ):
        endpoint = "issues" if claim.resource == "issue" else "pulls"
        # Numeric repository identity prevents a rename/name-reuse from redirecting a hint.
        return f"/repositories/{repo_id}/{endpoint}/{int(claim.locator)}"
    if (
        claim.resource == "release"
        and claim.locator_kind == "release_id"
        and claim.locator.isdecimal()
    ):
        return f"/repositories/{repo_id}/releases/{int(claim.locator)}"
    if (
        claim.resource == "commit"
        and claim.locator_kind == "sha"
        and re.fullmatch(r"[0-9a-fA-F]{40,64}", claim.locator)
    ):
        return f"/repositories/{repo_id}/commits/{claim.locator}"
    if (
        claim.resource == "commit"
        and claim.locator_kind == "ref"
        and claim.locator.startswith(("refs/heads/", "refs/tags/"))
    ):
        return f"/repositories/{repo_id}/commits?{urlencode({'sha': claim.locator, 'per_page': 1})}"
    if (
        claim.locator_kind == "repository"
        and claim.locator == fence.repository_id
        and claim.intent == "reconcile"
    ):
        endpoint = {"issue": "issues", "pull": "pulls", "commit": "commits", "release": "releases"}[
            claim.resource
        ]
        parameters: dict[str, str | int] = {"per_page": 100, "page": claim.reconcile_page}
        if claim.resource == "issue" or claim.resource == "pull":
            parameters.update({"state": "all", "sort": "updated", "direction": "desc"})
        return f"/repositories/{repo_id}/{endpoint}?{urlencode(parameters)}"
    if claim.locator_kind == "repository" and claim.locator == fence.repository_id:
        return f"/repositories/{repo_id}"
    if claim.locator_kind == "installation" and claim.locator == fence.installation_id:
        return f"/user/installations/{int(claim.locator)}/repositories?per_page=100"
    raise ValueError("github_target_path_invalid")


def _target_items(
    claim: GitHubHintClaimProof,
    payload: Any,
    next_page: int | None,
) -> tuple[list[dict[str, Any]], str]:
    """Project a one-object response or one installation page and preserve ambiguous pagination."""
    if claim.intent == "reconcile" and claim.locator_kind == "repository":
        if (
            not isinstance(payload, list)
            or len(payload) > 100
            or any(not isinstance(item, dict) for item in payload)
        ):
            raise ValueError("github_response_invalid")
        return payload, "found"
    if claim.locator_kind == "installation":
        data = payload.get("repositories") if isinstance(payload, dict) else None
        if (
            not isinstance(data, list)
            or len(data) > 100
            or any(not isinstance(item, dict) for item in data)
        ):
            raise ValueError("github_response_invalid")
        found = any(str(item.get("id")) == claim.repository_id for item in data)
        return [payload], "found" if found else "partial" if next_page is not None else "not_found"
    if claim.locator_kind == "ref":
        if (
            not isinstance(payload, list)
            or len(payload) > 100
            or any(not isinstance(item, dict) for item in payload)
        ):
            raise ValueError("github_response_invalid")
        return payload[:1], "found" if payload else "not_found"
    if not isinstance(payload, dict):
        raise ValueError("github_response_invalid")
    return [payload], "found"


def _target_proof(
    fence: GitHubBindingFence,
    claim: GitHubHintClaimProof,
    status_code: int,
    body: bytes,
    collected_at: datetime,
) -> GitHubSegmentProof:
    """Represent target 403/404 as unverified outcomes; neither response proves deletion."""
    raw_items: tuple[dict[str, Any], ...] = ()
    encoded = json.dumps(raw_items, separators=(",", ":")).encode()
    return GitHubSegmentProof(
        fence=fence,
        resource=claim.resource,
        page=claim.reconcile_page,
        sweep_revision=claim.dirty_revision,
        scan_floor=collected_at,
        scan_upper=collected_at,
        raw_items=raw_items,
        raw_sha256=sha256(encoded).hexdigest(),
        transport_bytes=len(body),
        next_page=None,
        has_next=False,
        collected_at=collected_at,
        hint_claim=claim,
        target_outcome="forbidden" if status_code == 403 else "not_found",
    )


def _rate_limit_deadline(response: httpx.Response, *, secondary: bool = False) -> datetime:
    """Project GitHub retry headers into a bounded UTC deadline, using one minute only for undocumented secondary limits."""
    now = datetime.now(UTC)
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            value = datetime.fromtimestamp(int(reset), UTC)
            if value > now:
                return value
        except (ValueError, OverflowError, OSError):
            pass
    retry = response.headers.get("Retry-After")
    if retry:
        try:
            seconds = float(retry)
            if 0 < seconds <= 24 * 60 * 60:
                return now + timedelta(seconds=seconds)
        except ValueError:
            try:
                value = parsedate_to_datetime(retry)
                if value.tzinfo is not None and value > now:
                    return value.astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                pass
    return now + timedelta(minutes=1 if secondary or response.status_code == 429 else 3)


async def _raise_for_provider_limit(response: httpx.Response) -> None:
    """Raise the shared-provider retry exception for primary, secondary, and 429 limits only."""
    if response.status_code == 429:
        raise ProviderRateLimited(_rate_limit_deadline(response))
    if response.status_code != 403:
        return
    if response.headers.get("X-RateLimit-Remaining") == "0":
        raise ProviderRateLimited(_rate_limit_deadline(response))
    if response.headers.get("Retry-After"):
        raise ProviderRateLimited(_rate_limit_deadline(response, secondary=True))
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("github_response_too_large")
    try:
        value = httpx.Response(response.status_code, content=bytes(body)).json()
    except ValueError:
        value = None
    message = (
        value.get("message", "").casefold()
        if isinstance(value, dict) and isinstance(value.get("message"), str)
        else ""
    )
    if "secondary rate limit" in message or "rate limit exceeded" in message:
        raise ProviderRateLimited(_rate_limit_deadline(response, secondary=True))
    response.raise_for_status()


async def _read_json(client: httpx.AsyncClient, path: str, token: str) -> Any:
    """Read a fixed GitHub API path with a strict byte/time limit and no redirects/proxy env."""
    if not path.startswith("/") or ".." in path or not path.startswith(("/user", "/repositories/")):
        raise ValueError("GitHub API path is outside the allowlist")
    async with client.stream(
        "GET", f"{API}{path}", headers={**HEADERS, "Authorization": f"Bearer {token}"}
    ) as response:
        await _raise_for_provider_limit(response)
        response.raise_for_status()
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("github_response_too_large")
    return httpx.Response(200, content=bytes(payload)).json()


async def collect_github_repository(
    config: GitHubSourceConfig, access_token: str, repository_id: str
) -> ProviderCollectionPage:
    """Collect at most one page per selected resource and mark partial coverage honestly.

    Requests use only fixed read-only endpoint paths. A `Link` header with a next page marks the
    snapshot truncated; pagination state is intentionally owned by the later P09-T2 slice.
    """
    repo_id = int(repository_id)
    paths = []
    if config.include_issues:
        paths.append(("issue", f"/repositories/{repo_id}/issues?per_page=100&state=all"))
    if config.include_pulls:
        paths.append(("pull", f"/repositories/{repo_id}/pulls?per_page=100&state=all"))
    if config.include_commits:
        paths.append(("commit", f"/repositories/{repo_id}/commits?per_page=100"))
    if config.include_releases:
        paths.append(("release", f"/repositories/{repo_id}/releases?per_page=100"))
    mapped: list[tuple[str, dict[str, Any]]] = []
    truncated = False
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False
    ) as client:
        for kind, path in paths:
            if len(path) > 256:
                raise ValueError("github_path_invalid")
            async with client.stream(
                "GET",
                f"{API}{path}",
                headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
            ) as response:
                await _raise_for_provider_limit(response)
                response.raise_for_status()
                truncated = truncated or 'rel="next"' in response.headers.get("Link", "")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ValueError("github_response_too_large")
                raw_items = httpx.Response(200, content=bytes(data)).json()
            if not isinstance(raw_items, list):
                raise ValueError("github_response_invalid")
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("github_response_invalid")
                # GitHub issue lists also include pull request-shaped objects; prevent duplicates.
                if kind == "issue" and "pull_request" in raw:
                    continue
                mapped.append((kind, raw))
                if len(mapped) >= 500:
                    truncated = True
                    break
    coverage = "truncated" if truncated else "returned_snapshot"
    records = tuple(normalize_github_record(kind, raw, coverage=coverage) for kind, raw in mapped)
    return ProviderCollectionPage(records=records, coverage=coverage)
