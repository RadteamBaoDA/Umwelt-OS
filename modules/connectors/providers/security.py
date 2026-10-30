"""Pure CISA Known Exploited Vulnerabilities (KEV) adapter: one fixed JSON catalog, newest additions only."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    day_start,
    decimal_text,
    finite_decimal,
    naive_utc_instant,
    parse_iso_date,
    provider_instant,
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


# --------------------------------------------------------------------------- disease.sh (global COVID-19 totals)

DISEASE_SH_URL = "https://disease.sh/v3/covid-19/all"
_DISEASE_METRICS = ("cases", "deaths", "recovered", "active")


def disease_sh_request() -> ProviderRequest:
    """Return the fixed keyless disease.sh global totals request."""
    return ProviderRequest("GET", DISEASE_SH_URL)


def map_disease_sh(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map global cumulative totals to one record per metric, keyed by metric so a refresh is a new revision of the same series (chart history comes from revisions).

    ``updated`` is the provider's own instant and is kept as ``observed_at`` so staleness stays visible.
    Negative or non-numeric totals raise ``ProviderPayloadError``.
    """
    if not isinstance(payload, dict):
        raise ProviderPayloadError("disease_sh_response_invalid")
    updated = provider_instant(payload.get("updated"), collected_at, unit="ms")
    stamp = int(updated.timestamp() * 1000)
    records = []
    for metric in _DISEASE_METRICS:
        try:
            value = finite_decimal(payload.get(metric))
        except ValueError as exc:
            raise ProviderPayloadError("disease_sh_value_invalid") from exc
        if value < 0:
            raise ProviderPayloadError("disease_sh_value_invalid")
        text = decimal_text(value)
        records.append(build_record(
            provider="disease_sh", identity=f"disease_sh:global:{metric}",
            content=f"Global COVID-19 {metric} (cumulative): {text}", title=f"Global COVID-19 {metric}",
            observed_at=updated, collected_at=collected_at,
            version=content_version("disease_sh", metric, text, stamp),
            metric=f"covid19_{metric}", value=value, unit="persons", region="Global", quality="provider_reported",
            timestamp_basis="provider_modified", modified_at=updated,
            provider_fields={"updated": stamp},
        ))
    return records


# --------------------------------------------------------------------------- IODA (internet outage events)

IODA_BASE = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/events"
_IODA_MAX_EVENTS = 50
_IODA_ID_RE = re.compile(r"^[a-z]+/[A-Za-z0-9_.-]{1,40}$")


def ioda_request() -> ProviderRequest:
    """Country-level outage events of the past 24 hours (unix ``from``/``until``, computed at call time)."""
    now = datetime.now(UTC)
    start = int((now - timedelta(days=1)).timestamp())
    query = f"from={start}&until={int(now.timestamp())}&entityType=country&limit={_IODA_MAX_EVENTS}"
    return ProviderRequest("GET", f"{IODA_BASE}?{query}")


def map_ioda(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map outage events to one record per (location, start, datasource); ``value`` is the IODA score if present.

    A body carrying a provider ``error`` or no ``data`` list raises ``ProviderPayloadError``. An empty (or
    wholly malformed) list raises ``incomplete`` so the last good page is kept rather than emptied.
    """
    if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("data"), list):
        raise ProviderPayloadError("ioda_response_invalid")
    events = payload["data"]
    if len(events) > _IODA_MAX_EVENTS:
        raise ProviderPayloadError("ioda_event_limit_exceeded", "incomplete")
    records = []
    for event in events:
        if not isinstance(event, dict):
            continue
        location, source = event.get("location"), event.get("datasource")
        if not isinstance(location, str) or not _IODA_ID_RE.fullmatch(location) or not isinstance(source, str):
            continue
        try:
            start = provider_instant(event.get("start"), collected_at, unit="s")
            score = finite_decimal(event["score"]) if event.get("score") is not None else None
        except (ProviderPayloadError, ValueError):
            continue  # one malformed event must not fail the batch
        name = "".join(ch for ch in str(event.get("location_name") or location) if ch.isprintable())[:80]
        duration = event.get("duration")
        minutes = duration // 60 if type(duration) is int and duration >= 0 else None
        stamp = int(start.timestamp())
        method = event.get("method")
        records.append(build_record(
            provider="ioda", identity=f"ioda:{location}:{stamp}:{source}"[:512],
            content=f"Internet outage signal in {name} ({source}), started {start.isoformat()}"
                    + (f", lasting {minutes} min" if minutes is not None else ""),
            title=f"Internet outage: {name}", observed_at=start, collected_at=collected_at,
            version=content_version("ioda", location, stamp, source, minutes, score),
            metric="internet_outage_event", value=score, unit="ioda_score", region=name,
            quality="provider_reported", decimal_key=None,
            provider_fields={
                "location": location, "datasource": source[:40], "start": stamp,
                "duration_s": duration if type(duration) is int else None,
                "method": method[:40] if isinstance(method, str) else None,
            },
        ))
    if not records:
        raise ProviderPayloadError("ioda_no_events", "incomplete")
    return records


# --------------------------------------------------------------------------- NVD (CVE 2.0, keyless, low cadence)

NVD_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_NVD_PAGE = 20  # keyless NVD allows 5 requests / 30 s; one small page per run


def nvd_request() -> ProviderRequest:
    """CVEs published in the past 24 hours, one page (dates are zone-less UTC as NVD requires)."""
    now = datetime.now(UTC)
    fmt = "%Y-%m-%dT%H:%M:%S.000"
    start, end = (now - timedelta(days=1)).strftime(fmt), now.strftime(fmt)
    return ProviderRequest("GET", f"{NVD_BASE}?resultsPerPage={_NVD_PAGE}&pubStartDate={start}&pubEndDate={end}")


def map_nvd(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map one NVD CVE page: ``value`` is the CVSS v3.1 base score when scored (else explicitly missing).

    ``totalResults`` above the page size marks the batch ``truncated``. Rows without a valid CVE id are
    skipped; a body with no ``vulnerabilities`` list raises ``ProviderPayloadError``.
    """
    rows = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(rows, list) or len(rows) > _NVD_PAGE:
        raise ProviderPayloadError("nvd_response_invalid")
    total = payload.get("totalResults")
    coverage: Literal["returned_snapshot", "truncated"] = (
        "truncated" if type(total) is int and total > len(rows) else "returned_snapshot"
    )
    records = []
    for row in rows:
        cve = row.get("cve") if isinstance(row, dict) else None
        cve_id = cve.get("id") if isinstance(cve, dict) else None
        if not isinstance(cve, dict) or not isinstance(cve_id, str) or not _CVE_RE.fullmatch(cve_id):
            continue
        try:
            published = naive_utc_instant(cve.get("published"), collected_at)
        except ProviderPayloadError:
            continue
        modified = cve.get("lastModified") if isinstance(cve.get("lastModified"), str) else None
        status = _text(cve, "vulnStatus", 40)
        description = next(
            (_text(d, "value", 1500) for d in cve.get("descriptions") or [] if isinstance(d, dict) and d.get("lang") == "en"),
            None,
        )
        metrics = cve.get("metrics")
        v31 = metrics.get("cvssMetricV31") if isinstance(metrics, dict) else None
        cvss = v31[0].get("cvssData") if isinstance(v31, list) and v31 and isinstance(v31[0], dict) else None
        cvss = cvss if isinstance(cvss, dict) else {}
        try:
            score = finite_decimal(cvss["baseScore"]) if "baseScore" in cvss else None
        except ValueError:
            score = None
        severity = _text(cvss, "baseSeverity", 16)
        records.append(build_record(
            provider="nvd", identity=cve_id, content="\n".join(p for p in (cve_id, description) if p),
            title=f"{cve_id} {severity or ''}".strip(), observed_at=published, collected_at=collected_at,
            version=content_version("nvd", cve_id, modified, status, score), metric="cve_base_score", value=score,
            unit="cvss", symbol=cve_id, quality="provider_reported", timestamp_basis="provider_published",
            coverage=coverage,
            provider_fields={
                "cveID": cve_id, "status": status, "published": published.isoformat(),
                "lastModified": (modified or "")[:40] or None, "severity": severity,
            },
        ))
    if not records:
        raise ProviderPayloadError("nvd_no_items", "incomplete")
    return records


SECURITY_ADAPTERS: dict[str, PureAdapter] = {
    "cisa_kev": PureAdapter(cisa_kev_request, map_cisa_kev, binary=True),
    "disease_sh": PureAdapter(disease_sh_request, map_disease_sh),
    "ioda": PureAdapter(ioda_request, map_ioda),
    "nvd": PureAdapter(nvd_request, map_nvd),
}
