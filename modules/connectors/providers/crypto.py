"""Pure crypto/sentiment adapters: Binance BTCUSDT, Alternative.me Fear and Greed, optional CoinPaprika/CoinGecko.

Units are never relabeled (USDT stays USDT, currency=None); a latest quote with no provider clock is
observed at collection time with ``timestamp_basis="collection"``. Optional providers are separate
adapters chosen explicitly by the owner; there is no fallback between them.
"""

import re
from datetime import datetime
from decimal import Decimal

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    decimal_text,
    finite_decimal,
    positive_decimal,
    provider_instant,
)
from modules.ingestion.schemas import IngestionRecord

BINANCE_URL = "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=2"
COINPAPRIKA_URL = "https://api.coinpaprika.com/v1/tickers/btc-bitcoin"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
COINGECKO_KEY_HEADER = "x-cg-demo-api-key"

DEFAULT_BINANCE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("BTCUSDT", "BTC", "USDT"),
)
DEFAULT_COINGECKO_ASSETS: tuple[tuple[str, str, str], ...] = (
    ("bitcoin", "BTC", "USD"),
)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_COIN_ID_RE = re.compile(r"^[a-z0-9-]{2,64}$")


def binance_request(
    symbol: str = "BTCUSDT",
    *,
    pairs: tuple[tuple[str, str, str], ...] = DEFAULT_BINANCE_PAIRS,
) -> ProviderRequest:
    """Build a Binance spot ticker GET request; defaults to the approved BTCUSDT endpoint."""
    target_symbol = symbol
    if pairs != DEFAULT_BINANCE_PAIRS and pairs:
        target_symbol = pairs[0][0]
    if not _SYMBOL_RE.fullmatch(target_symbol):
        raise ProviderPayloadError("binance_scope_mismatch")
    if target_symbol == "BTCUSDT" and pairs == DEFAULT_BINANCE_PAIRS:
        return ProviderRequest("GET", BINANCE_URL)
    return ProviderRequest(
        "GET",
        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={target_symbol}",
    )


def fear_greed_request() -> ProviderRequest:
    """Return the fixed GET request for the Alternative.me Fear and Greed Index endpoint."""
    return ProviderRequest("GET", FEAR_GREED_URL)


def coinpaprika_request() -> ProviderRequest:
    """Return the fixed GET request for the CoinPaprika BTC/USD ticker endpoint."""
    return ProviderRequest("GET", COINPAPRIKA_URL)


def coingecko_request(
    api_key: str,
    *,
    assets: tuple[tuple[str, str, str], ...] = DEFAULT_COINGECKO_ASSETS,
) -> ProviderRequest:
    """Demo key travels only in the header (never in the URL); it is excluded from repr."""
    if not api_key or len(api_key) > 256 or not api_key.isprintable() or api_key != api_key.strip():
        raise ProviderPayloadError("coingecko_key_invalid", "credential")
    if not assets:
        raise ProviderPayloadError("coingecko_scope_mismatch")
    if assets == DEFAULT_COINGECKO_ASSETS:
        return ProviderRequest("GET", COINGECKO_URL, {COINGECKO_KEY_HEADER: api_key})
    coin_ids: list[str] = []
    quotes: list[str] = []
    for coin_id, symbol, quote in assets:
        if not _COIN_ID_RE.fullmatch(coin_id) or not _SYMBOL_RE.fullmatch(symbol) or not _SYMBOL_RE.fullmatch(quote):
            raise ProviderPayloadError("coingecko_scope_mismatch")
        if coin_id not in coin_ids:
            coin_ids.append(coin_id)
        lower_quote = quote.lower()
        if lower_quote not in quotes:
            quotes.append(lower_quote)
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={','.join(coin_ids)}&vs_currencies={','.join(quotes)}"
    )
    return ProviderRequest("GET", url, {COINGECKO_KEY_HEADER: api_key})


def map_binance(
    payload: object,
    collected_at: datetime,
    *,
    pairs: tuple[tuple[str, str, str], ...] = DEFAULT_BINANCE_PAIRS,
) -> list[IngestionRecord]:
    """Map the latest Binance spot price(s); Binance supplies no event clock so collection time is the basis; the identity is fixed so each collection is a new revision of one series."""
    if not pairs:
        raise ProviderPayloadError("binance_scope_mismatch")
    allowed_pairs: dict[str, tuple[str, str]] = {
        pair_symbol: (base_asset, quote_asset)
        for pair_symbol, base_asset, quote_asset in pairs
    }
    items: list[object]
    if isinstance(payload, dict):
        if "code" in payload or "msg" in payload:
            limited = payload.get("code") in {-1003, 429, 418}
            raise ProviderPayloadError(
                "binance_rate_limited" if limited else "binance_error_payload",
                "rate_limited" if limited else "invalid",
            )
        items = [payload]
    elif isinstance(payload, list) and len(pairs) > 1 and payload:
        items = payload
    else:
        raise ProviderPayloadError("binance_response_invalid")

    seen: set[str] = set()
    records: list[IngestionRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise ProviderPayloadError("binance_response_invalid")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or symbol not in allowed_pairs or symbol in seen:
            raise ProviderPayloadError("binance_scope_mismatch")
        seen.add(symbol)
        base_asset, quote_asset = allowed_pairs[symbol]
        try:
            price = positive_decimal(item.get("price"))
        except ValueError as exc:
            raise ProviderPayloadError("binance_price_invalid") from exc
        text = decimal_text(price)
        records.append(build_record(
            provider="binance", identity=f"binance:{symbol}:price",
            content=f"Binance {symbol} last price: {text} {quote_asset} (collected quote, no exchange timestamp)",
            title=f"{symbol} price", observed_at=collected_at, collected_at=collected_at,
            version=content_version("binance", symbol, text), metric="spot_price", value=price,
            unit=quote_asset, currency=None, symbol=symbol, quality="provider_reported",
            provider_fields={"symbol": symbol, "base": base_asset, "quote": quote_asset},
        ))
    if len(records) != len(allowed_pairs):
        raise ProviderPayloadError("binance_scope_mismatch")
    return records


def map_fear_greed(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map up to two sentiment index points (0-100); provider Unix seconds are the observation clock."""
    if not isinstance(payload, dict):
        raise ProviderPayloadError("fear_greed_response_invalid")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("error") is not None:
        raise ProviderPayloadError("fear_greed_error_payload")
    data = payload.get("data")
    if not isinstance(data, list) or not 1 <= len(data) <= 2:
        raise ProviderPayloadError("fear_greed_data_invalid")
    seen: set[int] = set()
    records = []
    for item in data:
        if not isinstance(item, dict):
            raise ProviderPayloadError("fear_greed_data_invalid")
        observed_at = provider_instant(item.get("timestamp"), collected_at, unit="s")
        stamp = int(observed_at.timestamp())
        label = item.get("value_classification")
        if stamp in seen or not isinstance(label, str) or not re.fullmatch(r"[A-Za-z ]{1,40}", label):
            raise ProviderPayloadError("fear_greed_data_invalid")
        seen.add(stamp)
        try:
            value = finite_decimal(item.get("value"))
        except ValueError as exc:
            raise ProviderPayloadError("fear_greed_value_invalid") from exc
        if value != value.to_integral_value() or not 0 <= value <= 100:
            raise ProviderPayloadError("fear_greed_value_invalid")
        text = decimal_text(value)
        records.append(build_record(
            provider="alternative_me", identity=f"alternative_me:fng:{stamp}",
            content=f"Crypto Fear and Greed Index {observed_at.date().isoformat()}: {text}/100 ({label})",
            title="Fear and Greed Index", observed_at=observed_at, collected_at=collected_at,
            version=content_version("alternative_me", stamp, text, label), metric="fear_greed_index",
            value=value, unit="index (0-100)", quality="provider_reported", timestamp_basis="provider_published",
            provider_fields={"timestamp": stamp, "value_classification": label},
        ))
    return records


def _error_kind(text: str) -> str:
    """Classify a provider error string into rate_limited, entitlement, or invalid."""
    lowered = text.lower()
    if "too many" in lowered or "limit" in lowered or "throttl" in lowered:
        return "rate_limited"
    if "payment" in lowered or "plan" in lowered or "upgrade" in lowered:
        return "entitlement"
    return "invalid"


def map_coinpaprika(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map the BTC USD ticker (optional personal-use provider); an empty-string price is explicit missing."""
    if not isinstance(payload, dict):
        raise ProviderPayloadError("coinpaprika_response_invalid")
    if "error" in payload:
        message = payload["error"]
        kind = _error_kind(message if isinstance(message, str) else "")
        raise ProviderPayloadError(f"coinpaprika_{kind}_payload", kind)  # type: ignore[arg-type]
    if payload.get("id") != "btc-bitcoin" or payload.get("symbol") != "BTC":
        raise ProviderPayloadError("coinpaprika_scope_mismatch")
    updated = provider_instant(payload.get("last_updated"), collected_at, unit="iso")
    quotes = payload.get("quotes")
    usd = quotes.get("USD") if isinstance(quotes, dict) else None
    if not isinstance(usd, dict) or "price" not in usd:
        raise ProviderPayloadError("coinpaprika_quote_missing")
    raw = usd["price"]
    price: Decimal | None
    try:
        price = None if raw == "" else positive_decimal(raw)
    except ValueError as exc:
        raise ProviderPayloadError("coinpaprika_price_invalid") from exc
    text = "null" if price is None else decimal_text(price)
    stamp = updated.isoformat()
    return [build_record(
        provider="coinpaprika", identity=f"coinpaprika:btc-bitcoin:USD:{stamp}",
        content=f"CoinPaprika BTC/USD at {stamp}: {'not reported' if price is None else text + ' USD'}",
        title="BTC price (CoinPaprika)", observed_at=updated, collected_at=collected_at,
        version=content_version("coinpaprika", "btc-bitcoin", "USD", stamp, text), metric="spot_price",
        value=price, unit="USD", currency="USD", symbol="BTC", quality="provider_reported",
        timestamp_basis="provider_modified", modified_at=updated,
        provider_fields={"id": "btc-bitcoin", "symbol": "BTC", "last_updated": stamp, "quote": "USD"},
    )]


_COINGECKO_CREDENTIAL_CODES = {401, 10002, 10010, 10011}


def map_coingecko(
    payload: object,
    collected_at: datetime,
    *,
    assets: tuple[tuple[str, str, str], ...] = DEFAULT_COINGECKO_ASSETS,
) -> list[IngestionRecord]:
    """Map configured CoinGecko simple-price asset rows; status/error objects fail even on HTTP 200."""
    if not assets:
        raise ProviderPayloadError("coingecko_scope_mismatch")
    if not isinstance(payload, dict):
        raise ProviderPayloadError("coingecko_response_invalid")
    if "status" in payload or "error" in payload or "error_code" in payload:
        status = payload.get("status")
        code = (status.get("error_code") if isinstance(status, dict) else payload.get("error_code"))
        if code == 429:
            raise ProviderPayloadError("coingecko_rate_limited", "rate_limited")
        if code in _COINGECKO_CREDENTIAL_CODES:
            raise ProviderPayloadError("coingecko_credential_rejected", "credential")
        if code == 10005:
            raise ProviderPayloadError("coingecko_entitlement_required", "entitlement")
        raise ProviderPayloadError("coingecko_error_payload")
    records: list[IngestionRecord] = []
    for coin_id, symbol, quote_ccy in assets:
        quote_key = quote_ccy.lower()
        coin = payload.get(coin_id)
        if not isinstance(coin, dict) or quote_key not in coin:
            raise ProviderPayloadError("coingecko_scope_mismatch")
        try:
            price = None if coin[quote_key] is None else positive_decimal(coin[quote_key])
        except ValueError as exc:
            raise ProviderPayloadError("coingecko_price_invalid") from exc
        text = "null" if price is None else decimal_text(price)
        fields: dict[str, str | float | int | None] = {"coin_id": coin_id, "quote": quote_ccy}
        if "last_updated_at" in coin:  # only when the owner-approved include_last_updated_at is in use
            observed_at = provider_instant(coin["last_updated_at"], collected_at, unit="s")
            stamp = int(observed_at.timestamp())
            fields["last_updated_at"] = stamp
            identity, basis, modified = f"coingecko:{coin_id}:{quote_ccy}:{stamp}", "provider_modified", observed_at
        else:
            observed_at, identity, basis, modified = collected_at, f"coingecko:{coin_id}:{quote_ccy}", "collection", None
        records.append(build_record(
            provider="coingecko", identity=identity,
            content=f"CoinGecko {symbol}/{quote_ccy}: {'not reported' if price is None else text + ' ' + quote_ccy}",
            title=f"{symbol} price (CoinGecko)", observed_at=observed_at, collected_at=collected_at,
            version=content_version("coingecko", coin_id, quote_ccy, fields.get("last_updated_at"), text),
            metric="spot_price", value=price, unit=quote_ccy, currency=quote_ccy, symbol=symbol,
            quality="provider_reported", timestamp_basis=basis, modified_at=modified, provider_fields=fields,  # type: ignore[arg-type]
        ))
    return records


CRYPTO_ADAPTERS: dict[str, PureAdapter] = {
    "binance": PureAdapter(binance_request, map_binance),
    "alternative_me": PureAdapter(fear_greed_request, map_fear_greed),
    "coinpaprika": PureAdapter(coinpaprika_request, map_coinpaprika),
    "coingecko": PureAdapter(coingecko_request, map_coingecko),
}
