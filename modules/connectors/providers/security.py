"""Pure CISA Known Exploited Vulnerabilities (KEV) adapter: one fixed JSON catalog, newest additions only."""

import json
import re
from datetime import datetime

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    day_start,
    parse_iso_date,
)
from modules.ingestion.schemas import IngestionRecord

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_MAX_BYTES = 4 * 1024 * 1024  # full catalog is ~1.8 MB and grows; collection.PURE_MAX_BYTES uses the same bound
_MAX_RECORDS = 200  # newest dateAdded first; the older tail is reported as truncated, never silently dropped
_MAX_ROWS = 20_000  # sanity bound on the decoded catalog
_CVE_RE = re.compile(r"^CVE-[0-9]{4}-[0-9]{4,10}$")


def cisa_kev_request() -> ProviderRequest:
    """Fixed official CISA feed (www.cisa.gov); no parameters, no key."""
    return ProviderRequest("GET", KEV_URL)


def _text(row: dict[str, object], key: str, limit: int, *, required: bool = False) -> str | None:
    """Return printable, stripped, length-bounded text; a missing required field invalidates the payload."""
    raw = row.get(key)
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()[:limit] if isinstance(raw, str) else ""
    if required and not cleaned:
        raise ProviderPayloadError("cisa_kev_row_invalid")
    return cleaned or None


def map_cisa_kev(body: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Map the newest ``_MAX_RECORDS`` catalog rows to one record per CVE (dateAdded is observed_at).

    ``body`` is raw bytes (the adapter is ``binary``) so this module owns the larger size bound. The
    catalog is a full snapshot, so a larger catalog is flagged ``truncated`` coverage. Duplicate CVE
    ids, a ``count`` mismatch or any malformed row raise ``ProviderPayloadError``. Pure; no I/O.
    """
    if len(body) > KEV_MAX_BYTES:
        raise ProviderPayloadError("provider_response_too_large")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise ProviderPayloadError("provider_response_not_json") from exc
    rows = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise ProviderPayloadError("cisa_kev_response_invalid")
    if "count" in payload and payload["count"] != len(rows):
        raise ProviderPayloadError("cisa_kev_count_mismatch", "incomplete")
    seen: set[str] = set()
    parsed = []
    for row in rows:
        cve = row.get("cveID") if isinstance(row, dict) else None
        if not isinstance(cve, str) or not _CVE_RE.fullmatch(cve) or cve in seen:
            raise ProviderPayloadError("cisa_kev_row_invalid")
        seen.add(cve)
        parsed.append((parse_iso_date(row.get("dateAdded")), cve, row))
    parsed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    coverage = "truncated" if len(parsed) > _MAX_RECORDS else "returned_snapshot"
    records = []
    for added, cve, row in parsed[:_MAX_RECORDS]:
        observed = day_start(added)
        if (observed - collected_at).total_seconds() > 600:
            raise ProviderPayloadError("provider_time_invalid")
        name = _text(row, "vulnerabilityName", 300, required=True)
        description = _text(row, "shortDescription", 1500)
        action = _text(row, "requiredAction", 500)
        due = _text(row, "dueDate", 10)
        if due is not None:
            parse_iso_date(due)
        ransomware = _text(row, "knownRansomwareCampaignUse", 32)
        fields: dict[str, str | float | int | None] = {
            "cveID": cve, "vendorProject": _text(row, "vendorProject", 200), "product": _text(row, "product", 200),
            "dateAdded": added.isoformat(), "dueDate": due, "knownRansomwareCampaignUse": ransomware,
        }
        content = "\n".join(part for part in (name, description, action) if part)
        records.append(build_record(
            provider="cisa_kev", identity=cve, content=content, title=f"{cve} {name}"[:500],
            observed_at=observed, collected_at=collected_at,
            version=content_version("cisa_kev", cve, name, description, action, due, ransomware),
            # An event listing has no number: value=None is the schema's explicit "missing" form.
            metric="known_exploited_vulnerability", value=None, unit="listing", quality="provider_reported",
            symbol=cve, timestamp_basis="provider_published", coverage=coverage,  # type: ignore[arg-type]
            provider_fields=fields, decimal_key=None,
        ))
    return records


SECURITY_ADAPTERS: dict[str, PureAdapter] = {"cisa_kev": PureAdapter(cisa_kev_request, map_cisa_kev, binary=True)}
