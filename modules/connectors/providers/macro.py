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


def naive_utc_instant(raw: object, collected_at: datetime) -> datetime:
    """Provider ISO time documented as UTC but sent without a zone (NOAA SWPC, NVD) -> aware UTC.

    An explicit zone is honoured; future beyond the 10-minute skew is invalid like ``provider_instant``.
    """
    try:
        if not isinstance(raw, str) or len(raw) > 40:
            raise ValueError
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProviderPayloadError("provider_time_invalid") from exc
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
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
        provider=provider, metric=metric, value=as_float, unit=unit, currency=currency,
        timezone=None, symbol=symbol, region=region, latitude=latitude, longitude=longitude,
        published_at=None, quality="missing" if value is None else quality,
        missing_reason="provider_null" if value is None else None, provider_fields=fields,
    )
    envelope = ProviderRecordMetadata(
        provider=provider, identity=identity, provider_version=version,
        timestamp_basis=timestamp_basis, coverage=coverage, content_truncated=False,
        provider_modified_at=modified_at, world_data=measurement,
    )
    return IngestionRecord(
        provider_id=identity, content=content, observed_at=observed_at, collected_at=collected_at,
        version=version, metadata={"title": title, "provider_record": envelope.model_dump(mode="json")},
    )


# --------------------------------------------------------------------------- World Bank

WORLD_BANK_URL = "https://api.worldbank.org/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3"
WORLD_BANK_INDICATORS: tuple[tuple[str, str, str], ...] = (
    ("VN", "NY.GDP.MKTP.CD", "gdp_current_usd"),
)
FRED_SERIES: tuple[tuple[str, str, str], ...] = (
    ("DFF", "fed_funds_rate", "percent"),
    ("CPIAUCSL", "us_cpi_index", "index"),
    ("DGS10", "us_10y_treasury_yield", "percent"),
)
_WB_MAX_ROWS = 50
_WB_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_WB_INDICATOR_RE = re.compile(r"^[A-Z0-9._-]{3,40}$")
_WB_ISO2_META: dict[str, tuple[str, str]] = {
    "VN": ("VNM", "Viet Nam"),
    "US": ("USA", "United States"),
    "EU": ("EUU", "European Union"),
    "JP": ("JPN", "Japan"),
    "CN": ("CHN", "China"),
    "GB": ("GBR", "United Kingdom"),
}


def world_bank_request(
    country: str = "VN",
    indicator: str = "NY.GDP.MKTP.CD",
    per_page: int = 3,
    *,
    indicators: tuple[tuple[str, str, str], ...] = WORLD_BANK_INDICATORS,
) -> ProviderRequest:
    """Build a World Bank indicator GET request; defaults to the approved Viet Nam GDP endpoint."""
    target_country = country
    target_indicator = indicator
    if indicators != WORLD_BANK_INDICATORS and indicators:
        target_country, target_indicator, _ = indicators[0]
    if not _WB_COUNTRY_RE.fullmatch(target_country) or not _WB_INDICATOR_RE.fullmatch(target_indicator):
        raise ProviderPayloadError("world_bank_scope_mismatch")
    if per_page < 1 or per_page > _WB_MAX_ROWS:
        raise ProviderPayloadError("world_bank_paging_invalid")
    if target_country == "VN" and target_indicator == "NY.GDP.MKTP.CD" and per_page == 3:
        return ProviderRequest("GET", WORLD_BANK_URL)
    url = (
        f"https://api.worldbank.org/v2/country/{target_country}"
        f"/indicator/{target_indicator}?format=json&per_page={per_page}"
    )
    return ProviderRequest("GET", url)


def map_world_bank(
    payload: object,
    collected_at: datetime,
    *,
    indicators: tuple[tuple[str, str, str], ...] = WORLD_BANK_INDICATORS,
) -> list[IngestionRecord]:
    """Map annual World Bank indicator rows; null stays missing and never replaces earlier valid years."""
    if not indicators:
        raise ProviderPayloadError("world_bank_scope_mismatch")
    allowed_indicators: dict[tuple[str, str], str] = {
        (country_code, indicator_id): metric_name
        for country_code, indicator_id, metric_name in indicators
    }
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
    seen: set[tuple[str, str, str]] = set()
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderPayloadError("world_bank_row_invalid")
        indicator, country = row.get("indicator"), row.get("country")
        if not isinstance(indicator, dict) or not isinstance(country, dict):
            raise ProviderPayloadError("world_bank_scope_mismatch")
        country_id = country.get("id")
        indicator_id = indicator.get("id")
        if not isinstance(country_id, str) or not isinstance(indicator_id, str):
            raise ProviderPayloadError("world_bank_scope_mismatch")
        metric_name = allowed_indicators.get((country_id, indicator_id))
        if metric_name is None:
            raise ProviderPayloadError("world_bank_scope_mismatch")
        iso3_meta = _WB_ISO2_META.get(country_id)
        iso3_code = row.get("countryiso3code")
        if iso3_meta is not None:
            if iso3_code != iso3_meta[0]:
                raise ProviderPayloadError("world_bank_scope_mismatch")
            region_name = iso3_meta[1]
        else:
            if not isinstance(iso3_code, str) or not re.fullmatch(r"[A-Z]{3}", iso3_code):
                raise ProviderPayloadError("world_bank_scope_mismatch")
            country_value = country.get("value")
            region_name = country_value if isinstance(country_value, str) and country_value.strip() else country_id
        period = row.get("date")
        if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}", period) or (country_id, indicator_id, period) in seen:
            raise ProviderPayloadError("world_bank_period_invalid")
        seen.add((country_id, indicator_id, period))
        observed_at = datetime(int(period), 1, 1, tzinfo=UTC)
        if observed_at > collected_at:
            raise ProviderPayloadError("world_bank_period_in_future")
        raw = row.get("value")
        try:
            value = None if raw is None else finite_decimal(raw)
        except ValueError as exc:
            raise ProviderPayloadError("world_bank_value_invalid") from exc
        text = "null" if value is None else decimal_text(value)
        # Preserve exact canonical title and content for the default Viet Nam GDP indicator.
        if country_id == "VN" and indicator_id == "NY.GDP.MKTP.CD":
            content = f"Viet Nam GDP (current US$) {period}: {'not reported' if value is None else text + ' USD'}"
            title = f"Viet Nam GDP {period}"
        else:
            content = (
                f"{region_name} {indicator_id} (current US$) {period}: "
                f"{'not reported' if value is None else text + ' USD'}"
            )
            title = f"{region_name} {indicator_id} {period}"
        records.append(build_record(
            provider="world_bank", identity=f"world_bank:{country_id}:{indicator_id}:{period}",
            content=content,
            title=title, observed_at=observed_at, collected_at=collected_at,
            version=content_version("world_bank", country_id, indicator_id, period, text),
            metric=metric_name, value=value, unit="current US$", currency="USD", region=region_name,
            quality="provider_reported", coverage=coverage,
            provider_fields={"country": country_id, "indicator": indicator_id, "period": period, "unit": "current US$"},
        ))
    return records


def fetch_world_bank_indicators(
    payloads_by_indicator: dict[tuple[str, str], object],
    collected_at: datetime,
    *,
    indicators: tuple[tuple[str, str, str], ...] = WORLD_BANK_INDICATORS,
) -> list[IngestionRecord]:
    """Map a batch of pre-fetched World Bank payloads keyed by ``(country_iso2, indicator_id)``."""
    records: list[IngestionRecord] = []
    for country_id, indicator_id, metric_name in indicators:
        payload = payloads_by_indicator.get((country_id, indicator_id))
        if payload is None:
            raise ProviderPayloadError("world_bank_response_invalid")
        records.extend(
            map_world_bank(
                payload,
                collected_at,
                indicators=((country_id, indicator_id, metric_name),),
            )
        )
    return records


def map_fred_series(
    payload: object,
    collected_at: datetime,
    *,
    series_id: str = "DFF",
    series: tuple[tuple[str, str, str], ...] = FRED_SERIES,
) -> list[IngestionRecord]:
    """Map FRED observations JSON into typed ``IngestionRecord`` items for a configured series."""
    allowed_series = {sid: (metric, unit) for sid, metric, unit in series}
    if series_id not in allowed_series:
        raise ProviderPayloadError("fred_scope_mismatch")
    metric_name, unit_name = allowed_series[series_id]
    if not isinstance(payload, dict):
        raise ProviderPayloadError("fred_response_invalid")
    if "error_code" in payload or "error_message" in payload:
        raise ProviderPayloadError("fred_error_payload")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations or len(observations) > _WB_MAX_ROWS:
        raise ProviderPayloadError("fred_observations_invalid")
    seen: set[str] = set()
    records: list[IngestionRecord] = []
    for item in observations:
        if not isinstance(item, dict):
            raise ProviderPayloadError("fred_observations_invalid")
        day = parse_iso_date(item.get("date"))
        iso = day.isoformat()
        if iso in seen:
            raise ProviderPayloadError("fred_date_duplicate")
        seen.add(iso)
        observed_at = day_start(day)
        if observed_at > collected_at:
            raise ProviderPayloadError("fred_date_in_future")
        raw_val = item.get("value")
        try:
            # FRED encodes missing observations as "."
            value = None if raw_val in {None, "."} else finite_decimal(raw_val)
        except ValueError as exc:
            raise ProviderPayloadError("fred_value_invalid") from exc
        text = "null" if value is None else decimal_text(value)
        records.append(build_record(
            provider="fred",
            identity=f"fred:{series_id}:{iso}",
            content=f"FRED {series_id} ({metric_name}) {iso}: {'not reported' if value is None else text + ' ' + unit_name}",
            title=f"FRED {series_id} {iso}",
            observed_at=observed_at,
            collected_at=collected_at,
            version=content_version("fred", series_id, iso, text),
            metric=metric_name,
            value=value,
            unit=unit_name,
            quality="provider_reported",
            provider_fields={"series_id": series_id, "date": iso, "unit": unit_name},
        ))
    return records


# --------------------------------------------------------------------------- Frankfurter

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/usd/vnd"


def frankfurter_request() -> ProviderRequest:
    """Return the fixed GET request for the Frankfurter USD/VND reference rate endpoint."""
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
    """Return the fixed GET request for the ECB daily euro reference rates XML feed."""
    return ProviderRequest("GET", ECB_URL)


def _local(tag: str) -> str:
    """Strip any XML namespace URI prefix from an ElementTree tag name."""
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


# --------------------------------------------------------------------------- Open ER API (daily FX)

OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/USD"
OPEN_ER_QUOTES = ("VND", "EUR", "JPY", "GBP", "CNY")  # fixed quote set: one request, a handful of records


def open_er_api_request() -> ProviderRequest:
    """Return the fixed keyless USD-base daily rate request (open.er-api.com)."""
    return ProviderRequest("GET", OPEN_ER_API_URL)


def map_open_er_api(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map USD-base daily rates for ``OPEN_ER_QUOTES``; one record per quote and provider update day.

    The provider updates once a day and names that instant (``time_last_update_unix``). A non-success
    result, a quota error or a missing quote raises ``ProviderPayloadError`` rather than a partial series.
    """
    if not isinstance(payload, dict):
        raise ProviderPayloadError("open_er_api_response_invalid")
    if payload.get("result") != "success":
        kind = "rate_limited" if payload.get("error-type") == "quota-reached" else "invalid"
        raise ProviderPayloadError("open_er_api_error_payload", kind)  # type: ignore[arg-type]
    rates = payload.get("rates")
    if payload.get("base_code") != "USD" or not isinstance(rates, dict):
        raise ProviderPayloadError("open_er_api_scope_mismatch")
    updated = provider_instant(payload.get("time_last_update_unix"), collected_at, unit="s")
    iso = updated.date().isoformat()
    records = []
    for quote in OPEN_ER_QUOTES:
        try:
            rate = positive_decimal(rates.get(quote))
        except ValueError as exc:
            raise ProviderPayloadError("open_er_api_rate_invalid") from exc
        text = decimal_text(rate)
        records.append(build_record(
            provider="open_er_api", identity=f"open_er_api:USD:{quote}:{iso}",
            content=f"Open ER API daily rate {iso}: 1 USD = {text} {quote}",
            title=f"USD/{quote} daily rate", observed_at=updated, collected_at=collected_at,
            version=content_version("open_er_api", quote, iso, text), metric="fx_reference_rate",
            value=rate, unit=f"{quote} per USD", currency=quote, symbol=f"USD/{quote}", quality="reference",
            timestamp_basis="provider_modified", modified_at=updated,
            provider_fields={"date": iso, "base": "USD", "quote": quote},
        ))
    return records


# --------------------------------------------------------------------------- SDMX-JSON (OECD CLI)

OECD_CLI_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA+CHN+JPN+DEU+GBR.M.LI...AA...H"
    "?lastNObservations=24&dimensionAtObservation=AllDimensions"
)
_SDMX_MAX_OBSERVATIONS = 200
_PERIOD_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def oecd_cli_request() -> ProviderRequest:
    """Return the fixed OECD composite-leading-indicator request (SDMX-JSON 2.0, last 24 monthly observations per area)."""
    return ProviderRequest("GET", OECD_CLI_URL, {"Accept": "application/vnd.sdmx.data+json; version=2"})


def map_sdmx_json(
    provider: str, payload: object, collected_at: datetime, *, metric: str, unit: str, title: str,
) -> list[IngestionRecord]:
    """Map an SDMX-JSON 2.0 ``AllDimensions`` data message to one record per (REF_AREA, TIME_PERIOD).

    Observation keys are colon-joined indexes into the message's observation dimensions; the first
    observation value is the measure. Periods ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` are observed at their
    UTC start. Any structural mismatch raises ``ProviderPayloadError`` so a changed shape is not silent.
    """
    code = f"{provider}_response_invalid"
    data = payload.get("data") if isinstance(payload, dict) else None
    try:
        observations = data["dataSets"][0]["observations"]  # type: ignore[index]
        dims = data["structures"][0]["dimensions"]["observation"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderPayloadError(code) from exc
    if not isinstance(observations, dict) or not isinstance(dims, list) or not observations:
        raise ProviderPayloadError(code)
    if len(observations) > _SDMX_MAX_OBSERVATIONS:
        raise ProviderPayloadError(f"{provider}_too_many_observations", "incomplete")
    records = []
    for key, obs in observations.items():
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) != len(dims) or not all(p.isdigit() for p in parts) or not isinstance(obs, list) or not obs:
            raise ProviderPayloadError(code)
        try:
            members = {d["id"]: d["values"][int(i)] for d, i in zip(dims, parts, strict=True)}
            area, period = members["REF_AREA"]["id"], members["TIME_PERIOD"]["id"]
            value = finite_decimal(obs[0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderPayloadError(code) from exc
        match = _PERIOD_RE.match(str(period))
        if not match or not isinstance(area, str) or not re.fullmatch(r"[A-Z0-9_]{2,16}", area):
            raise ProviderPayloadError(code)
        observed = datetime(int(match[1]), int(match[2] or 1), int(match[3] or 1), tzinfo=UTC)
        text = decimal_text(value)
        records.append(build_record(
            provider=provider, identity=f"{provider}:{area}:{period}",
            content=f"{title} {area} {period}: {text} {unit}", title=f"{title} {area}",
            observed_at=observed, collected_at=collected_at,
            # symbol is part of the version so rows stored before ``symbol=area`` existed are re-upserted with it
            version=content_version(provider, area, area, period, text), metric=metric, value=value, unit=unit,
            region=area, symbol=area, quality="provider_reported",
            provider_fields={"area": area, "period": str(period)},
        ))
    return sorted(records, key=lambda r: r.provider_id)


def map_oecd_cli(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """OECD composite leading indicator (amplitude adjusted, index) per configured area and period."""
    return map_sdmx_json(
        "oecd_cli", payload, collected_at, metric="composite_leading_indicator", unit="index",
        title="OECD composite leading indicator",
    )


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
    "open_er_api": PureAdapter(open_er_api_request, map_open_er_api),
    "oecd_cli": PureAdapter(oecd_cli_request, map_oecd_cli),
}
