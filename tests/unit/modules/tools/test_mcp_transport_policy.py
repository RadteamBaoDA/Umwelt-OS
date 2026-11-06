"""Behavioral tests for the bounded MCP HTTP transport: quotas, destination policy and response guards."""

import time

import httpx2
import pytest

from modules.tools.mcp_transport import (
    McpOperationNetworkBudget,
    McpPinnedHttpTransport,
    McpTransportError,
    create_mcp_http_client,
)


async def _allow() -> bool:
    return True


def _budget(**overrides: object) -> McpOperationNetworkBudget:
    params: dict[str, object] = {
        "deadline": time.monotonic() + 30,
        "max_requests": 2,
        "max_request_bytes": 10,
        "max_response_bytes": 10,
        "max_inflight_requests": 2,
        "max_teardown_requests": 1,
        "max_teardown_bytes": 4,
        "before_request": _allow,
    }
    params.update(overrides)
    return McpOperationNetworkBudget(**params)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field", ["max_requests", "max_request_bytes", "max_response_bytes", "deadline"]
)
def test_budget_rejects_non_positive_limits(field: str) -> None:
    with pytest.raises(ValueError):
        _budget(**{field: 0})


def test_budget_expired_deadline_fails_closed() -> None:
    budget = _budget(deadline=0.001)
    with pytest.raises(TimeoutError):
        budget.remaining_seconds()


async def test_request_quota_counts_and_inflight_slots_are_released() -> None:
    budget = _budget(max_requests=2, max_inflight_requests=1)
    await budget.claim_request()
    with pytest.raises(McpTransportError, match="too many open"):
        await budget.claim_request()
    await budget.finish_request()
    await budget.finish_request()  # extra release never goes negative
    await budget.claim_request()
    await budget.finish_request()
    with pytest.raises(McpTransportError, match="request limit"):
        await budget.claim_request()


async def test_teardown_requests_have_a_separate_allowance() -> None:
    budget = _budget(max_requests=1)
    await budget.claim_request(teardown=True)
    await budget.finish_request()
    with pytest.raises(McpTransportError, match="teardown request limit"):
        await budget.claim_request(teardown=True)
    await budget.claim_request()  # ordinary quota untouched by teardown


async def test_byte_quotas_reject_overflow_and_negative_counts() -> None:
    budget = _budget()
    await budget.claim_request_bytes(10)
    with pytest.raises(McpTransportError, match="request byte limit"):
        await budget.claim_request_bytes(1)
    await budget.claim_response_bytes(10)
    with pytest.raises(McpTransportError, match="response byte limit"):
        await budget.claim_response_bytes(1)
    with pytest.raises(McpTransportError, match="invalid byte count"):
        await budget.claim_request_bytes(-1)
    with pytest.raises(McpTransportError, match="invalid byte count"):
        await budget.claim_response_bytes(-1)


async def test_teardown_bytes_share_one_allowance_across_directions() -> None:
    budget = _budget(max_teardown_bytes=4)
    await budget.claim_request_bytes(3, teardown=True)
    with pytest.raises(McpTransportError, match="teardown byte limit"):
        await budget.claim_response_bytes(2, teardown=True)
    with pytest.raises(McpTransportError, match="teardown byte limit"):
        await budget.claim_request_bytes(2, teardown=True)
    await budget.claim_response_bytes(1, teardown=True)


async def _resolve(
    host: str,
    scheme: str = "https",
    port: int = 443,
    cidrs: dict[tuple[str, str, int], tuple[str, ...]] | None = None,
) -> str:
    transport = McpPinnedHttpTransport(
        f"{scheme}://{host}:{port}/mcp", _budget(), approved_destination_cidrs=cidrs
    )
    try:
        return await transport._resolve_destination((scheme, host, port))
    finally:
        await transport.aclose()


async def test_public_https_literal_is_allowed() -> None:
    assert await _resolve("93.184.216.34") == "93.184.216.34"


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "0.0.0.0", "224.0.0.1", "10.0.0.5", "127.0.0.1"],  # noqa: S104 - literal under test
)
async def test_https_rejects_special_use_and_private_literals(host: str) -> None:
    with pytest.raises(McpTransportError):
        await _resolve(host)


async def test_https_private_literal_needs_exact_origin_cidr_exception() -> None:
    cidrs = {("https", "10.0.0.5", 443): ("10.0.0.0/24",)}
    assert await _resolve("10.0.0.5", cidrs=cidrs) == "10.0.0.5"
    with pytest.raises(McpTransportError):
        await _resolve("10.0.1.5", cidrs={("https", "10.0.1.5", 443): ("10.0.0.0/24",)})


async def test_plain_http_requires_approved_private_or_loopback_cidr() -> None:
    with pytest.raises(McpTransportError, match="approved private or loopback"):
        await _resolve("93.184.216.34", "http", 80)
    with pytest.raises(McpTransportError, match="approved private or loopback"):
        await _resolve("10.0.0.5", "http", 80)
    cidrs = {("http", "10.0.0.5", 80): ("10.0.0.0/8",)}
    assert await _resolve("10.0.0.5", "http", 80, cidrs) == "10.0.0.5"
    loop = {("http", "127.0.0.1", 8080): ("127.0.0.0/8",)}
    assert await _resolve("127.0.0.1", "http", 8080, loop) == "127.0.0.1"


async def test_ipv4_mapped_metadata_address_is_rejected() -> None:
    transport = McpPinnedHttpTransport("https://93.184.216.34/mcp", _budget())
    with pytest.raises(McpTransportError):
        await transport._resolve_destination(("https", "::ffff:169.254.169.254", 443))
    await transport.aclose()


async def test_invalid_deployment_cidr_is_rejected() -> None:
    with pytest.raises(McpTransportError, match="exception is invalid"):
        await _resolve("10.0.0.5", cidrs={("https", "10.0.0.5", 443): ("not-a-cidr",)})


def test_transport_rejects_credentialed_or_malformed_endpoint() -> None:
    with pytest.raises(McpTransportError):
        McpPinnedHttpTransport("ftp://example.com/mcp", _budget())


class _Chunks(httpx2.AsyncByteStream):
    """Unsized response body whose total exceeds the 10-byte test budget."""

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"x" * 6
        yield b"x" * 6


class _FakeDelegate:
    """Stand-in socket delegate returning a canned response and recording the pinned request."""

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response

    async def aclose(self) -> None:
        return None


async def _send(
    request: httpx2.Request,
    response: httpx2.Response | None = None,
    **budget_overrides: object,
) -> tuple[httpx2.Response, _FakeDelegate, McpOperationNetworkBudget]:
    budget = _budget(**budget_overrides)
    transport = McpPinnedHttpTransport("https://93.184.216.34/mcp", budget)
    await transport._delegate.aclose()
    delegate = _FakeDelegate(response or httpx2.Response(200, content=b"ok"))
    transport._delegate = delegate  # type: ignore[assignment]
    result = await transport.handle_async_request(request)
    return result, delegate, budget


async def test_request_is_pinned_with_identity_encoding_and_sni() -> None:
    request = httpx2.Request(
        "POST", "https://93.184.216.34/mcp", content=b"hi", headers={"accept-encoding": "gzip"}
    )
    response, delegate, budget = await _send(request)
    pinned = delegate.requests[0]
    assert pinned.headers["accept-encoding"] == "identity"
    assert pinned.extensions["sni_hostname"] == "93.184.216.34"
    assert response.status_code == 200
    body = b"".join([chunk async for chunk in response.stream])  # type: ignore[union-attr]
    assert body == b"ok"
    assert budget._inflight_requests == 0


async def test_origin_change_is_rejected_and_slot_released() -> None:
    budget = _budget()
    transport = McpPinnedHttpTransport("https://93.184.216.34/mcp", budget)
    request = httpx2.Request("GET", "https://93.184.216.35/mcp")
    with pytest.raises(McpTransportError, match="changed the configured origin"):
        await transport.handle_async_request(request)
    assert budget._inflight_requests == 0
    await transport.aclose()


async def test_auth_rejections_and_compressed_responses_are_refused() -> None:
    request = httpx2.Request("GET", "https://93.184.216.34/mcp")
    with pytest.raises(PermissionError):
        await _send(request, httpx2.Response(401))
    with pytest.raises(McpTransportError, match="Compressed"):
        await _send(
            request, httpx2.Response(200, headers={"content-encoding": "gzip"}, content=b"x")
        )


async def test_declared_oversized_response_is_refused_but_garbage_length_is_bounded_later() -> None:
    request = httpx2.Request("GET", "https://93.184.216.34/mcp")
    with pytest.raises(McpTransportError, match="exceeds"):
        await _send(request, httpx2.Response(200, headers={"content-length": "999"}, content=b"x"))
    response, _, _ = await _send(
        request, httpx2.Response(200, headers={"content-length": "abc"}, content=b"x")
    )
    assert response.status_code == 200


async def test_response_body_over_budget_is_cut_off() -> None:
    request = httpx2.Request("GET", "https://93.184.216.34/mcp")
    response, _, budget = await _send(request, httpx2.Response(200, stream=_Chunks()))
    with pytest.raises(McpTransportError, match="response byte limit"):
        async for _ in response.stream:  # type: ignore[union-attr]
            pass
    assert budget._inflight_requests == 0


async def test_plain_http_with_credentials_and_failed_fence_are_refused() -> None:
    budget = _budget()
    transport = McpPinnedHttpTransport(
        "http://10.0.0.5/mcp",
        budget,
        approved_destination_cidrs={("http", "10.0.0.5", 80): ("10.0.0.0/8",)},
    )
    with pytest.raises(McpTransportError, match="cannot carry credentials"):
        await transport.handle_async_request(
            httpx2.Request("GET", "http://10.0.0.5/mcp", headers={"authorization": "Bearer x"})
        )

    async def deny() -> bool:
        return False

    fenced = McpPinnedHttpTransport("https://93.184.216.34/mcp", _budget(before_request=deny))
    with pytest.raises(McpTransportError, match="fence"):
        await fenced.handle_async_request(httpx2.Request("GET", "https://93.184.216.34/mcp"))
    await transport.aclose()
    await fenced.aclose()


async def test_client_factory_sets_bearer_and_disables_redirects() -> None:
    client = create_mcp_http_client(
        "https://93.184.216.34/mcp", "tok", _budget(), timeout_seconds=5
    )
    assert client.headers["authorization"] == "Bearer tok"
    assert client.headers["accept-encoding"] == "identity"
    assert client.follow_redirects is False
    await client.aclose()
    bare = create_mcp_http_client("https://93.184.216.34/mcp", None, _budget(), timeout_seconds=5)
    assert "authorization" not in bare.headers
    await bare.aclose()
