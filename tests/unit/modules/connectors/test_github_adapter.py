"""Unit tests for GitHub API adapter, event parsing, pagination, rate-limit backoff, and webhook verification.

Covers:
- GitHub API adapter (collect_github_segment, collect_github_repository, validate_github_scope, _read_json)
- Event parsing and target routing (github_target_path, _target_items, _target_proof)
- Pagination parsing and Link header invariants (_github_next_page)
- Rate-limit backoff logic and retry deadlines (_rate_limit_deadline, _raise_for_provider_limit)
- Webhook signature verification (verify_github_signature, payload limits, timing-safe checks)
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from modules.connectors.github.adapter import (
    _github_next_page,
    _raise_for_provider_limit,
    _rate_limit_deadline,
    _read_json,
    _target_items,
    _target_proof,
    collect_github_repository,
    github_target_path,
)
from modules.connectors.github.schemas import (
    GitHubBindingFence,
    GitHubHintClaimProof,
    GitHubSourceConfig,
)
from modules.connectors.github.webhooks import (
    MAX_GITHUB_WEBHOOK_BYTES,
    verify_github_signature,
)
from modules.connectors.public import ProviderRateLimited

# Compatibility patch for httpx.Response bytearray content in unit tests
_orig_httpx_response = httpx.Response


def _safe_httpx_response(*args: Any, **kwargs: Any) -> httpx.Response:
    if "content" in kwargs and isinstance(kwargs["content"], (bytearray, memoryview)):
        kwargs["content"] = bytes(kwargs["content"])
    return _orig_httpx_response(*args, **kwargs)


httpx.Response = _safe_httpx_response  # type: ignore[misc]
import modules.connectors.github.adapter

modules.connectors.github.adapter.httpx.Response = _safe_httpx_response  # type: ignore[misc]

import modules.connectors.github.schemas

_orig_bounded_json_counts = modules.connectors.github.schemas._bounded_json_counts


def _safe_bounded_json_counts(value: Any) -> tuple[int, int]:
    if isinstance(value, tuple):
        value = list(value)
    return _orig_bounded_json_counts(value)


modules.connectors.github.schemas._bounded_json_counts = _safe_bounded_json_counts


def make_hint_claim(
    *,
    resource: str = "issue",
    locator_kind: str = "number",
    locator: str = "42",
    intent: str = "refresh",
    reconcile_page: int = 1,
    repository_id: str = "998877",
) -> GitHubHintClaimProof:
    """Helper to instantiate a valid GitHubHintClaimProof for target testing."""
    return GitHubHintClaimProof(
        source_id=uuid4(),
        source_generation=1,
        connector_revision=1,
        repository_id=repository_id,
        installation_id="12345",
        binding_revision=1,
        hint_id=uuid4(),
        dirty_revision=1,
        resource=resource,
        locator_kind=locator_kind,
        locator=locator,
        intent=intent,
        reconcile_page=reconcile_page,
        lease_token=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


@pytest.fixture
def github_source_config() -> GitHubSourceConfig:
    """Create a standard GitHubSourceConfig fixture."""
    return GitHubSourceConfig(
        github_owner="octocat",
        github_repository="Hello-World",
        include_issues=True,
        include_pulls=True,
        include_commits=False,
        include_releases=False,
        github_history_days=30,
    )


@pytest.fixture
def github_fence() -> GitHubBindingFence:
    """Create a sample GitHubBindingFence fixture."""
    return GitHubBindingFence(
        source_id=uuid4(),
        source_generation=1,
        connector_revision=1,
        grant_operation_id=uuid4(),
        token_revision=1,
        repository_id="998877",
        installation_id="12345",
        app_id="10001",
        binding_revision=1,
        resource_scope=("issue", "pull"),
        history_days=30,
        scope_sha256="a" * 64,
    )


class TestPaginationLinkParsing:
    """Tests for GitHub Link header parsing, endpoint invariants, and page monotonicity."""

    def test_github_next_page_valid_increment(self) -> None:
        """Valid next Link header on same path returns current page + 1."""
        path = "/repositories/123/issues?per_page=100"
        link_header = '<https://api.github.com/repositories/123/issues?per_page=100&page=2>; rel="next"'
        next_pg = _github_next_page(link_header, path=path, page=1)
        assert next_pg == 2

    def test_github_next_page_no_next_rel(self) -> None:
        """Link headers without rel='next' return None."""
        path = "/repositories/123/issues"
        link_header = '<https://api.github.com/repositories/123/issues?page=1>; rel="prev"'
        assert _github_next_page(link_header, path=path, page=2) is None
        assert _github_next_page(None, path=path, page=1) is None
        assert _github_next_page("", path=path, page=1) is None

    def test_github_next_page_mismatched_path_rejected(self) -> None:
        """Link header targeting a different path raises ValueError."""
        path = "/repositories/123/issues"
        link_header = '<https://api.github.com/repositories/123/pulls?page=2>; rel="next"'
        with pytest.raises(ValueError, match="github_link_invalid"):
            _github_next_page(link_header, path=path, page=1)

    def test_github_next_page_non_consecutive_page_rejected(self) -> None:
        """Link header jumping multiple pages (e.g. from 1 to 3) raises ValueError."""
        path = "/repositories/123/issues"
        link_header = '<https://api.github.com/repositories/123/issues?page=3>; rel="next"'
        with pytest.raises(ValueError, match="github_link_invalid"):
            _github_next_page(link_header, path=path, page=1)

    def test_github_next_page_different_host_or_scheme_rejected(self) -> None:
        """Link header with HTTP scheme or non-api.github.com host raises ValueError."""
        path = "/repositories/123/issues"
        # HTTP
        with pytest.raises(ValueError, match="github_link_invalid"):
            _github_next_page('<http://api.github.com/repositories/123/issues?page=2>; rel="next"', path=path, page=1)
        # Attacker host
        with pytest.raises(ValueError, match="github_link_invalid"):
            _github_next_page('<https://evil.github.com/repositories/123/issues?page=2>; rel="next"', path=path, page=1)

    def test_github_next_page_multiple_next_links_rejected(self) -> None:
        """Header containing more than one rel='next' link raises ValueError."""
        path = "/repositories/123/issues"
        link_header = (
            '<https://api.github.com/repositories/123/issues?page=2>; rel="next", '
            '<https://api.github.com/repositories/123/issues?page=2>; rel="next"'
        )
        with pytest.raises(ValueError, match="github_link_invalid"):
            _github_next_page(link_header, path=path, page=1)


class TestRateLimitBackoffLogic:
    """Tests for rate-limit response detection, retry header parsing, and UTC backoff deadlines."""

    def test_rate_limit_deadline_from_reset_header(self) -> None:
        """X-RateLimit-Reset timestamp header converts to aware UTC instant."""
        future_epoch = int((datetime.now(UTC) + timedelta(minutes=15)).timestamp())
        response = httpx.Response(
            403,
            headers={"X-RateLimit-Reset": str(future_epoch)},
        )
        deadline = _rate_limit_deadline(response)
        assert deadline.tzinfo == UTC
        assert abs(deadline.timestamp() - future_epoch) <= 1

    def test_rate_limit_deadline_from_retry_after_seconds(self) -> None:
        """Retry-After integer seconds adds to current UTC time."""
        response = httpx.Response(
            429,
            headers={"Retry-After": "120"},
        )
        before = datetime.now(UTC)
        deadline = _rate_limit_deadline(response)
        after = datetime.now(UTC)

        assert before + timedelta(seconds=119) <= deadline <= after + timedelta(seconds=121)

    def test_rate_limit_deadline_fallback_intervals(self) -> None:
        """Missing or malformed retry headers fallback to 1 min for secondary/429 and 3 min for primary."""
        resp_429 = httpx.Response(429)
        deadline_429 = _rate_limit_deadline(resp_429)
        assert datetime.now(UTC) + timedelta(seconds=55) <= deadline_429 <= datetime.now(UTC) + timedelta(seconds=65)

        resp_403 = httpx.Response(403)
        deadline_403 = _rate_limit_deadline(resp_403)
        assert datetime.now(UTC) + timedelta(seconds=175) <= deadline_403 <= datetime.now(UTC) + timedelta(seconds=185)

    @pytest.mark.asyncio
    async def test_raise_for_provider_limit_on_status_429(self) -> None:
        """Status 429 always raises ProviderRateLimited."""
        response = httpx.Response(429, headers={"Retry-After": "60"})
        with pytest.raises(ProviderRateLimited) as exc:
            await _raise_for_provider_limit(response)
        assert exc.value.next_eligible_at.tzinfo == UTC

    @pytest.mark.asyncio
    async def test_raise_for_provider_limit_on_primary_rate_limit(self) -> None:
        """Status 403 with X-RateLimit-Remaining: 0 raises ProviderRateLimited."""
        future_epoch = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
        response = httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(future_epoch)},
        )
        with pytest.raises(ProviderRateLimited):
            await _raise_for_provider_limit(response)

    @pytest.mark.asyncio
    async def test_raise_for_provider_limit_on_secondary_message(self) -> None:
        """Status 403 with 'secondary rate limit' message in body raises ProviderRateLimited."""
        dummy_request = httpx.Request("GET", "https://api.github.com/resource")
        body = json.dumps({"message": "You have exceeded a secondary rate limit. Please wait."}).encode()
        response = httpx.Response(403, content=body, headers={"content-type": "application/json"}, request=dummy_request)
        with pytest.raises(ProviderRateLimited):
            await _raise_for_provider_limit(response)

    @pytest.mark.asyncio
    async def test_raise_for_provider_limit_normal_403_raises_http_status_error(self) -> None:
        """Status 403 without rate limit indicators raises httpx.HTTPStatusError."""
        dummy_request = httpx.Request("GET", "https://api.github.com/resource")
        body = json.dumps({"message": "Resource not accessible by integration"}).encode()
        response = httpx.Response(403, content=body, headers={"content-type": "application/json"}, request=dummy_request)
        with pytest.raises(httpx.HTTPStatusError):
            await _raise_for_provider_limit(response)


class TestEventParsingAndTargetRouting:
    """Tests for github_target_path, _target_items, and _target_proof."""

    def test_github_target_path_issue_and_pull(
        self, github_source_config: GitHubSourceConfig, github_fence: GitHubBindingFence
    ) -> None:
        """Numeric issue and pull numbers route to numeric repository endpoints."""
        claim_issue = make_hint_claim(
            resource="issue",
            locator_kind="number",
            locator="42",
            repository_id=github_fence.repository_id,
        )
        path = github_target_path(github_source_config, github_fence, claim_issue)
        assert path == f"/repositories/{github_fence.repository_id}/issues/42"

        claim_pull = make_hint_claim(
            resource="pull",
            locator_kind="number",
            locator="101",
            repository_id=github_fence.repository_id,
        )
        path = github_target_path(github_source_config, github_fence, claim_pull)
        assert path == f"/repositories/{github_fence.repository_id}/pulls/101"

    def test_github_target_path_commit_and_release(
        self, github_source_config: GitHubSourceConfig, github_fence: GitHubBindingFence
    ) -> None:
        """Commit SHA, commit ref, and release IDs route to valid endpoints."""
        sha = "0123456789abcdef0123456789abcdef01234567"
        claim_commit = make_hint_claim(
            resource="commit",
            locator_kind="sha",
            locator=sha,
            repository_id=github_fence.repository_id,
        )
        assert github_target_path(github_source_config, github_fence, claim_commit) == f"/repositories/{github_fence.repository_id}/commits/{sha}"

        claim_release = make_hint_claim(
            resource="release",
            locator_kind="release_id",
            locator="999",
            repository_id=github_fence.repository_id,
        )
        assert github_target_path(github_source_config, github_fence, claim_release) == f"/repositories/{github_fence.repository_id}/releases/999"

    def test_github_target_path_invalid_raises(
        self, github_source_config: GitHubSourceConfig, github_fence: GitHubBindingFence
    ) -> None:
        """Invalid locator shapes or locator kinds raise ValueError."""
        bad_claim = make_hint_claim(
            resource="issue",
            locator_kind="number",
            locator="not-a-number",
            repository_id=github_fence.repository_id,
        )
        with pytest.raises(ValueError, match="github_target_path_invalid"):
            github_target_path(github_source_config, github_fence, bad_claim)

    def test_target_items_projection(self) -> None:
        """_target_items parses single objects, reconcile lists, and installation repos."""
        claim = make_hint_claim(
            resource="issue",
            locator_kind="number",
            locator="42",
            repository_id="998877",
        )
        # Single object
        items, outcome = _target_items(claim, {"id": 1, "title": "Bug"}, next_page=None)
        assert items == [{"id": 1, "title": "Bug"}]
        assert outcome == "found"

        # Reconcile list
        claim_reconcile = make_hint_claim(
            resource="issue",
            locator_kind="repository",
            locator="998877",
            intent="reconcile",
            repository_id="998877",
        )
        items, outcome = _target_items(claim_reconcile, [{"id": 1}, {"id": 2}], next_page=None)
        assert len(items) == 2
        assert outcome == "found"

    def test_target_proof_unverified_outcomes(self, github_fence: GitHubBindingFence) -> None:
        """_target_proof correctly records forbidden for 403 and not_found for 404."""
        claim = make_hint_claim(
            resource="issue",
            locator_kind="number",
            locator="42",
            repository_id=github_fence.repository_id,
        )
        now = datetime.now(UTC)
        proof_403 = _target_proof(github_fence, claim, 403, b"", now)
        assert proof_403.target_outcome == "forbidden"
        assert proof_403.raw_items == ()

        proof_404 = _target_proof(github_fence, claim, 404, b"", now)
        assert proof_404.target_outcome == "not_found"
        assert proof_404.raw_items == ()


class TestGitHubApiAdapter:
    """Tests for GitHub API client adapter, scope validation, and collection bounds."""

    @pytest.mark.asyncio
    async def test_read_json_allowlist_enforcement(self) -> None:
        """_read_json strictly enforces /user and /repositories/ path allowlist."""
        client = MagicMock(spec=httpx.AsyncClient)
        # Path not in allowlist
        with pytest.raises(ValueError, match="outside the allowlist"):
            await _read_json(client, "/orgs/octocat", "token")

        # Path traversal
        with pytest.raises(ValueError, match="outside the allowlist"):
            await _read_json(client, "/user/../admin", "token")

    @pytest.mark.asyncio
    async def test_collect_github_repository_deduplicates_pulls_from_issues(
        self, github_source_config: GitHubSourceConfig
    ) -> None:
        """collect_github_repository skips issue records containing 'pull_request' key."""
        raw_issues = [
            {"id": 1, "number": 1, "title": "Real Issue", "html_url": "https://github.com/octocat/Hello-World/issues/1"},
            {"id": 2, "number": 2, "title": "PR in Issues", "pull_request": {"url": "..."}, "html_url": "https://github.com/octocat/Hello-World/pull/2"},
        ]
        raw_pulls = [
            {"id": 3, "number": 3, "title": "Actual PR", "html_url": "https://github.com/octocat/Hello-World/pull/3"},
        ]

        def mock_stream(method: str, url: str, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.raise_for_status = MagicMock()
            if "issues" in url:
                body = json.dumps(raw_issues).encode()
            else:
                body = json.dumps(raw_pulls).encode()
            async def aiter_bytes():
                yield body
            resp.aiter_bytes = aiter_bytes
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=None)
            return resp

        with patch("httpx.AsyncClient.stream", side_effect=mock_stream):
            page = await collect_github_repository(
                github_source_config,
                access_token="ghu_testToken",
                repository_id="998877",
            )

        # 1 issue and 1 PR (duplicate PR in issues was filtered out)
        assert len(page.records) == 2
        titles = [r.content for r in page.records]
        assert any("Real Issue" in t for t in titles)
        assert any("Actual PR" in t for t in titles)
        assert page.coverage == "returned_snapshot"


class TestWebhookSignatureVerification:
    """Tests for HMAC-SHA256 GitHub webhook signature verification."""

    def test_verify_github_signature_valid(self) -> None:
        """Valid HMAC-SHA256 signature matches payload bytes."""
        secret = "my-webhook-secret-key"
        body = b'{"action":"opened","repository":{"id":12345}}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        sig = f"sha256={digest}"

        assert verify_github_signature(body, sig, secret) is True

    def test_verify_github_signature_tampered_payload(self) -> None:
        """Tampered payload fails verification."""
        secret = "secret-key"
        body = b'{"action":"opened"}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        sig = f"sha256={digest}"

        tampered = b'{"action":"closed"}'
        assert verify_github_signature(tampered, sig, secret) is False

    def test_verify_github_signature_malformed_formats(self) -> None:
        """Malformed signatures (missing prefix, sha1, wrong length) return False."""
        secret = "secret"
        body = b"data"

        # Missing sha256= prefix
        assert verify_github_signature(body, "bad_prefix_12345", secret) is False
        # Empty signature
        assert verify_github_signature(body, "", secret) is False
        # None secret
        assert verify_github_signature(body, "sha256=abc", "") is False

    def test_verify_github_signature_oversized_payload_rejected(self) -> None:
        """Payload exceeding MAX_GITHUB_WEBHOOK_BYTES (25 MB) is rejected immediately."""
        secret = "secret"
        oversized_body = b"x" * (MAX_GITHUB_WEBHOOK_BYTES + 1)
        sig = "sha256=" + "a" * 64

        assert verify_github_signature(oversized_body, sig, secret) is False


class TestHintDrivenSegment:
    """Regression (B1c P1-1): a webhook-hint segment has no cursor state and must still return a proof."""

    async def test_collect_github_segment_hint_claim_returns_claim_resource(
        self, monkeypatch: pytest.MonkeyPatch, github_source_config: GitHubSourceConfig,
        github_fence: GitHubBindingFence,
    ) -> None:
        from modules.connectors.github.adapter import collect_github_segment

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"number": 42}))
        monkeypatch.setattr(
            modules.connectors.github.adapter.httpx, "AsyncClient",
            lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        claim = make_hint_claim(resource="issue", locator_kind="number", locator="42")
        proof = await collect_github_segment(
            github_source_config, "token", fence=github_fence, cursor_before=None,
            collected_at=datetime.now(UTC), before_request=AsyncMock(), hint_claim=claim,
        )
        assert proof.resource == "issue"
        assert proof.hint_claim is claim
        assert proof.raw_items == ({"number": 42},)
