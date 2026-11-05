"""Pinned model-gateway HTTP client pool: reuse, bounds, shutdown, unchanged DNS-pinning/SSRF guarantees."""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from core.model_gateway import pool
from core.model_gateway.transport import ApprovedEndpointTransport, EndpointNetworkPolicyError


@pytest.fixture(autouse=True)
async def _clean_pool() -> Any:
    await pool.close_pinned_clients()
    yield
    await pool.close_pinned_clients()


async def test_same_origin_policy_and_protocol_share_one_client() -> None:
    first = pool.pinned_client(
        "https://api.example.com/v1", ["10.0.0.0/8", "192.168.0.0/16"], protocol="Openai"
    )
    again = pool.pinned_client(
        "https://api.example.com/other/path", ["192.168.0.0/16", "10.0.0.0/8"], protocol="Openai"
    )
    assert first is again  # path and CIDR order are irrelevant to the pinned origin/policy
    assert (
        pool.pinned_client("https://api.example.com", ["10.0.0.0/8"], protocol="Openai")
        is not first
    )
    assert (
        pool.pinned_client(
            "https://api.example.com", ["10.0.0.0/8", "192.168.0.0/16"], protocol="Anthropic"
        )
        is not first
    )
    assert (
        pool.pinned_client(
            "https://api.example.com", ["10.0.0.0/8", "192.168.0.0/16"], allow_global=True
        )
        is not first
    )
    assert (
        pool.pinned_client(
            "https://other.example.com", ["10.0.0.0/8", "192.168.0.0/16"], protocol="Openai"
        )
        is not first
    )
    assert first.follow_redirects is False and first.trust_env is False


async def test_invalid_policy_is_rejected_and_never_cached() -> None:
    with pytest.raises(EndpointNetworkPolicyError):
        pool.pinned_client("https://api.example.com", [])
    with pytest.raises(EndpointNetworkPolicyError):
        pool.pinned_client("https://api.example.com", ["not-a-cidr"])
    assert not pool._POOL


async def test_pool_is_lru_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool, "MAX_POOLED_CLIENTS", 3)
    monkeypatch.setattr(pool, "_schedule_close", lambda _client: None)
    clients = [pool.pinned_client(f"https://h{i}.example.com", ["10.0.0.0/8"]) for i in range(3)]
    pool.pinned_client("https://h0.example.com", ["10.0.0.0/8"])  # touch: h1 becomes least recent
    pool.pinned_client("https://h3.example.com", ["10.0.0.0/8"])
    assert len(pool._POOL) == 3
    assert pool.pinned_client("https://h0.example.com", ["10.0.0.0/8"]) is clients[0]
    assert (
        pool.pinned_client("https://h1.example.com", ["10.0.0.0/8"]) is not clients[1]
    )  # evicted, rebuilt


async def test_sdk_context_exit_does_not_close_the_shared_client() -> None:
    http = pool.pinned_client("https://api.example.com", ["10.0.0.0/8"])
    async with AsyncOpenAI(base_url="https://api.example.com/v1", api_key="k", http_client=http):
        pass
    assert not http.is_closed
    assert pool.pinned_client("https://api.example.com", ["10.0.0.0/8"]) is http


async def test_shutdown_closes_every_pooled_client() -> None:
    clients = [pool.pinned_client(f"https://h{i}.example.com", ["10.0.0.0/8"]) for i in range(2)]
    await pool.close_pinned_clients()
    assert all(client.is_closed for client in clients) and not pool._POOL
    await pool.close_pinned_clients()  # idempotent


async def test_evicted_client_is_closed_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pool, "MAX_POOLED_CLIENTS", 1)
    monkeypatch.setattr(pool, "EVICTION_CLOSE_DELAY_SECONDS", 0.01)
    first = pool.pinned_client("https://a.example.com", ["10.0.0.0/8"])
    pool.pinned_client("https://b.example.com", ["10.0.0.0/8"])
    assert not first.is_closed  # still usable by an in-flight call during the grace period
    for task in list(pool._CLOSERS):
        await task
    assert first.is_closed


async def test_reused_client_still_resolves_and_validates_dns_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding after the first (approved) answer is denied on the second request over the same client."""
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(200, json={"ok": True})

    def build(
        base_url: str, cidrs: Any, *, allow_global: bool = False
    ) -> ApprovedEndpointTransport:
        return ApprovedEndpointTransport(
            httpx.URL(base_url),
            cidrs,
            httpx.MockTransport(handler),
            allow_global=allow_global,
        )

    monkeypatch.setattr(pool, "approved_transport", build)
    answers = iter(["10.1.2.3", "169.254.169.254", "10.1.2.3"])
    resolutions: list[str] = []

    async def getaddrinfo(host: str, port: int | None, **_kw: Any) -> list[Any]:
        resolutions.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port or 443))]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo, raising=False)
    client = pool.pinned_client("https://api.example.com", ["10.0.0.0/8"])
    assert (await client.get("https://api.example.com/v1/models")).status_code == 200
    again = pool.pinned_client("https://api.example.com", ["10.0.0.0/8"])
    assert again is client
    with pytest.raises(
        EndpointNetworkPolicyError
    ):  # DNS now points at link-local: denied, nothing dialled
        await again.get("https://api.example.com/v1/models")
    assert (await again.get("https://api.example.com/v1/models")).status_code == 200
    assert resolutions == ["api.example.com"] * 3  # resolved per request, not per client
    assert seen_hosts == [
        "10.1.2.3",
        "10.1.2.3",
    ]  # the delegate only ever saw the checked literal IP
    with pytest.raises(
        EndpointNetworkPolicyError
    ):  # origin change on a reused client is still refused
        await again.get("https://evil.example.org/v1/models")


async def test_gateway_http_client_uses_the_pool() -> None:
    from core.model_gateway.client import ModelGateway

    def gateway(**overrides: Any) -> ModelGateway:
        return ModelGateway(
            redis=AsyncMock(),
            base_url="https://api.example.com",
            api_key="k",
            destination_id="d",
            scope=_scope(),
            gateway_identity="a" * 64,
            configuration_revision=1,
            before_send=AsyncMock(),
            approved_endpoint_cidrs=("10.0.0.0/8",),
            **overrides,
        )

    first = gateway()._http_client("https://api.example.com/v1")
    assert gateway()._http_client("https://api.example.com/v1") is first
    assert (
        gateway(allow_public_endpoints=True)._http_client("https://api.example.com/v1") is not first
    )
    assert gateway(kind="anthropic")._http_client("https://api.example.com/v1") is not first


def _scope() -> Any:
    from uuid import uuid4

    from core.workspaces.schemas import WorkspaceContext

    return WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)


async def test_pooled_client_never_stores_or_replays_cookies() -> None:
    """A Set-Cookie from one call must not become a Cookie header on the next (shared across tenants)."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "sid=secret; Path=/"})

    client = pool.pinned_client("https://api.example.com", ["10.0.0.0/8"])
    client._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    await client.get("https://api.example.com/a")
    await client.get("https://api.example.com/b")
    assert seen == [None, None]
    assert len(client.cookies.jar) == 0
