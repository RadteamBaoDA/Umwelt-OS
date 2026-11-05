"""Egress address policy: NAT64/compat/6to4 encodings of metadata and private IPv4 are never public."""

# ruff: noqa: S104
from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock, patch

import pytest

from core.net_policy import embedded_ipv4, is_public_address

NOT_PUBLIC = [
    "169.254.169.254",
    "::ffff:169.254.169.254",
    "::a9fe:a9fe",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::a9fe:a9fe",
    "2002:a9fe:a9fe::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "64:ff9b::a00:1",
    "100.64.0.1",
    "fd00:ec2::254",
    "::1",
    "::",
    "0.0.0.0",
    "100::1",
    "10.0.0.1",
    "ff02::1",
]
# NAT64 well-known prefix is public only when the embedded IPv4 is public (documented decision).
PUBLIC = ["8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"]
METADATA_FORMS = ["::a9fe:a9fe", "64:ff9b::a9fe:a9fe", "64:ff9b:1::a9fe:a9fe", "2002:a9fe:a9fe::1"]


@pytest.mark.parametrize("value", NOT_PUBLIC)
def test_not_public(value: str) -> None:
    assert not is_public_address(ipaddress.ip_address(value))


@pytest.mark.parametrize("value", PUBLIC)
def test_public(value: str) -> None:
    assert is_public_address(ipaddress.ip_address(value))


def test_embedded_ipv4_extracts_carried_address() -> None:
    for value in METADATA_FORMS:
        assert embedded_ipv4(ipaddress.ip_address(value)) == ipaddress.ip_address("169.254.169.254")
    assert embedded_ipv4(ipaddress.ip_address("::1")) is None
    assert embedded_ipv4(ipaddress.ip_address("8.8.8.8")) is None


@pytest.mark.asyncio
async def test_rest_resolve_public_refuses_nat64_metadata() -> None:
    from modules.connectors.providers import rest

    info = [(10, 1, 6, "", ("64:ff9b::a9fe:a9fe", 443, 0, 0))]
    with patch.object(rest, "run_dns_lookup", AsyncMock(return_value=info)):
        with pytest.raises(rest.UnsafeDestination):
            await rest.resolve_public("x.example", 443)


@pytest.mark.asyncio
async def test_rest_fetch_bounded_refuses_nat64_metadata() -> None:
    from modules.connectors.providers import rest

    resolver = AsyncMock(return_value=["64:ff9b::a9fe:a9fe"])
    with pytest.raises(rest.UnsafeDestination):
        await rest.fetch_bounded("https://x.example/", resolve=resolver, before_send=AsyncMock())


@pytest.mark.asyncio
async def test_collection_validate_public_url_refuses_nat64_metadata() -> None:
    from modules.connectors.service import collection_requests as cr

    info = [(10, 1, 6, "", ("64:ff9b::a9fe:a9fe", 443, 0, 0))]
    with patch.object(cr, "run_dns_lookup", AsyncMock(return_value=info)):
        with pytest.raises(ValueError):
            await cr.validate_public_url("https://x.example/")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", METADATA_FORMS)
async def test_local_services_refuses_encoded_metadata(value: str) -> None:
    from modules.connectors.providers import local_services as ls

    with pytest.raises(ls.LocalServiceError):
        await ls._resolve_local_addresses(value, 80)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", METADATA_FORMS)
async def test_mcp_transport_refuses_encoded_metadata(value: str) -> None:
    from modules.tools import mcp_transport as mt

    class _Budget:
        def remaining_seconds(self) -> float:
            return 5.0

    transport = object.__new__(mt.McpPinnedHttpTransport)
    transport._budget = _Budget()  # type: ignore[attr-defined]
    transport._approved_destination_cidrs = {}  # type: ignore[attr-defined]
    with pytest.raises(mt.McpTransportError):
        await transport._resolve_destination(("https", value, 443))
