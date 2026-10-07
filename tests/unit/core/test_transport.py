"""W1b: ``_is_public``, ``allow_global`` and the web search transport factory (review P2-2, W1a P2-1)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.model_gateway.transport import (
    ApprovedEndpointTransport,
    EndpointNetworkPolicyError,
    _is_public,
    approved_http_client,
    approved_web_search_transport,
)
from modules.chat.web_search import search


@pytest.mark.parametrize(
    "value",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254 (is_global True on 3.12)
        "64:ff9b::a00:1",  # NAT64 -> 10.0.0.1 (is_global True on 3.12)
        "64:ff9b:1::a00:1",  # local-use NAT64 /48
        "2002:a00:1::",  # 6to4 -> 10.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo, client 10.0.0.1
        "::ffff:10.0.0.1",  # IPv4-mapped
        "::a00:1",  # IPv4-compatible
        "fec0::1",  # deprecated site-local
        "ff02::1",
        "224.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "::",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "fc00::1",
        "fe80::1",
        "240.0.0.1",
        "192.0.2.1",
    ],
)
def test_is_public_denies(value: str) -> None:
    assert _is_public(ipaddress.ip_address(value)) is False


@pytest.mark.parametrize(
    "value",
    ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "64:ff9b::808:808", "2002:808:808::", "::ffff:8.8.8.8"],
)
def test_is_public_allows(value: str) -> None:
    assert _is_public(ipaddress.ip_address(value)) is True


def _answer(ip: str) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class _Delegate(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json={"results": []})


def _global(delegate: _Delegate, cidrs: tuple[str, ...] = ()) -> ApprovedEndpointTransport:
    return ApprovedEndpointTransport(httpx.URL("https://api.example.com"), cidrs, delegate, allow_global=True)


async def _send(transport: ApprovedEndpointTransport, *answers: list[str]) -> list[httpx.Response | Exception]:
    resolver = AsyncMock(side_effect=[[_answer(ip) for ip in ips] for ips in answers])
    out: list[httpx.Response | Exception] = []
    with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
        for _ in answers:
            try:
                out.append(await transport.handle_async_request(httpx.Request("GET", "https://api.example.com/s")))
            except EndpointNetworkPolicyError as exc:
                out.append(exc)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ips", [["10.0.0.1"], ["169.254.169.254"], ["64:ff9b::a9fe:a9fe"], ["::1"], ["8.8.8.8", "10.0.0.1"]]
)
async def test_allow_global_rejects_private_answer(ips: list[str]) -> None:
    delegate = _Delegate()
    [result] = await _send(_global(delegate), ips)
    assert isinstance(result, EndpointNetworkPolicyError)
    assert delegate.seen == []


@pytest.mark.asyncio
async def test_allow_global_pins_public_ip_and_keeps_host_and_sni() -> None:
    delegate = _Delegate()
    [result] = await _send(_global(delegate), ["2606:4700:4700::1111", "8.8.8.8"])
    assert isinstance(result, httpx.Response)
    pinned = delegate.seen[0]
    assert pinned.url.host == "2606:4700:4700::1111"
    assert pinned.headers["Host"] == "api.example.com"
    assert pinned.extensions["sni_hostname"] == "api.example.com"


@pytest.mark.asyncio
async def test_allow_global_cidrs_admit_listed_private_range() -> None:
    delegate = _Delegate()
    [result] = await _send(_global(delegate, ("172.18.0.0/16",)), ["172.18.0.5"])
    assert isinstance(result, httpx.Response)


@pytest.mark.asyncio
async def test_dns_rebinding_public_then_private_is_rejected() -> None:
    """Each request re-resolves and re-checks; the dialled address is always the checked literal."""
    delegate = _Delegate()
    first, second = await _send(_global(delegate), ["8.8.8.8"], ["169.254.169.254"])
    assert isinstance(first, httpx.Response)
    assert isinstance(second, EndpointNetworkPolicyError)
    assert [r.url.host for r in delegate.seen] == ["8.8.8.8"]


@pytest.mark.asyncio
async def test_default_mode_unchanged() -> None:
    """Without allow_global a public address outside the CIDRs is still denied and empty CIDRs fail closed."""
    with pytest.raises(EndpointNetworkPolicyError, match="unavailable"):
        ApprovedEndpointTransport(httpx.URL("https://api.example.com"), (), _Delegate())
    delegate = _Delegate()
    transport = ApprovedEndpointTransport(httpx.URL("https://api.example.com"), ("10.0.0.0/8",), delegate)
    denied, allowed = await _send(transport, ["8.8.8.8"], ["10.0.0.7"])
    assert isinstance(denied, EndpointNetworkPolicyError)
    assert isinstance(allowed, httpx.Response)
    assert delegate.seen[0].url.host == "10.0.0.7"
    for public in ("8.8.8.8", "64:ff9b::a9fe:a9fe"):  # nothing becomes "public" in default mode
        assert not transport._approved(ipaddress.ip_address(public))
    client = approved_http_client("https://api.example.com", ("10.0.0.0/8",))
    assert client._transport._allow_global is False  # type: ignore[attr-defined]
    assert client.follow_redirects is False and client._trust_env is False


def test_factory_enforces_host_allowlist() -> None:
    with pytest.raises(EndpointNetworkPolicyError, match="not allowed"):
        approved_web_search_transport("https://evil.example.com", {"api.tavily.com"})
    with pytest.raises(EndpointNetworkPolicyError, match="invalid"):
        approved_web_search_transport("https://user@api.tavily.com", {"api.tavily.com"})
    assert approved_web_search_transport("https://api.tavily.com:8443", {"api.tavily.com:8443"})
    transport = approved_web_search_transport("https://API.tavily.com", {"api.tavily.com"})
    delegate = transport._delegate
    assert isinstance(delegate, httpx.AsyncHTTPTransport)
    assert delegate._pool._retries == 0 and delegate._pool._http2 is False  # type: ignore[attr-defined]
    assert transport._allow_global is True and transport._networks == ()


@pytest.mark.asyncio
async def test_factory_transport_accepted_by_w1a_search() -> None:
    transport = approved_web_search_transport("https://api.tavily.com", {"api.tavily.com"})
    delegate = _Delegate()
    transport._delegate = delegate
    resolver = AsyncMock(return_value=[_answer("8.8.8.8")])
    with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
        assert await search("tavily", "https://api.tavily.com", "k", "hello", transport=transport) == []
    assert delegate.seen[0].url.host == "8.8.8.8"
    assert delegate.seen[0].headers["Host"] == "api.tavily.com"
