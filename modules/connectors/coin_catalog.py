"""Bounded, shared CoinGecko coin catalog used only by the owner source editor."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import TypedDict

from modules.connectors.providers import rest

COINS_URL = "https://api.coingecko.com/api/v3/coins/list?include_platform=false"
MAX_CATALOG_BYTES = 4 * 1024 * 1024
CATALOG_TTL_SECONDS = 3600
_COIN_ID = re.compile(r"^[a-z0-9-]{2,64}$")
_lock = asyncio.Lock()
_catalog: tuple["Coin", ...] = ()
_expires_at = 0.0


class Coin(TypedDict):
    """CoinGecko identity and display text; ``id`` is the stable selection key."""

    id: str
    symbol: str
    name: str


def parse_catalog(body: bytes) -> tuple[Coin, ...]:
    """Validate the bounded list and keep only coin IDs the source config can safely select."""
    if len(body) > MAX_CATALOG_BYTES:
        raise ValueError("CoinGecko catalog is too large")
    try:
        rows = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("CoinGecko catalog is invalid") from exc
    if not isinstance(rows, list) or len(rows) > 100_000:
        raise ValueError("CoinGecko catalog is invalid")
    coins: list[Coin] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        coin_id, symbol, name = row.get("id"), row.get("symbol"), row.get("name")
        symbol = symbol.upper() if isinstance(symbol, str) else symbol
        if (not isinstance(coin_id, str) or not _COIN_ID.fullmatch(coin_id)
                or not isinstance(symbol, str) or not 1 <= len(symbol) <= 32
                or symbol != symbol.strip() or not symbol.isprintable()
                or not isinstance(name, str) or not 1 <= len(name) <= 128
                or name != name.strip() or not name.isprintable()):
            continue  # A single unusable upstream entry must not hide every valid coin.
        if coin_id in seen:
            continue
        seen.add(coin_id)
        coins.append({"id": coin_id, "symbol": symbol, "name": name})
    if not coins:
        raise ValueError("CoinGecko catalog contains no selectable coins")
    return tuple(coins)


async def load_catalog(api_key: str, before_send: Callable[[], Awaitable[None]]) -> tuple[Coin, ...]:
    """Fetch the fixed public catalog once per process-hour; only a cache miss debits and sends."""
    global _catalog, _expires_at
    if _catalog and time.monotonic() < _expires_at:
        return _catalog
    async with _lock:
        if _catalog and time.monotonic() < _expires_at:
            return _catalog
        fetched = await rest.fetch_bounded(
            COINS_URL, headers={"x-cg-demo-api-key": api_key}, before_send=before_send,
            max_bytes=MAX_CATALOG_BYTES, timeout=15,
        )
        if fetched.body is None:
            raise ValueError("CoinGecko catalog returned no data")
        parsed = parse_catalog(fetched.body)
        _catalog, _expires_at = parsed, time.monotonic() + CATALOG_TTL_SECONDS
        return parsed


def search_catalog(coins: tuple[Coin, ...], query: str) -> list[Coin]:
    """Return at most fifty id/name/symbol matches without hiding identical ticker symbols."""
    term = query.casefold().strip()
    matches = [coin for coin in coins if term in coin["id"].casefold()
               or term in coin["symbol"].casefold() or term in coin["name"].casefold()]
    matches.sort(key=lambda coin: (
        coin["symbol"].casefold() != term, coin["id"].casefold() != term,
        not coin["name"].casefold().startswith(term), coin["name"].casefold(), coin["id"],
    ))
    return matches[:50]
