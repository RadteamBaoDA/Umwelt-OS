"""Bounded Alpha Vantage daily and Open-Meteo weather collection adapters."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import math
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from modules.connectors.public import ConnectorConfig, ProviderCollectionPage, ProviderRateLimited
from modules.connectors.models import ConnectorProvisioning, ConnectorWorldCredential
from modules.connectors.credentials import decrypt_credential_input, secret_fingerprint
from modules.connectors.providers.feed_catalog import _retry_deadline
from modules.ingestion.schemas import IngestionRecord
from modules.sources.schemas import ConnectorSource

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_WEATHER_VARIABLES = {
    "temperature_2m": "temperature_2m",
    "relative_humidity_2m": "relative_humidity_2m",
    "precipitation": "precipitation",
    "wind_speed_10m": "wind_speed_10m",
}


async def get_alpha_vantage_key(session: AsyncSession, source: ConnectorSource, settings: Settings) -> tuple[str, UUID] | None:
    """Decrypt a key only while source and connector revision fences still match."""
    credential = await session.get(ConnectorWorldCredential, source.id)
    provisioning = await session.get(ConnectorProvisioning, source.id)
    if (
        credential is None or provisioning is None or source.provider != "alpha_vantage"
        or credential.provider != "alpha_vantage" or credential.source_generation != source.generation
        or credential.configuration_revision != provisioning.desired_revision
        or provisioning.source_generation != source.generation
    ):
        return None
    key = settings.connector_credential_encryption_key.get_secret_value()
    request, binding = decrypt_credential_input(
        key, credential.encrypted_key, source_id=source.id,
        slot="native:alpha_vantage", operation_id=credential.operation_id,
    )
    api_key = request.get("api_key")
    if (
        not isinstance(api_key, str) or binding.get("provider") != "alpha_vantage"
        or binding.get("source_generation") != source.generation
        or binding.get("configuration_revision") != provisioning.desired_revision
        or binding.get("fingerprint") != secret_fingerprint(key, api_key)
    ):
        raise ValueError("alpha_vantage_credentials_unavailable")
    return api_key, credential.operation_id


async def reserve_alpha_vantage_daily_calls(
    redis: object, *, key_reference: str, calls: int, now: datetime,
) -> datetime | None:
    """Atomically reserve a bounded UTC-day call budget before any provider egress.

    The Redis key contains only a keyed credential reference and day. Reservations
    are conservative: failed or interrupted requests are not refunded.
    """
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)
    ttl = int((next_day - now.astimezone(UTC)).total_seconds()) + 3600
    redis_key = f"connectors:provider:quota:alpha_vantage:{key_reference}:{day_start:%Y%m%d}"
    script = """
    local used = tonumber(redis.call('GET', KEYS[1]) or '0')
    local amount = tonumber(ARGV[1])
    local maximum = tonumber(ARGV[2])
    if used + amount > maximum then return 0 end
    used = redis.call('INCRBY', KEYS[1], amount)
    if used == amount then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3])) end
    return used
    """
    reserved = await redis.eval(script, 1, redis_key, calls, 25, ttl)
    return None if reserved else next_day


async def _json_get(
    url: str, *, params: dict[str, str],
    before_request: Callable[[], Awaitable[None]],
) -> dict[str, object]:
    """Fetch one fixed-host HTTPS JSON response with a hard byte and time ceiling."""
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30), trust_env=False, follow_redirects=False, verify=True
            ) as client:
                await before_request()
                async def final_send_fence(event_name: str, info: dict[str, object]) -> None:
                    """Recheck source and credential authority after connection admission, before headers send."""
                    if event_name.endswith("send_request_headers.started"):
                        await before_request()

                async with client.stream(
                    "GET", url, params=params, headers={"Accept": "application/json"},
                    extensions={"trace": final_send_fence},
                ) as response:
                    if response.status_code == 429:
                        raise ProviderRateLimited(_retry_deadline(response.headers, datetime.now(UTC)))
                    if response.is_redirect or response.status_code >= 400:
                        raise ValueError("world_data_provider_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise ValueError("world_data_response_too_large")
        decoded = json.loads(body)
    except ProviderRateLimited:
        raise
    except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("world_data_provider_unavailable") from exc
    if not isinstance(decoded, dict):
        raise ValueError("world_data_response_invalid")
    return decoded


def _finite_number(raw: object) -> float:
    """Parse a provider-declared finite numeric observation without coercing missing values."""
    try:
        value = float(raw)  # Alpha Vantage publishes numeric strings.
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("world_data_measurement_invalid") from exc
    if not math.isfinite(value):
        raise ValueError("world_data_measurement_invalid")
    return value


def _market_records(source: ConnectorSource, config: ConnectorConfig, payload: dict[str, object], collected_at: datetime) -> list[IngestionRecord]:
    """Map raw daily OHLCV rows while retaining configured currency and exchange-timezone claims."""
    metadata = payload.get("Meta Data")
    series = payload.get("Time Series (Daily)")
    if not isinstance(metadata, dict) or not isinstance(series, dict):
        envelope = next((
            (key, payload.get(key)) for key in ("Note", "Information", "Error Message")
            if key in payload
        ), None)
        if envelope is not None:
            field, message = envelope
            normalized = message.lower() if isinstance(message, str) else ""
            quota_markers = ("rate limit", "call frequency", "requests per day", "requests per minute", "api call frequency")
            if any(marker in normalized for marker in quota_markers):
                now = datetime.now(UTC)
                if "per minute" in normalized:
                    retry_at = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                elif "per day" in normalized or "daily" in normalized:
                    retry_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                else:
                    retry_at = now + timedelta(hours=1)
                raise ProviderRateLimited(retry_at)
            if field == "Error Message" and "invalid api call" in normalized:
                raise ValueError("alpha_vantage_symbol_or_endpoint_unavailable")
            if "premium" in normalized or "entitlement" in normalized:
                raise ValueError("alpha_vantage_entitlement_unavailable")
            raise ValueError("alpha_vantage_access_unavailable")
        raise ValueError("alpha_vantage_response_invalid")
    provider_symbol = metadata.get("2. Symbol")
    if not isinstance(provider_symbol, str) or provider_symbol.upper() not in set(config.market_symbols or ()):
        raise ValueError("alpha_vantage_scope_mismatch")
    # The daily endpoint supplies dates but does not establish the exchange timezone/currency;
    # the explicitly configured source metadata stays attached rather than being guessed.
    zone = ZoneInfo(config.market_exchange_timezone or "")
    records: list[IngestionRecord] = []
    # One daily point per configured symbol per trigger keeps the receipt within the
    # existing 500-record ingress limit; historical points accumulate across schedules.
    for day, row in sorted(series.items(), reverse=True)[:1]:
        if not isinstance(day, str) or not isinstance(row, dict):
            raise ValueError("alpha_vantage_response_invalid")
        try:
            observed_at = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone).astimezone(UTC)
        except ValueError as exc:
            raise ValueError("alpha_vantage_timestamp_invalid") from exc
        raw_measurements = {
            "open": row.get("1. open"), "high": row.get("2. high"),
            "low": row.get("3. low"), "close": row.get("4. close"),
            "volume": row.get("5. volume"),
        }
        for metric, raw in raw_measurements.items():
            unit = config.market_currency if metric != "volume" else "shares"
            records.append(IngestionRecord(
                provider_id=f"alpha_vantage:{provider_symbol}:{day}:{metric}",
                content=f"{provider_symbol} {day} {metric} {raw} {unit}",
                observed_at=observed_at, collected_at=collected_at,
                version=None,
                metadata={"title": f"{provider_symbol} {metric}", "world_data": {
                    "provider": "alpha_vantage", "metric": metric,
                    "value": _finite_number(raw), "unit": unit,
                    "currency": config.market_currency if metric != "volume" else None,
                    "timezone": config.market_exchange_timezone,
                    "symbol": provider_symbol, "region": None, "latitude": None,
                    "longitude": None, "published_at": None, "quality": "provider_reported",
                    "provider_fields": {"date": day, "symbol": provider_symbol, **raw_measurements},
                }},
            ))
    if len(records) > 5_000:
        raise ValueError("alpha_vantage_result_exceeds_point_limit")
    return records


def _weather_records(source: ConnectorSource, config: ConnectorConfig, payload: dict[str, object], collected_at: datetime) -> list[IngestionRecord]:
    """Map bounded current/hourly values using the returned Open-Meteo units and source timezone."""
    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    returned_timezone = payload.get("timezone")
    if not isinstance(hourly, dict) or not isinstance(units, dict) or returned_timezone != config.weather_timezone:
        raise ValueError("open_meteo_response_invalid")
    times = hourly.get("time")
    if not isinstance(times, list) or len(times) > 168 or any(not isinstance(item, str) for item in times):
        raise ValueError("open_meteo_time_scope_invalid")
    zone = ZoneInfo(config.weather_timezone or "")
    records: list[IngestionRecord] = []
    for metric in config.weather_metrics or ():
        values = hourly.get(metric)
        unit = units.get(metric)
        if not isinstance(values, list) or len(values) != len(times) or not isinstance(unit, str) or not unit:
            raise ValueError("open_meteo_measurement_scope_invalid")
        for stamp, raw in zip(times, values, strict=True):
            if not isinstance(stamp, str):
                raise ValueError("open_meteo_timestamp_invalid")
            try:
                observed_at = datetime.fromisoformat(stamp).replace(tzinfo=zone).astimezone(UTC)
            except ValueError as exc:
                raise ValueError("open_meteo_timestamp_invalid") from exc
            records.append(IngestionRecord(
                provider_id=f"open_meteo:{source.id}:{metric}:{observed_at.isoformat()}",
                content=f"{metric} {raw} {unit} at {stamp} {returned_timezone}",
                observed_at=observed_at, collected_at=collected_at,
                version=None,
                metadata={"title": f"Weather {metric}", "world_data": {
                    "provider": "open_meteo", "metric": metric,
                    "value": _finite_number(raw) if raw is not None else None, "unit": unit, "currency": None,
                    "timezone": returned_timezone,
                    "symbol": None, "region": None,
                    "latitude": config.weather_latitude, "longitude": config.weather_longitude,
                    "published_at": None, "quality": "forecast" if raw is not None else "missing",
                    "missing_reason": None if raw is not None else "provider_value_missing",
                    "provider_fields": {
                        "time": stamp, "timezone": returned_timezone,
                        "utc_offset_seconds": payload.get("utc_offset_seconds"),
                        "latitude": payload.get("latitude"), "longitude": payload.get("longitude"),
                    },
                }},
            ))
    return records


async def collect_world_data(
    source: ConnectorSource, *, collected_at: datetime, settings: Settings, session: AsyncSession,
    redis: object, before_request: Callable[[UUID | None], Awaitable[None]],
) -> ProviderCollectionPage:
    """Fetch one configured provider scope through the existing leased collector path."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    config = ConnectorConfig.model_validate(source.configuration)
    if source.provider == "open_meteo":
        metrics = list(config.weather_metrics or ())
        payload = await _json_get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": str(config.weather_latitude), "longitude": str(config.weather_longitude),
            "hourly": ",".join(_WEATHER_VARIABLES[name] for name in metrics),
            "forecast_days": "3", "timezone": str(config.weather_timezone),
        }, before_request=lambda: before_request(None))
        records = _weather_records(source, config, payload, collected_at)
        coverage = "truncated" if any(
            isinstance(payload.get("hourly"), dict)
            and isinstance(payload["hourly"].get(metric), list)
            and len(payload["hourly"][metric]) >= 72 for metric in metrics
        ) else "returned_snapshot"
        return ProviderCollectionPage(records=tuple(records), coverage=coverage)
    if source.provider != "alpha_vantage":
        raise ValueError("provider_scope_invalid")
    credential = await get_alpha_vantage_key(session, source, settings)
    if credential is None:
        raise ValueError("alpha_vantage_credentials_required")
    key, operation_id = credential
    key_reference = secret_fingerprint(settings.connector_credential_encryption_key.get_secret_value(), key)
    next_day = await reserve_alpha_vantage_daily_calls(
        redis, key_reference=key_reference, calls=len(config.market_symbols or ()), now=datetime.now(UTC),
    )
    if next_day is not None:
        raise ProviderRateLimited(next_day)
    records: list[IngestionRecord] = []
    for symbol in config.market_symbols or ():
        payload = await _json_get("https://www.alphavantage.co/query", params={
            "function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact", "apikey": key,
        }, before_request=lambda: before_request(operation_id))
        records.extend(_market_records(source, config, payload, collected_at))
    return ProviderCollectionPage(records=tuple(records), coverage="returned_snapshot", credential_operation_id=operation_id)
