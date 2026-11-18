"""Pure rules for Topic watch: URL canonicalisation, site filters, title keys and the token budget.

No I/O and no heavy imports, so the collector, the automations owner (through
``modules.connectors.public``) and the tests share one definition of the budget formula.
"""

import math
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLAN_TOKENS = 600  # one planning call
PAGE_TOKENS = 1500  # about 4000 characters of page text plus a short JSON answer
MAX_URL_CHARS = 2048
_TRACKING_KEYS = frozenset({"fbclid", "gclid"})


def estimate_tokens(max_items: int) -> int:
    """Return the per-run token estimate: planning plus at most ``2 * max_items`` page reviews."""
    return PLAN_TOKENS + max_items * 2 * PAGE_TOKENS


def token_cap(estimate: int) -> int:
    """Return the hard per-run cap, 1.5 times the estimate rounded up."""
    return math.ceil(1.5 * estimate)


def canonical_url(raw: str) -> str | None:
    """Normalise a public http(s) URL for de-duplication, or return None when it must be dropped.

    Lowercases scheme and host, drops credentials, the fragment and a default port, removes
    ``utm_*``/``fbclid``/``gclid`` query keys, sorts the remaining query, and turns an empty path
    into ``/``. Anything that is not http(s), has no host, is malformed or exceeds 2048
    characters (before or after normalising) yields None. Pure and total.
    """
    if len(raw) > MAX_URL_CHARS:
        return None
    try:
        parts = urlsplit(raw.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host.lower()
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc += f":{port}"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
        )
    )
    url = urlunsplit((scheme, netloc, parts.path or "/", query, ""))
    return url if len(url) <= MAX_URL_CHARS else None


def _matches(host: str, site: str) -> bool:
    """True when ``host`` is ``site`` or a subdomain of it (dot-boundary suffix match)."""
    return host == site or host.endswith("." + site)


def site_allowed(host: str, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """Apply the owner's site filters: exclusion always wins, an empty include list allows all."""
    host = host.lower()
    if any(_matches(host, site) for site in exclude):
        return False
    return not include or any(_matches(host, site) for site in include)


def title_key(title: str) -> str:
    """Return a near-duplicate key: casefolded alphanumerics of the title, first 80 characters."""
    return "".join(ch for ch in title.casefold() if ch.isalnum())[:80]
