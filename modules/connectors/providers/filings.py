"""Pure SEC EDGAR recent-filings adapter (``data.sec.gov/submissions/CIK##########.json``).

No network and no database. The URL is built here from a validated 10-digit CIK; SEC fair-access rules
require a declared ``User-Agent`` with contact details, so a missing/invalid owner contact email fails
closed (``provider_scope_invalid``) before any send. One record per recent filing: identity is the
accession number, ``observed_at`` is the filing date (UTC midnight). Filing documents are cited by URL and
never fetched.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    content_version,
    day_start,
    parse_iso_date,
)
from modules.connectors.providers.news import _record, plain_text
from modules.ingestion.schemas import IngestionRecord

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
MAX_FILINGS = 100  # `filings.recent` holds up to ~1000 rows, newest first; more is reported as truncated
CIK_RE = re.compile(r"^[0-9]{10}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_DOCUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_FORM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /.-]{0,31}$")


def sec_edgar_request(cik: object, contact_email: object) -> ProviderRequest:
    """Build the submissions GET with the declared ``User-Agent``; invalid scope raises ``provider_scope_invalid``."""
    if not isinstance(cik, str) or not CIK_RE.fullmatch(cik):
        raise ProviderPayloadError("provider_scope_invalid")
    if not isinstance(contact_email, str) or not _EMAIL_RE.fullmatch(contact_email):
        raise ProviderPayloadError("provider_scope_invalid")
    return ProviderRequest(
        "GET", SUBMISSIONS_URL.format(cik=cik),
        {"User-Agent": f"Umwelt-OS {contact_email}", "Accept": "application/json"},
    )


def _column(recent: dict[str, Any], name: str, size: int | None) -> list[Any]:
    """One columnar array of ``filings.recent``; its length must match the accession column."""
    values = recent.get(name)
    if not isinstance(values, list) or (size is not None and len(values) != size):
        raise ProviderPayloadError("sec_edgar_response_invalid")
    return values


def map_sec_edgar(payload: object, collected_at: datetime, cik: str) -> list[IngestionRecord]:
    """Map ``filings.recent`` (columnar arrays) for the requested CIK; the payload CIK must match the scope."""
    if not isinstance(payload, dict) or not CIK_RE.fullmatch(cik):
        raise ProviderPayloadError("sec_edgar_response_invalid")
    payload_cik = payload.get("cik")
    if not isinstance(payload_cik, (str, int)) or str(payload_cik).lstrip("0") != cik.lstrip("0"):
        raise ProviderPayloadError("sec_edgar_scope_mismatch")
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise ProviderPayloadError("sec_edgar_response_invalid")
    accessions = _column(recent, "accessionNumber", None)
    size = len(accessions)
    forms, dates = _column(recent, "form", size), _column(recent, "filingDate", size)
    documents, descriptions = _column(recent, "primaryDocument", size), _column(recent, "primaryDocDescription", size)
    if size == 0:
        raise ProviderPayloadError("sec_edgar_no_filings", "incomplete")
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if size > MAX_FILINGS else "returned_snapshot"
    cik_number = str(int(cik))  # Archives paths use the CIK without leading zeros
    records: dict[str, IngestionRecord] = {}
    for accession, form, filed, document, description in list(zip(
        accessions, forms, dates, documents, descriptions, strict=True,
    ))[:MAX_FILINGS]:
        if not isinstance(accession, str) or not _ACCESSION_RE.fullmatch(accession) or accession in records:
            raise ProviderPayloadError("sec_edgar_accession_invalid")
        if not isinstance(form, str) or not _FORM_RE.fullmatch(form):
            raise ProviderPayloadError("sec_edgar_form_invalid")
        filed_on = day_start(parse_iso_date(filed))
        if filed_on > collected_at:
            raise ProviderPayloadError("sec_edgar_date_invalid")
        summary = plain_text(description if isinstance(description, str) else None, 500)
        url = ARCHIVES_URL.format(
            cik=cik_number, accession=accession.replace("-", ""), document=document,
        ) if isinstance(document, str) and _DOCUMENT_RE.fullmatch(document) else None
        filed_iso = filed_on.astimezone(UTC).isoformat()
        fields: dict[str, Any] = {
            "title": form, "guid": accession, "form": form, "cik": cik, "published_at": filed_iso,
        }
        if summary:
            fields["summary"] = summary
        if url:
            fields["canonical_url"] = url
        records[accession] = _record(
            provider="sec_edgar", identity=accession, title=form, summary=summary, observed_at=filed_on,
            collected_at=collected_at, version=content_version("sec_edgar", cik, accession, form, filed, summary, url),
            basis="provider_published", modified_at=None, coverage=coverage, source_fields=fields,
            canonical_url=url, published_at=filed_iso,
        )
    return list(records.values())
