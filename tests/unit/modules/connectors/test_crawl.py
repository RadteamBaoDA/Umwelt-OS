"""Unit tests for web crawling options, URL normalizers, depth bounds, guards, and rate limiters.

Covers:
- Web crawling options (CrawlRequest, AgentReadRequest, AgentReadCancel, RSSRequest parameter constraints)
- URL normalizers and domain/DNS guards (_dns_safe, validate_public_url, _agent_target_allowed)
- Depth bounds and traversal limits (max_depth queue bounding, max_pages ceiling, redirect bounding)
- Rate limiters, concurrency locks, and byte budgets (_job_lock 429, MAX_BYTES 413, _job_tokens_match)
"""

import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

# Mock bs4 and crawlee before importing crawl module if not installed in the environment
if "bs4" not in sys.modules:
    class MockBeautifulSoup:
        """Lightweight BeautifulSoup mock extracting text and links from HTML."""
        def __init__(self, markup: bytes | str, parser: str = "html.parser") -> None:
            self.text = markup.decode("utf-8", "ignore") if isinstance(markup, bytes) else markup

        def get_text(self, separator: str = " ", strip: bool = False) -> str:
            text = re.sub(r"<[^>]+>", " ", self.text)
            return "\n".join(line.strip() for line in text.split("\n") if line.strip()) if strip else text

        def find_all(self, tag: str, href: bool = False) -> list[dict[str, str]]:
            if tag == "a" and href:
                matches = re.findall(r'href=["\'](.*?)["\']', self.text, re.IGNORECASE)
                return [{"href": m} for m in matches]
            return []

    mock_bs4 = MagicMock()
    mock_bs4.BeautifulSoup = MockBeautifulSoup
    sys.modules["bs4"] = mock_bs4

if "crawlee" not in sys.modules:
    sys.modules["crawlee"] = MagicMock()
    sys.modules["crawlee.crawlers"] = MagicMock()

from modules.connectors.crawl import (
    MAX_BYTES,
    AgentReadRequest,
    _agent_target_allowed,
    _dns_safe,
    _job_lock,
    _job_tokens_match,
    crawl,
    preview_rss,
)
from modules.connectors.public import CrawlRequest, RSSRequest


class TestWebCrawlingOptions:
    """Tests for Pydantic schema validation of crawl and agent-read request options."""

    def test_crawl_request_valid_http_options(self) -> None:
        """Valid CrawlRequest with http mode and default parameters."""
        req = CrawlRequest(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            url="https://example.com/blog",
            mode="http",
            max_pages=5,
            max_depth=1,
            timeout_seconds=30,
        )
        assert str(req.url) == "https://example.com/blog"
        assert req.mode == "http"
        assert req.max_pages == 5
        assert req.max_depth == 1
        assert req.timeout_seconds == 30

    def test_crawl_request_bounds_and_mode_restrictions(self) -> None:
        """CrawlRequest enforces mode pattern, max_pages <= 10, max_depth <= 2, and timeout <= 60."""
        sid = uuid4()
        # Invalid mode
        with pytest.raises(ValidationError, match="pattern"):
            CrawlRequest(source_id=sid, source_generation=1, connector_revision=1, url="https://example.com", mode="curl")

        # Exceeding max_pages (max is 10)
        with pytest.raises(ValidationError, match="less than or equal to 10"):
            CrawlRequest(source_id=sid, source_generation=1, connector_revision=1, url="https://example.com", max_pages=11)

        # Exceeding max_depth (max is 2)
        with pytest.raises(ValidationError, match="less than or equal to 2"):
            CrawlRequest(source_id=sid, source_generation=1, connector_revision=1, url="https://example.com", max_depth=3)

        # Negative depth
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            CrawlRequest(source_id=sid, source_generation=1, connector_revision=1, url="https://example.com", max_depth=-1)

        # Exceeding timeout (max is 60)
        with pytest.raises(ValidationError, match="less than or equal to 60"):
            CrawlRequest(source_id=sid, source_generation=1, connector_revision=1, url="https://example.com", timeout_seconds=61)

    def test_agent_read_request_valid(self) -> None:
        """Valid AgentReadRequest satisfies tight bounds and SHA-256 job token pattern."""
        req = AgentReadRequest(
            job_id=uuid4(),
            operation_id=uuid4(),
            claim_generation=1,
            service_instance_id="instance-1",
            job_token="a" * 64,
            target_url="https://example.com/docs",
            origin="https://example.com",
            path_prefix="/docs",
            max_pages=2,
            timeout_seconds=30,
        )
        assert req.max_pages == 2
        assert req.timeout_seconds == 30
        assert req.job_token == "a" * 64

    def test_agent_read_request_strict_bounds(self) -> None:
        """AgentReadRequest rejects out-of-bound max_pages, timeouts, or non-hex tokens."""
        # Non-hex or wrong-length job_token
        with pytest.raises(ValidationError, match="pattern"):
            AgentReadRequest(
                job_id=uuid4(),
                operation_id=uuid4(),
                claim_generation=1,
                service_instance_id="inst",
                job_token="not-a-64-char-hex-token",
                target_url="https://example.com",
                origin="https://example.com",
                path_prefix="/",
                max_pages=2,
                timeout_seconds=30,
            )

        # max_pages > 3
        with pytest.raises(ValidationError, match="less than or equal to 3"):
            AgentReadRequest(
                job_id=uuid4(),
                operation_id=uuid4(),
                claim_generation=1,
                service_instance_id="inst",
                job_token="f" * 64,
                target_url="https://example.com",
                origin="https://example.com",
                path_prefix="/",
                max_pages=4,
                timeout_seconds=30,
            )

        # timeout_seconds > 45
        with pytest.raises(ValidationError, match="less than or equal to 45"):
            AgentReadRequest(
                job_id=uuid4(),
                operation_id=uuid4(),
                claim_generation=1,
                service_instance_id="inst",
                job_token="f" * 64,
                target_url="https://example.com",
                origin="https://example.com",
                path_prefix="/",
                max_pages=2,
                timeout_seconds=46,
            )


class TestUrlNormalizersAndDomainGuards:
    """Tests for URL DNS safety checks, domain boundaries, and SSRF defenses."""

    @pytest.mark.asyncio
    async def test_dns_safe_calls_validate_public_url(self) -> None:
        """_dns_safe delegates to validate_public_url to check public address resolution."""
        with patch("modules.connectors.crawl.validate_public_url", new_callable=AsyncMock) as mock_val:
            await _dns_safe("https://example.com")
            mock_val.assert_awaited_once_with("https://example.com")

    def test_agent_target_allowed_valid_cases(self) -> None:
        """_agent_target_allowed approves HTTPS targets within matching origin and path prefix."""
        origin = "https://example.com"
        prefix = "/docs"

        # Exact prefix match
        assert _agent_target_allowed("https://example.com/docs", origin, prefix) is True
        # Child path
        assert _agent_target_allowed("https://example.com/docs/api", origin, prefix) is True
        # Root prefix
        assert _agent_target_allowed("https://example.com/anywhere", origin, "/") is True

    def test_agent_target_allowed_rejection_reasons(self) -> None:
        """_agent_target_allowed strictly rejects non-HTTPS, credentials, traversal, queries, and separators."""
        origin = "https://example.com"
        prefix = "/docs"

        # Insecure HTTP
        assert _agent_target_allowed("http://example.com/docs", origin, prefix) is False

        # Credentials
        assert _agent_target_allowed("https://user:pass@example.com/docs", origin, prefix) is False

        # Query strings and fragments
        assert _agent_target_allowed("https://example.com/docs?page=1", origin, prefix) is False
        assert _agent_target_allowed("https://example.com/docs#anchor", origin, prefix) is False

        # Non-standard port
        assert _agent_target_allowed("https://example.com:8443/docs", origin, prefix) is False

        # Path traversal segments
        assert _agent_target_allowed("https://example.com/docs/../private", origin, prefix) is False
        assert _agent_target_allowed("https://example.com/docs/./page", origin, prefix) is False

        # Encoded separators (%2f, %5c, %25)
        assert _agent_target_allowed("https://example.com/docs%2fapi", origin, prefix) is False
        assert _agent_target_allowed("https://example.com/docs%5capi", origin, prefix) is False
        assert _agent_target_allowed("https://example.com/docs%25api", origin, prefix) is False

        # Backslash in path
        assert _agent_target_allowed("https://example.com/docs\\admin", origin, prefix) is False

        # Outside path prefix
        assert _agent_target_allowed("https://example.com/other", origin, prefix) is False

        # Origin mismatch
        assert _agent_target_allowed("https://attacker.com/docs", origin, prefix) is False

        # Length > 2048
        long_url = "https://example.com/docs/" + "a" * 2030
        assert _agent_target_allowed(long_url, origin, prefix) is False


class TestDepthBoundsAndTraversalLimits:
    """Tests for crawl queue depth bounding, max pages capping, and redirect limits."""

    @pytest.mark.asyncio
    async def test_crawl_depth_bounding_and_page_limits(self) -> None:
        """HTTP crawl respects max_depth and max_pages limits without overflowing."""
        req = CrawlRequest(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            url="https://example.com/start",
            mode="http",
            max_pages=2,
            max_depth=1,
            timeout_seconds=10,
        )

        html_root = b'<html><body><a href="/p1">Page 1</a><a href="/p2">Page 2</a></body></html>'
        html_p1 = b'<html><body><a href="/p3">Page 3</a></body></html>'

        # Mock httpx client response stream
        mock_response_root = MagicMock(spec=httpx.Response)  # no __aenter__: real httpx.Response is not an async context manager
        mock_response_root.status_code = 200
        mock_response_root.headers = {"content-type": "text/html"}
        mock_response_root.url = "https://example.com/start"
        mock_response_root.raise_for_status = MagicMock()
        async def aiter_root():
            yield html_root
        mock_response_root.aiter_bytes = aiter_root
        mock_response_root.aclose = AsyncMock()

        mock_response_p1 = MagicMock(spec=httpx.Response)  # no __aenter__: real httpx.Response is not an async context manager
        mock_response_p1.status_code = 200
        mock_response_p1.headers = {"content-type": "text/html"}
        mock_response_p1.url = "https://example.com/p1"
        mock_response_p1.raise_for_status = MagicMock()
        async def aiter_p1():
            yield html_p1
        mock_response_p1.aiter_bytes = aiter_p1
        mock_response_p1.aclose = AsyncMock()

        with patch("modules.connectors.crawl._dns_safe", new_callable=AsyncMock), \
             patch("httpx.AsyncClient.send", side_effect=[mock_response_root, mock_response_p1]):
            records = await crawl(req)

        # Capped by max_pages=2
        assert len(records) == 2
        assert records[0]["metadata"]["url"] == "https://example.com/start"
        assert records[1]["metadata"]["url"] == "https://example.com/p1"
        # Each streamed response must be closed exactly once (httpx.Response has aclose, not __aenter__).
        mock_response_root.aclose.assert_awaited_once()
        mock_response_p1.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_crawl_redirect_limit_exceeded(self) -> None:
        """Redirect loop (> 6 redirects) raises ValueError('Web redirect limit exceeded')."""
        req = CrawlRequest(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            url="https://example.com/loop",
            mode="http",
            max_pages=2,
            max_depth=1,
            timeout_seconds=10,
        )

        # Mock infinite redirect response
        mock_redirect = MagicMock()
        mock_redirect.status_code = 302
        mock_redirect.headers = {"location": "https://example.com/loop"}
        mock_redirect.aclose = AsyncMock()

        with patch("modules.connectors.crawl._dns_safe", new_callable=AsyncMock), \
             patch("httpx.AsyncClient.send", return_value=mock_redirect):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(ValueError, match="Web redirect limit exceeded"):
                await crawl(req)


class TestRateLimitersAndConcurrencyGuards:
    """Tests for job concurrency locking, timing-safe token matching, and byte-budget enforcement."""

    @pytest.mark.asyncio
    async def test_job_lock_429_concurrency_rejection(self) -> None:
        """When _job_lock is acquired, concurrent crawl() and preview_rss() reject with 429."""
        async with _job_lock:
            # Crawl request rejected
            req = CrawlRequest(
                source_id=uuid4(),
                source_generation=1,
                connector_revision=1,
                url="https://example.com",
                mode="http",
            )
            with pytest.raises(HTTPException) as exc_crawl:
                await crawl(req)
            assert exc_crawl.value.status_code == 429
            assert "A browser job is already running" in exc_crawl.value.detail

            # RSS preview rejected
            rss_req = RSSRequest(url="https://example.com/feed.xml")
            with pytest.raises(HTTPException) as exc_rss:
                await preview_rss(rss_req)
            assert exc_rss.value.status_code == 429
            assert "A browser job is already running" in exc_rss.value.detail

    def test_job_tokens_match_timing_safe(self) -> None:
        """_job_tokens_match performs constant-time comparison and rejects mismatched or missing tokens."""
        valid_token = "e" * 64
        req = AgentReadRequest(
            job_id=uuid4(),
            operation_id=uuid4(),
            claim_generation=1,
            service_instance_id="inst",
            job_token=valid_token,
            target_url="https://example.com/docs",
            origin="https://example.com",
            path_prefix="/docs",
            max_pages=1,
            timeout_seconds=10,
        )

        # Matching header token
        assert _job_tokens_match(req, valid_token) is True
        # Mismatched token
        assert _job_tokens_match(req, "f" * 64) is False
        # None header
        assert _job_tokens_match(req, None) is False
        # Empty string
        assert _job_tokens_match(req, "") is False

    @pytest.mark.asyncio
    async def test_crawl_byte_budget_exceeded_413(self) -> None:
        """Exceeding MAX_BYTES (25 MB) raises HTTPException 413 Browser download limit exceeded."""
        req = CrawlRequest(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            url="https://example.com/huge",
            mode="http",
            max_pages=1,
            max_depth=0,
            timeout_seconds=10,
        )

        mock_resp = MagicMock(spec=httpx.Response)  # no __aenter__: real httpx.Response is not an async context manager
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(MAX_BYTES + 1024), "content-type": "text/html"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aclose = AsyncMock()

        with patch("modules.connectors.crawl._dns_safe", new_callable=AsyncMock), \
             patch("httpx.AsyncClient.send", return_value=mock_resp):
            with pytest.raises(HTTPException) as exc:
                await crawl(req)
            assert exc.value.status_code == 413
            assert "Browser download limit exceeded" in exc.value.detail
            mock_resp.aclose.assert_awaited_once()  # closed even when the byte budget aborts the read
