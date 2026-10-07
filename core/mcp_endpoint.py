"""Canonical parsing for MCP HTTP endpoint URLs and deployment origin keys."""

import re
from ipaddress import ip_address
from urllib.parse import urlsplit


def normalize_mcp_url(value: str, *, origin_only: bool = False) -> tuple[str, str, int, str]:
    """Parse an MCP HTTP URL into a canonical scheme, host, effective port, and path.

    Hostnames are IDNA ASCII and lowercase; IP literals use compressed form. IPv6
    hosts are unbracketed in the result. Endpoint paths are retained, while
    deployment keys set ``origin_only`` to reject any path beyond the root.
    User information, query/fragment data, scoped IP literals, unsupported
    schemes, malformed authorities, and explicitly supplied port zero are rejected.
    """
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        raise ValueError("MCP endpoint URL is invalid")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP endpoint URL must use HTTP or HTTPS")
        if parsed.netloc.endswith(":"):
            raise ValueError("MCP endpoint URL has an empty port")
        if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or "?" in value or "#" in value):
            raise ValueError("MCP endpoint URL must not contain credentials, query, or fragment")
        if "%" in parsed.hostname:
            raise ValueError("Scoped IP destinations are not supported")
        port = parsed.port
    except ValueError:  # noqa: TRY203  # explicit re-raise kept to preserve the exact error contract
        raise
    if port == 0:
        raise ValueError("MCP endpoint port must be between 1 and 65535")
    try:
        address = ip_address(parsed.hostname)
        host = address.compressed.lower()
    except ValueError:
        try:
            host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise ValueError("MCP endpoint hostname is invalid") from exc
        labels = host.split(".")
        if (len(host) > 253 or any(
                len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels
        )):
            raise ValueError("MCP endpoint hostname is invalid")
    if not host:
        raise ValueError("MCP endpoint hostname is invalid")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if origin_only and path != "/":
        raise ValueError("MCP deployment policy keys must contain only an origin")
    return scheme, host, effective_port, path
