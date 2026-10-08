"""Pure macro/FX adapters (World Bank, Frankfurter, ECB) plus the shared parse/record policy.

No network and no database: request builders return fixed https URLs for an injected transport,
mappers turn an already-fetched body into typed ``IngestionRecord`` envelopes.
Conventions: annual/day periods live in ``provider_fields`` and ``observed_at`` is the start of the
period (UTC) with ``timestamp_basis="collection"`` because the provider names no publication instant;
values missing at the provider are ``quality="missing"`` with ``missing_reason="provider_null"``, never
zero; bodies that look like quota/error/HTML are rejected, not emitted as empty series.
"""

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET  # DOCTYPE/ENTITY rejected before parsing
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from modules.ingestion.schemas import IngestionRecord

MAX_BODY_BYTES = 2 * 1024 * 1024
_DECIMAL_RE = re.compile(r"^-?[0-9]{1,40}(\.[0-9]{1,40})?([eE][+-]?[0-9]{1,3})?$")
_CCY_RE = re.compile(r"^[A-Z]{3}$")


class ProviderPayloadError(ValueError):
    """A provider body that must not become records; ``kind`` tells the controller how to react."""

    def __init__(
        self, code: str,
        kind: Literal["invalid", "rate_limited", "credential", "entitlement", "incomplete"] = "invalid",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


@dataclass(frozen=True)
class ProviderRequest:
    """Fixed GET request for an injected transport; headers may hold a secret and are never repr'd."""
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def finite_decimal(raw: object) -> Decimal:
    """Parse a provider number without silently accepting booleans, NaN, underscores or huge exponents."""
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int, float, Decimal)):
        raise ValueError("provider_measurement_invalid")
    if isinstance(raw, str) and not _DECIMAL_RE.fullmatch(raw):
        raise ValueError("provider_measurement_invalid")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("provider_measurement_invalid") from exc
    if not value.is_finite() or abs(value.adjusted()) > 30:
        raise ValueError("provider_measurement_invalid")
    return value


def positive_decimal(raw: object) -> Decimal:
    """Finite and strictly positive (prices and rates)."""
    value = finite_decimal(raw)
    if value <= 0:
        raise ValueError("provider_measurement_invalid")
    return value


def decimal_text(value: Decimal) -> str:
    """Canonical plain-notation text (no exponent, no trailing fractional zeros) kept beside the float."""
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def decode_json(body: bytes) -> object:
    """Decode bounded JSON with Decimal numbers so lexical precision survives until the float boundary."""
    if len(body) > MAX_BODY_BYTES:
        raise ProviderPayloadError("provider_response_too_large")

    def _reject(_: str) -> object:
        raise ValueError("non-finite")

    try:
        return json.loads(body, parse_float=Decimal, parse_constant=_reject)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:  # HTML/plain-text 200s land here
        raise ProviderPayloadError("provider_response_not_json") from exc


def content_version(*parts: object) -> str:
    """Deterministic 32-hex version over selected content (never collected_at or ordering)."""
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def parse_iso_date(raw: object) -> date:
    """Strict YYYY-MM-DD provider day."""
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise ProviderPayloadError("provider_date_invalid")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ProviderPayloadError("provider_date_invalid") from exc


def provider_instant(raw: object, collected_at: datetime, *, unit: Literal["s", "ms", "iso"]) -> datetime:
    """Strict aware provider instant (Unix s/ms ints or tz-aware ISO text); future beyond 10 min skew is invalid."""
    try:
        if unit == "iso":
            if not isinstance(raw, str) or len(raw) > 40:
                raise ValueError
            moment = datetime.fromisoformat(raw)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError
        else:
            if type(raw) is not int and not (isinstance(raw, str) and raw.isdigit() and len(raw) <= 16):
                raise ValueError
            moment = datetime.fromtimestamp(int(raw) / (1000 if unit == "ms" else 1), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise ProviderPayloadError("provider_time_invalid") from exc
    moment = moment.astimezone(UTC)
    if moment.year < 2000 or (moment - collected_at).total_seconds() > 600:
        raise ProviderPayloadError("provider_time_invalid")
    return moment


def day_start(day: date) -> datetime:
    """Documented period convention: a day (or year) is observed at its UTC start."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def build_record(
    *, provider: str, identity: str, content: str, title: str, observed_at: datetime,
    collected_at: datetime, version: str, metric: str, value: Decimal | None, unit: str,
    quality: Literal["provider_reported", "forecast", "reference"],
    provider_fields: dict[str, str | float | int | None], decimal_key: str | None = "decimal_value",
    currency: str | None = None, symbol: str | None = None, region: str | None = None,
    latitude: float | None = None, longitude: float | None = None,
    timestamp_basis: Literal["provider_modified", "provider_published", "collection"] = "collection",
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"] = "returned_snapshot",
    modified_at: datetime | None = None,
) -> IngestionRecord:
    """Build the trusted typed envelope; ``value`` None becomes explicit provider_null missing."""
    from modules.knowledge.documents.schemas import ProviderRecordMetadata, WorldDataMeasurement

    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    as_float = None
    fields = dict(provider_fields)
    if value is not None:
        as_float = float(value)
        if not math.isfinite(as_float):
            raise ProviderPayloadError("provider_measurement_invalid")
        if decimal_key:
            fields[decimal_key] = decimal_text(value)
    measurement = WorldDataMeasurement(
        provider=provider, metric=metric, value=as_float, unit=unit, currency=currency,  # type: ignore[arg-type]
        timezone=None, symbol=symbol, region=region, latitude=latitude, longitude=longitude,
        published_at=None, quality="missing" if value is None else quality,
        missing_reason="provider_null" if value is None else None, provider_fields=fields,
    )
    envelope = ProviderRecordMetadata(
        provider=provider, identity=identity, provider_version=version,  # type: ignore[arg-type]
        timestamp_basis=timestamp_basis, coverage=coverage, content_truncated=False,
        provider_modified_at=modified_at, world_data=measurement,
    )
    return IngestionRecord(
        provider_id=identity, content=content, observed_at=observed_at, collected_at=collected_at,
        version=version, metadata={"title": title, "provider_record": envelope.model_dump(mode="json")},
    )


# --------------------------------------------------------------------------- World Bank

WORLD_BANK_URL = "https://api.worldbank.org/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3"
_WB_MAX_ROWS = 50


def world_bank_request() -> ProviderRequest:
    """Fixed approved endpoint; exact continuation paging is a controller decision (see report)."""
    return ProviderRequest("GET", WORLD_BANK_URL)


def map_world_bank(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map annual GDP (current US$) rows; null stays missing and never replaces earlier valid years."""
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict) and "message" in payload[0]:
        raise ProviderPayloadError("world_bank_error_payload")
    if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict) or not isinstance(payload[1], list):
        raise ProviderPayloadError("world_bank_response_invalid")
    meta, rows = payload
    raw_paging = [meta.get(k) for k in ("page", "pages", "per_page", "total")]
    if any(type(v) is not int and not (isinstance(v, str) and v.isdigit()) for v in raw_paging):
        raise ProviderPayloadError("world_bank_paging_invalid")
    page, pages, per_page, total = (int(v) for v in raw_paging)
    if page != 1 or pages < 1 or per_page < 1 or len(rows) > _WB_MAX_ROWS or len(rows) != min(per_page, total):
        raise ProviderPayloadError("world_bank_incomplete_page", "incomplete")
    if not rows:
        raise ProviderPayloadError("world_bank_no_rows", "incomplete")
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if pages > 1 else "returned_snapshot"
    seen: set[str] = set()
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderPayloadError("world_bank_row_invalid")
        indicator, country = row.get("indicator"), row.get("country")
        if (
            not isinstance(indicator, dict) or indicator.get("id") != "NY.GDP.MKTP.CD"
            or not isinstance(country, dict) or country.get("id") != "VN" or row.get("countryiso3code") != "VNM"
        ):
            raise ProviderPayloadError("world_bank_scope_mismatch")
        period = row.get("date")
        if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}", period) or period in seen:
            raise ProviderPayloadError("world_bank_period_invalid")
        seen.add(period)
        observed_at = datetime(int(period), 1, 1, tzinfo=UTC)
        if observed_at > collected_at:
            raise ProviderPayloadError("world_bank_period_in_future")
        raw = row.get("value")
        try:
            value = None if raw is None else finite_decimal(raw)
        except ValueError as exc:
            raise ProviderPayloadError("world_bank_value_invalid") from exc
        text = "null" if value is None else decimal_text(value)
        records.append(build_record(
            provider="world_bank", identity=f"world_bank:VN:NY.GDP.MKTP.CD:{period}",
            content=f"Viet Nam GDP (current US$) {period}: {'not reported' if value is None else text + ' USD'}",
            title=f"Viet Nam GDP {period}", observed_at=observed_at, collected_at=collected_at,
            version=content_version("world_bank", "VN", "NY.GDP.MKTP.CD", period, text),
            metric="gdp_current_usd", value=value, unit="current US$", currency="USD", region="Viet Nam",
            quality="provider_reported", coverage=coverage,
            provider_fields={"country": "VN", "indicator": "NY.GDP.MKTP.CD", "period": period, "unit": "current US$"},
        ))
    return records


# --------------------------------------------------------------------------- Frankfurter

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/usd/vnd"


def frankfurter_request() -> ProviderRequest:
    return ProviderRequest("GET", FRANKFURTER_URL)


def map_frankfurter(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map the USD/VND blended reference rate; the date is the rate period, not a release instant."""
    if not isinstance(payload, dict):
        raise ProviderPayloadError("frankfurter_response_invalid")
    if "message" in payload or "error" in payload:
        raise ProviderPayloadError("frankfurter_error_payload")
    day = parse_iso_date(payload.get("date"))
    if payload.get("base") != "USD" or payload.get("quote") != "VND":
        raise ProviderPayloadError("frankfurter_scope_mismatch")
    try:
        rate = positive_decimal(payload.get("rate"))
    except ValueError as exc:
        raise ProviderPayloadError("frankfurter_rate_invalid") from exc
    providers = payload.get("providers")
    providers_text = None  # absent or unsupported shape: never invented
    if isinstance(providers, list) and all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", p) for p in providers):
        providers_text = ",".join(providers)[:200] or None
    iso = day.isoformat()
    fields: dict[str, str | float | int | None] = {"date": iso, "base": "USD", "quote": "VND"}
    if providers_text:
        fields["providers"] = providers_text
    text = decimal_text(rate)
    return [build_record(
        provider="frankfurter", identity=f"frankfurter:USD:VND:{iso}:blended",
        content=f"USD/VND blended reference rate {iso}: {text} VND per USD",
        title="USD/VND reference rate", observed_at=day_start(day), collected_at=collected_at,
        version=content_version("frankfurter", "USD", "VND", iso, text), metric="fx_reference_rate",
        value=rate, unit="VND per USD", currency="VND", symbol="USD/VND", quality="reference",
        provider_fields=fields,
    )]


# --------------------------------------------------------------------------- ECB

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_MAX_BYTES = 256 * 1024
_ECB_MAX_RATES = 64


def ecb_request() -> ProviderRequest:
    return ProviderRequest("GET", ECB_URL)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def map_ecb(xml: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Map EUR reference rates; DOCTYPE/entities are rejected and no VND is ever synthesized."""
    if not isinstance(xml, (bytes, bytearray)) or len(xml) > _ECB_MAX_BYTES:
        raise ProviderPayloadError("ecb_response_invalid")
    try:  # non-UTF-8 encodings (e.g. UTF-16) could hide a DOCTYPE from the byte scan below
        text = bytes(xml).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderPayloadError("ecb_xml_malformed") from exc
    declared = re.match(r"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", text)
    if declared and declared.group(1).lower() != "utf-8":
        raise ProviderPayloadError("ecb_xml_malformed")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ProviderPayloadError("ecb_forbidden_xml_construct")
    try:
        root = ET.fromstring(bytes(xml))  # hostile constructs rejected above
    except ET.ParseError as exc:
        raise ProviderPayloadError("ecb_xml_malformed") from exc
    days = [el for el in root.iter() if _local(el.tag) == "Cube" and "time" in el.attrib]
    if len(days) != 1:
        raise ProviderPayloadError("ecb_date_missing")
    day = parse_iso_date(days[0].attrib["time"])
    cubes = [el for el in days[0] if _local(el.tag) == "Cube"]
    if not cubes or len(cubes) > _ECB_MAX_RATES:
        raise ProviderPayloadError("ecb_rates_invalid")
    iso = day.isoformat()
    seen: set[str] = set()
    records = []
    for cube in cubes:
        quote = cube.attrib.get("currency", "")
        if not _CCY_RE.fullmatch(quote) or quote == "EUR" or quote in seen:
            raise ProviderPayloadError("ecb_currency_invalid")
        seen.add(quote)
        try:
            rate = positive_decimal(cube.attrib.get("rate"))
        except ValueError as exc:
            raise ProviderPayloadError("ecb_rate_invalid") from exc
        text = decimal_text(rate)
        records.append(build_record(
            provider="ecb", identity=f"ecb:EUR:{quote}:{iso}",
            content=f"ECB euro reference rate {iso}: 1 EUR = {text} {quote}",
            title=f"EUR/{quote} reference rate", observed_at=day_start(day), collected_at=collected_at,
            version=content_version("ecb", "EUR", quote, iso, text), metric="fx_reference_rate",
            value=rate, unit=f"{quote} per EUR", currency=quote, symbol=f"EUR/{quote}", quality="reference",
            provider_fields={"date": iso, "base": "EUR", "quote": quote},
        ))
    return records


# --------------------------------------------------------------------------- adapter registry

@dataclass(frozen=True)
class PureAdapter:
    """Request builder plus body mapper; ``binary`` bodies (XML) bypass JSON decoding."""
    request: Callable[..., ProviderRequest]
    mapper: Callable[[Any, datetime], list[IngestionRecord]]
    binary: bool = False


MACRO_ADAPTERS: dict[str, PureAdapter] = {
    "world_bank": PureAdapter(world_bank_request, map_world_bank),
    "frankfurter": PureAdapter(frankfurter_request, map_frankfurter),
    "ecb": PureAdapter(ecb_request, map_ecb, binary=True),
}
