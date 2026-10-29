"""Read-only, origin-pinned collectors for explicitly configured local services.

This transport is intentionally separate from the public-only REST collector. A persisted
connector origin is the authority for the one service it names; every request is constrained
to that exact scheme, host and effective port, DNS-pinned, bounded, and redirect-free.
"""

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.schemas import ProviderRecordMetadata

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FHIR_PAGES = 10
MAX_HA_ENTITIES = 500
MAX_HA_BATCH_BYTES = 10 * 1024 * 1024
MAX_HA_REQUESTS_PER_RECEIPT = 25
MAX_HA_CURSOR_BYTES = 16_384
REQUEST_TIMEOUT_SECONDS = 20
BeforeSend = Callable[[], Awaitable[None]]


class LocalServiceError(ValueError):
    """A stable, secret-free local-service collection failure."""


@dataclass(frozen=True)
class HomeAssistantCollectionResult:
    """One bounded Home Assistant snapshot receipt and its scope-bound continuation cursor."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str
    coverage: Literal["returned_snapshot", "truncated"]


def _origin(url: str) -> tuple[str, str, int]:
    """Return a canonical HTTP(S) origin, rejecting credentials and malformed ports."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        explicit_port = parts.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise LocalServiceError("configured_origin_invalid") from exc
    if (scheme not in {"http", "https"} or not host or parts.username or parts.password
            or port < 1 or port > 65535):
        raise LocalServiceError("configured_origin_invalid")
    return scheme, host, port


def _url_for_origin(configured_origin: str, request_url: str) -> tuple[str, str, str, int]:
    """Require each generated or paginated request to retain its persisted origin and port."""
    expected = _origin(configured_origin)
    actual = _origin(request_url)
    if actual != expected:
        raise LocalServiceError("origin_changed")
    configured_parts = urlsplit(configured_origin)
    if configured_parts.query or configured_parts.fragment:
        raise LocalServiceError("configured_origin_invalid")
    parts = urlsplit(request_url)
    if parts.username or parts.password or parts.fragment:
        raise LocalServiceError("request_url_invalid")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, "")), *expected


async def fetch_configured_origin(
    configured_origin: str,
    request_url: str,
    *,
    before_send: BeforeSend,
    bearer_token: str | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    """GET one exact configured origin with DNS pinning, authorization fencing and hard bounds.

    DNS answers are resolved once and the selected address is pinned for the request while the
    original Host and TLS server name are retained. Every resolved address must be private or
    loopback for both HTTP and HTTPS. Redirects, ambient proxies, compression and responses above
    ``max_bytes`` fail.
    ``before_send`` revalidates the caller's durable source/credential fence immediately before
    bytes are sent. Network failures use stable errors without a URL or credential; caller fence
    exceptions remain intact for durable cancellation.
    """
    if not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
        raise LocalServiceError("response_limit_invalid")
    url, scheme, host, port = _url_for_origin(configured_origin, request_url)
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
            timeout=5,
        )
    except (OSError, TimeoutError) as exc:
        raise LocalServiceError("destination_unresolvable") from exc
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise LocalServiceError("destination_unresolvable")
    try:
        parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    except ValueError as exc:
        raise LocalServiceError("destination_invalid") from exc
    for address in parsed_addresses:
        if (address.is_link_local or address.is_multicast or address.is_unspecified
                or address.is_reserved and not address.is_loopback):
            raise LocalServiceError("destination_not_allowed")
        if not (address.is_private or address.is_loopback):
            raise LocalServiceError("destination_not_local")

    address_text = parsed_addresses[0].compressed
    literal = f"[{address_text}]" if ":" in address_text else address_text
    parts = urlsplit(url)
    default_port = 443 if scheme == "https" else 80
    pinned_netloc = f"{literal}:{port}" if port != default_port else literal
    pinned_url = urlunsplit((scheme, pinned_netloc, parts.path, parts.query, ""))
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    headers["Host"] = parts.netloc
    extensions: dict[str, object] = {"sni_hostname": host} if scheme == "https" else {}
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    await before_send()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False, verify=True,
        ) as client:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with client.stream("GET", pinned_url, headers=headers, extensions=extensions) as response:
                    if 300 <= response.status_code < 400:
                        raise LocalServiceError("redirect_blocked")
                    if response.status_code in {401, 403}:
                        raise LocalServiceError("authorization_rejected")
                    if response.status_code == 429:
                        raise LocalServiceError("rate_limited")
                    if response.status_code >= 400:
                        raise LocalServiceError(f"http_{response.status_code}")
                    advertised = response.headers.get("content-length")
                    if advertised and advertised.isdigit() and int(advertised) > max_bytes:
                        raise LocalServiceError("response_too_large")
                    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise LocalServiceError("compressed_response_not_supported")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise LocalServiceError("response_too_large")
                    return bytes(body)
    except LocalServiceError:
        raise
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        raise LocalServiceError("request_failed") from exc


def _decode_json(body: bytes) -> Any:
    """Decode bounded provider JSON while retaining decimal literals exactly."""
    try:
        return json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise LocalServiceError("response_not_json") from exc


def _instant(value: object) -> datetime:
    """Parse a provider timestamp as an aware UTC instant without inventing missing values."""
    if not isinstance(value, str) or not value:
        raise LocalServiceError("observation_time_missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LocalServiceError("observation_time_invalid") from exc
    if parsed.tzinfo is None:
        raise LocalServiceError("observation_time_invalid")
    return parsed.astimezone(UTC)


def map_fhir_observation(resource: object, *, patient_id: str, collected_at: datetime) -> IngestionRecord | None:
    """Map one exact-patient FHIR R4 Observation, preserving its ID, value, unit and source time.

    Non-Observation resources, other patients, and unsupported value types are skipped. A valid
    quantity retains its exact decimal representation and unit in metadata; no patient data from
    another reference is admitted.
    """
    if collected_at.tzinfo is None:
        raise LocalServiceError("collection_time_invalid")
    if not isinstance(resource, dict) or resource.get("resourceType") != "Observation":
        return None
    subject = resource.get("subject")
    reference = subject.get("reference") if isinstance(subject, dict) else None
    if reference != f"Patient/{patient_id}":
        return None
    identifier = resource.get("id")
    value_quantity = resource.get("valueQuantity")
    if not isinstance(identifier, str) or not identifier or not isinstance(value_quantity, dict):
        return None
    raw_value, unit = value_quantity.get("value"), value_quantity.get("unit")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, Decimal)) or not isinstance(unit, str):
        return None
    try:
        value = Decimal(str(raw_value))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    raw_time: object = resource.get("effectiveDateTime")
    if raw_time is None and isinstance(resource.get("effectivePeriod"), dict):
        raw_time = resource["effectivePeriod"].get("start")
    if raw_time is None:
        raw_time = resource.get("issued")
    observed_at = _instant(raw_time)
    version = resource.get("meta", {}).get("versionId") if isinstance(resource.get("meta"), dict) else None
    code = resource.get("code", {}).get("coding", []) if isinstance(resource.get("code"), dict) else []
    coding = [
        {key: item[key] for key in ("system", "code", "display") if isinstance(item.get(key), str)}
        for item in code if isinstance(item, dict)
    ][:16] if isinstance(code, list) else []
    exact_value = str(value)
    provider_record = ProviderRecordMetadata(
        provider="health",
        identity=identifier,
        provider_version=str(version) if isinstance(version, str) else None,
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=False,
        source_fields={
            "patient_id": patient_id,
            "observation_id": identifier,
            "value": exact_value,
            "unit": unit,
            "observed_at_source": str(raw_time),
        },
    )
    return IngestionRecord(
        provider_id=identifier,
        content=f"{exact_value} {unit}".strip(),
        observed_at=observed_at,
        collected_at=collected_at.astimezone(UTC),
        version=str(version) if isinstance(version, str) else None,
        metadata={
            "patient_id": patient_id,
            "observation_id": identifier,
            "value": exact_value,
            "unit": unit,
            "observed_at_source": str(raw_time),
            "coding": coding,
            "provider_record": provider_record.model_dump(mode="json", exclude_none=True),
        },
    )


async def collect_fhir_observations(
    base_url: str,
    patient_id: str,
    *,
    bearer_token: str,
    before_send: BeforeSend,
    collected_at: datetime | None = None,
    since: datetime | None = None,
) -> list[IngestionRecord]:
    """Read a bounded, patient-scoped FHIR R4 Observation search over the configured origin.

    The initial search defaults to 30 days and every Bundle next link must preserve exact origin
    and port. Only matching Patient references are returned. Cursor/receipt advancement belongs
    to the durable collection executor and is deliberately outside this provider module.
    """
    if not patient_id or "/" in patient_id or len(patient_id) > 128:
        raise LocalServiceError("patient_id_invalid")
    origin = _origin(base_url)
    parts = urlsplit(base_url)
    collected = collected_at or datetime.now(UTC)
    floor = since or (collected - timedelta(days=30))
    if floor.tzinfo is None or collected.tzinfo is None:
        raise LocalServiceError("collection_time_invalid")
    query = urlencode({"patient": patient_id, "_lastUpdated": f"ge{floor.astimezone(UTC).isoformat()}", "_count": "100"})
    current = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/Observation", query, ""))
    visited: set[str] = set()
    records: list[IngestionRecord] = []
    for _ in range(MAX_FHIR_PAGES):
        if current in visited:
            raise LocalServiceError("pagination_loop")
        visited.add(current)
        body = await fetch_configured_origin(base_url, current, before_send=before_send, bearer_token=bearer_token)
        bundle = _decode_json(body)
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle" or not isinstance(bundle.get("entry", []), list):
            raise LocalServiceError("fhir_bundle_invalid")
        for entry in bundle.get("entry", []):
            resource = entry.get("resource") if isinstance(entry, dict) else None
            mapped = map_fhir_observation(resource, patient_id=patient_id, collected_at=collected)
            if mapped is not None:
                records.append(mapped)
                if len(records) > 500:
                    raise LocalServiceError("record_limit_exceeded")
        links = bundle.get("link", [])
        next_link = next((link.get("url") for link in links if isinstance(link, dict) and link.get("relation") == "next"), None) if isinstance(links, list) else None
        if not next_link:
            return records
        expected_path = parts.path.rstrip("/") + "/Observation"
        if (not isinstance(next_link, str) or _origin(next_link) != origin
                or urlsplit(next_link).path != expected_path):
            raise LocalServiceError("pagination_origin_changed")
        current = next_link
    raise LocalServiceError("page_limit_exceeded")


async def collect_home_assistant_snapshot(
    base_url: str,
    entity_ids: tuple[str, ...],
    *,
    bearer_token: str,
    before_send: BeforeSend,
    cursor: str | None = None,
    collected_at: datetime | None = None,
) -> HomeAssistantCollectionResult:
    """Read selected Home Assistant states in receipt-sized, resumable batches.

    Each receipt performs at most 25 bounded GETs and carries a cursor bound to the ordered entity
    scope. The caller commits that cursor only with the matching durable ingestion receipt. Scope
    changes or invalid cursors restart at index zero; a failed request returns no cursor advancement.
    Only state, source timestamps, unit, name and actual source latitude/longitude are retained.
    No unselected state is fetched and no device-control endpoint is constructed.
    """
    if not entity_ids or len(entity_ids) > MAX_HA_ENTITIES or len(set(entity_ids)) != len(entity_ids):
        raise LocalServiceError("entity_scope_invalid")
    if any(not isinstance(entity_id, str) or len(entity_id) > 255
           or re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id) is None for entity_id in entity_ids):
        raise LocalServiceError("entity_scope_invalid")
    parts = urlsplit(base_url)
    collected = collected_at or datetime.now(UTC)
    if collected.tzinfo is None:
        raise LocalServiceError("collection_time_invalid")
    scope_hash = hashlib.sha256("\0".join(entity_ids).encode("utf-8")).hexdigest()
    index = 0
    if cursor is not None and len(cursor.encode("utf-8")) <= MAX_HA_CURSOR_BYTES:
        try:
            saved = json.loads(cursor)
        except (TypeError, ValueError):
            saved = None
        saved_version = saved.get("v") if isinstance(saved, dict) else None
        if isinstance(saved, dict) and isinstance(saved_version, int) and not isinstance(saved_version, bool) \
                and saved_version == 1 \
                and saved.get("provider") == "home_assistant" \
                and saved.get("scope_hash") == scope_hash:
            saved_index = saved.get("index")
            if (not isinstance(saved_index, bool) and isinstance(saved_index, int)
                    and 0 <= saved_index < len(entity_ids)):
                index = saved_index
    records: list[IngestionRecord] = []
    total_bytes = 0
    requests = 0
    while index < len(entity_ids) and requests < MAX_HA_REQUESTS_PER_RECEIPT:
        entity_id = entity_ids[index]
        path = parts.path.rstrip("/") + "/api/states/" + quote(entity_id, safe=".")
        request_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        try:
            body = await fetch_configured_origin(base_url, request_url, before_send=before_send, bearer_token=bearer_token)
        except LocalServiceError as exc:
            if str(exc) == "http_404":
                index += 1
                requests += 1
                continue
            raise
        requests += 1
        total_bytes += len(body)
        if total_bytes > MAX_HA_BATCH_BYTES:
            # Keep the current index for the next receipt; earlier entities remain committed.
            break
        state = _decode_json(body)
        if not isinstance(state, dict) or state.get("entity_id") != entity_id:
            raise LocalServiceError("home_assistant_response_invalid")
        raw_state = state.get("state")
        raw_attributes = state.get("attributes")
        attributes: dict[str, Any] = raw_attributes if isinstance(raw_attributes, dict) else {}
        raw_time = state.get("last_updated") or state.get("last_changed")
        observed_at = _instant(raw_time)
        source_fields: dict[str, object] = {}
        for key in ("unit_of_measurement", "friendly_name", "latitude", "longitude"):
            value = attributes.get(key)
            if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool):
                source_fields[key] = str(value)[:512 if key == "friendly_name" else 64]
        record_value = str(raw_state) if raw_state is not None else ""
        provider_record = ProviderRecordMetadata(
            provider="home_assistant",
            identity=entity_id,
            provider_version=str(state.get("last_updated") or state.get("last_changed") or "snapshot"),
            timestamp_basis="collection",
            coverage="returned_snapshot",
            content_truncated=False,
            source_fields={
                "entity_id": entity_id,
                "state": record_value[:512],
                "observed_at_source": str(raw_time),
                **source_fields,
            },
        )
        records.append(IngestionRecord(
            provider_id=entity_id,
            content=record_value,
            observed_at=observed_at,
            collected_at=collected.astimezone(UTC),
            version=str(state.get("last_updated") or state.get("last_changed") or "snapshot"),
            metadata={
                "entity_id": entity_id, "state": record_value[:512], "observed_at_source": str(raw_time),
                **source_fields, "provider_record": provider_record.model_dump(mode="json", exclude_none=True),
            },
        ))
        index += 1
    complete = index >= len(entity_ids)
    cursor_after = json.dumps(
        {"v": 1, "provider": "home_assistant", "scope_hash": scope_hash,
         "index": 0 if complete else index},
        separators=(",", ":"),
    )
    return HomeAssistantCollectionResult(
        tuple(records), cursor_after, "returned_snapshot" if complete else "truncated",
    )


__all__ = [
    "HomeAssistantCollectionResult",
    "LocalServiceError",
    "collect_fhir_observations",
    "collect_home_assistant_snapshot",
    "fetch_configured_origin",
    "map_fhir_observation",
]
