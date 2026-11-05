"""Pure Vietnamese stock/index adapter over the VNDirect finfo public endpoints (unofficial, keyless).

Chosen 2026-11-04 over TCBS because TCBS answers only behind a Cloudflare challenge while VNDirect answers plain
GETs. Owner-approved unofficial endpoint: it carries no contract and may change or disappear without notice.

Stocks come from ``stock_prices`` and indices from ``vnmarket_prices``; one request per non-empty group (at most
two per run). Prices are kept exactly as the provider states them (stocks in thousands of VND, indices in points);
nothing is converted or relabeled. There is no fallback to another provider.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

from modules.connectors.provider_options import DEFAULT_VN_SYMBOLS, MAX_VN_SYMBOLS, VN_INDEX_CODES
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    decimal_text,
    finite_decimal,
    parse_iso_date,
    positive_decimal,
)
from modules.ingestion.public import IngestionRecord

VN_STOCK_HOST = "api-finfo.vndirect.com.vn"
STOCK_URL = f"https://{VN_STOCK_HOST}/v4/stock_prices"
INDEX_URL = f"https://{VN_STOCK_HOST}/v4/vnmarket_prices"
# Newest-first rows requested per code: enough to cross a long holiday (Tet) and still hold each code's latest session.
ROWS_PER_SYMBOL = 12
_VN_TZ = timezone(timedelta(hours=7))  # Vietnam has no DST, so a fixed offset is exact
_STOCK_UNIT = "thousand VND"


def split_symbols(symbols: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split owner codes into (stocks, indices) by the known index-code list, keeping the given order."""
    indices = tuple(s for s in symbols if s in VN_INDEX_CODES)
    return tuple(s for s in symbols if s not in VN_INDEX_CODES), indices


def vn_stock_requests(symbols: tuple[str, ...]) -> list[tuple[tuple[str, ...], ProviderRequest]]:
    """Build the (symbols, GET) pairs for a run: one for the stock group and one for the index group, if non-empty.

    Symbols must already match ``^[A-Z0-9]{2,10}$`` (the option type enforces it); they are re-checked here
    because they are interpolated into the query string. Rows are sorted newest first with a bounded page size
    (no date filter, so the request is deterministic) and the mapper takes each code's latest session.
    """
    if not 1 <= len(symbols) <= MAX_VN_SYMBOLS or any(
        not (2 <= len(s) <= 10 and s.isascii() and s.isalnum() and s == s.upper()) for s in symbols
    ):
        raise ProviderPayloadError("vn_stock_scope_mismatch")
    stocks, indices = split_symbols(symbols)
    out: list[tuple[tuple[str, ...], ProviderRequest]] = []
    for base, group in ((STOCK_URL, stocks), (INDEX_URL, indices)):
        if group:
            out.append((group, ProviderRequest(
                "GET", f"{base}?q=code:{','.join(group)}&sort=date:desc&size={len(group) * ROWS_PER_SYMBOL}")))
    return out


def vn_stock_request() -> ProviderRequest:
    """Return the default-scope stock request (adapter-table shape); the runner builds the real ones per scope."""
    return vn_stock_requests(DEFAULT_VN_SYMBOLS)[0][1]


def _observed_at(row: dict[str, object], session_day: date, collected_at: datetime) -> datetime:
    """Provider session date plus its time-of-day (Vietnam time); falls back to midnight and never exceeds collection."""
    raw_time = row.get("time")
    moment = datetime.combine(session_day, time.min, tzinfo=_VN_TZ)
    if isinstance(raw_time, str) and len(raw_time) == 8:
        try:
            moment = datetime.combine(session_day, time.fromisoformat(raw_time), tzinfo=_VN_TZ)
        except ValueError:
            pass  # keep midnight: the date alone is the contract
    return min(moment.astimezone(UTC), collected_at)


def map_vn_stock(
    payload: object, collected_at: datetime, *, symbols: tuple[str, ...] = DEFAULT_VN_SYMBOLS,
) -> list[IngestionRecord]:
    """Map the latest session close of every requested code; one record per code, identity ``vn_stock:{CODE}:close``.

    Rows for codes outside ``symbols`` are ignored. A requested code with no row is skipped (a mistyped or halted
    ticker must not block the other series), but a body with none of them is a scope mismatch. OHLC and volume ride along as bounded provider
    fields. Index rows carry no currency; stock prices are in thousands of VND, exactly as the provider sends.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderPayloadError("vn_stock_response_invalid")
    wanted = set(symbols)
    latest: dict[str, tuple[date, dict[str, object]]] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ProviderPayloadError("vn_stock_response_invalid")
        code = row.get("code")
        if not isinstance(code, str) or code not in wanted:
            continue
        day = parse_iso_date(row.get("date"))
        if code not in latest or day > latest[code][0]:
            latest[code] = (day, row)
    if not latest:
        raise ProviderPayloadError("vn_stock_scope_mismatch")
    records: list[IngestionRecord] = []
    for code in (s for s in symbols if s in latest):
        day, row = latest[code]
        try:
            close = positive_decimal(row.get("close"))
            ohlc: dict[str, Decimal | None] = {
                key: (None if row.get(key) is None else positive_decimal(row.get(key)))
                for key in ("open", "high", "low")
            }
            volume_raw = row.get("nmVolume", row.get("accumulatedVol"))
            volume = None if volume_raw is None else finite_decimal(volume_raw)
        except ValueError as exc:
            raise ProviderPayloadError("vn_stock_price_invalid") from exc
        is_index = code in VN_INDEX_CODES
        unit = "points" if is_index else _STOCK_UNIT
        text = decimal_text(close)
        iso_day = day.isoformat()
        records.append(build_record(
            provider="vn_stock", identity=f"vn_stock:{code}:close",
            content=f"{code} close on {iso_day}: {text} {unit} (VNDirect, unofficial public endpoint)",
            title=f"{code} close", observed_at=_observed_at(row, day, collected_at), collected_at=collected_at,
            version=content_version("vn_stock", code, iso_day, text, volume), metric="close", value=close,
            unit=unit, currency=None, symbol=code, quality="provider_reported", timestamp_basis="provider_modified",
            modified_at=_observed_at(row, day, collected_at),
            provider_fields={
                "symbol": code, "date": iso_day, "kind": "index" if is_index else "stock",
                **{k: (None if v is None else float(v)) for k, v in ohlc.items()},
                "close": float(close), "volume": None if volume is None else float(volume),
            },
        ))
    return records


VN_STOCK_ADAPTERS: dict[str, PureAdapter] = {"vn_stock": PureAdapter(vn_stock_request, map_vn_stock)}
