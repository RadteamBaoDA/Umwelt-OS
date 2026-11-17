"""Shared egress address policy: one definition of "publicly routable" for every outbound-fetch gate."""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Prefixes whose embedded IPv4 is attacker-choosable and which nothing legitimate needs: denied outright.
_DENY_V6 = tuple(
    ipaddress.IPv6Network(v)
    for v in (
        "2001::/32",  # Teredo (embedded client IPv4 is obfuscated; rejected rather than decoded)
        "2002::/16",  # 6to4
        "64:ff9b:1::/48",  # local-use NAT64
        "::/96",  # IPv4-compatible (also ::, ::1)
        "fec0::/10",  # deprecated site-local
        "100::/64",  # discard-only
    )
)
_DENY_V4 = tuple(ipaddress.IPv4Network(v) for v in ("100.64.0.0/10", "192.88.99.0/24"))
_NAT64_WKP = ipaddress.IPv6Network("64:ff9b::/96")  # RFC 6052 fixes the layout only for this prefix
_IPV4_COMPAT = ipaddress.IPv6Network("::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_SIXTOFOUR = ipaddress.IPv6Network("2002::/16")


def embedded_ipv4(address: IPAddress) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address an IPv6 encoding carries, or None when ``address`` is not such an encoding.

    Covers IPv4-mapped, IPv4-compatible (``::a.b.c.d``, excluding ``::`` and ``::1``), NAT64 (well-known and
    local-use, low 32 bits) and 6to4 (bits 16-48). Used so link-local/private rules apply to the real target.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _SIXTOFOUR:
        return ipaddress.IPv4Address(address.packed[2:6])
    if (
        address in _NAT64_WKP
        or address in _NAT64_LOCAL
        or (address in _IPV4_COMPAT and int(address) > 1)
    ):
        return ipaddress.IPv4Address(address.packed[12:])
    return None


def is_public_address(address: IPAddress) -> bool:
    """Return whether an address is publicly routable and safe as an outbound destination.

    ``is_global`` alone is wrong on Python 3.12 (NAT64 ``64:ff9b::a9fe:a9fe``, multicast and ``fec0::/10`` all
    report True), so every non-public property is checked explicitly. IPv4-mapped and well-known NAT64 are
    unwrapped and judged by the embedded IPv4 (NAT64 of 8.8.8.8 is public, of 169.254.169.254 is not); the other
    tunnel prefixes are denied outright. Pure function, no I/O.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if any(address in network for network in _DENY_V6):
            return False
        if address in _NAT64_WKP:
            return is_public_address(ipaddress.IPv4Address(address.packed[12:]))
    elif any(address in network for network in _DENY_V4):
        return False
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
