"""Pure VietnamWorks job-search adapter (unofficial public search endpoint, keyless).

Owner-approved 2026-11-04. ``POST https://ms.vietnamworks.com/job-search/v1.0/search`` carries no contract and may
change or disappear without notice; there is no fallback endpoint. One request per owner keyword (at most five).
The server-side filter parameters are undocumented, so city, level and salary filters are applied here after the
fetch. Only public posting facts become a record: the recruiter contact name, e-mail and the age / gender /
marital / nationality display flags are never read, mapped or stored. The linked posting is a citation and is
never fetched.

ITviec (``itviec_jobs``) is intentionally not implemented: its terms of use restrict retrieval of site content
to "reasonable personal use" without written consent, so shipping a scheduled crawler needs an owner decision.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from modules.connectors.provider_options import DEFAULT_JOB_KEYWORDS
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    content_version,
)
from modules.connectors.providers.news import _record, https_link, parse_when, plain_text
from modules.ingestion.schemas import IngestionRecord

VIETNAMWORKS_API_HOST = "ms.vietnamworks.com"
VIETNAMWORKS_SEARCH_URL = f"https://{VIETNAMWORKS_API_HOST}/job-search/v1.0/search"
VIETNAMWORKS_JOB_HOST = "www.vietnamworks.com"
HITS_PER_PAGE = 50
# Provider ids pinned from live hits on 2026-11-04 (city names Ho Chi Minh / Ha Noi / Da Nang; level names
# Intern/Student, Fresher/Entry level, Experienced (non-manager), Manager, Director and above).
CITY_IDS = {"ho_chi_minh": 29, "ha_noi": 24, "da_nang": 17}
LEVEL_IDS = {"intern": 8, "fresher": 1, "experienced": 5, "manager": 7, "director": 3}
# ponytail: one fixed VND/USD rate for the salary floor; a live rate would add a second provider for a coarse filter.
VND_PER_USD = Decimal(25_000)
_TITLE_MAX, _BODY_MAX = 500, 4000


def vietnamworks_requests(keywords: Sequence[str]) -> list[ProviderRequest]:
    """Build one POST search request per keyword (1-5, each 1-60 chars, no control characters).

    The keyword only ever travels inside a JSON string, so it cannot add URL syntax; length and control
    characters are re-checked because this is the trust boundary to the wire.
    """
    if not 1 <= len(keywords) <= 5 or any(
        not 1 <= len(k) <= 60 or any(ord(c) < 32 for c in k) for k in keywords
    ):
        raise ProviderPayloadError("vietnamworks_scope_mismatch")
    return [
        ProviderRequest(
            "POST", VIETNAMWORKS_SEARCH_URL, {"Accept": "application/json"},
            json.dumps({"query": k, "hitsPerPage": HITS_PER_PAGE, "page": 0}, ensure_ascii=False,
                       separators=(",", ":")).encode(),
        )
        for k in keywords
    ]


def vietnamworks_request() -> ProviderRequest:
    """Return the default-scope request (adapter-table shape); the runner builds the real ones per keyword."""
    return vietnamworks_requests(DEFAULT_JOB_KEYWORDS)[0]


def _salary_max_usd(hit: Mapping[str, Any]) -> Decimal | None:
    """Upper salary bound in USD, or ``None`` when hidden/negotiable (``salaryMax`` 0) or the currency is unknown."""
    top = hit.get("salaryMax")
    if isinstance(top, bool) or not isinstance(top, (int, float)) or top <= 0:
        return None
    if hit.get("salaryCurrency") == "USD":
        return Decimal(str(top))
    if hit.get("salaryCurrency") == "VND":
        return Decimal(str(top)) / VND_PER_USD
    return None


def _kept(hit: Mapping[str, Any], city_ids: set[int], level_ids: set[int], min_salary_usd: int | None) -> bool:
    """Apply the owner's city, level and salary filters; hidden salaries are kept (they cannot be compared)."""
    if city_ids:
        locations = hit.get("workingLocations")
        found = {loc.get("cityId") for loc in locations if isinstance(loc, dict)} if isinstance(locations, list) else set()
        if not found & city_ids:
            return False
    if level_ids and hit.get("jobLevelId") not in level_ids:
        return False
    if min_salary_usd:
        top = _salary_max_usd(hit)
        if top is not None and top < min_salary_usd:
            return False
    return True


def _names(items: object, key: str, limit: int) -> list[str]:
    """Up to ``limit`` non-empty string values of ``key`` from a provider list of objects."""
    if not isinstance(items, list):
        return []
    out = [str(i[key]).strip() for i in items if isinstance(i, dict) and isinstance(i.get(key), str) and i[key].strip()]
    return out[:limit]


def _summary(hit: Mapping[str, Any]) -> str:
    """Public posting facts as short text: company, level, cities, salary line, skills and job function."""
    raw_function = hit.get("jobFunction")
    function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
    parts = [
        ("Company", hit.get("companyName")), ("Level", hit.get("jobLevel")),
        ("Location", ", ".join(_names(hit.get("workingLocations"), "cityName", 5))),
        ("Salary", hit.get("prettySalary")),
        ("Skills", ", ".join(_names(hit.get("skills"), "skillName", 15))),
        ("Function", ", ".join([n for n in (function.get("parentName"), *_names(function.get("children"), "name", 5))
                                 if isinstance(n, str) and n])),
    ]
    return plain_text("; ".join(f"{label}: {value}" for label, value in parts if isinstance(value, str) and value), _BODY_MAX)


def map_vietnamworks(
    payload: object, collected_at: datetime, *, cities: Iterable[str] = (), levels: Iterable[str] = (),
    min_salary_usd: int | None = None,
) -> list[IngestionRecord]:
    """Map one search response to job records, identity ``vietnamworks:{jobId}``, after the owner filters.

    A hit with no integer ``jobId``, no title, or a link outside ``https://www.vietnamworks.com/`` is skipped
    (one malformed posting must not block the rest). Personal and recruiter fields are never read.

    Raises:
        ProviderPayloadError: the body is not ``{"data": [...]}`` (``vietnamworks_response_invalid``).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderPayloadError("vietnamworks_response_invalid")
    city_ids = {CITY_IDS[c] for c in cities if c in CITY_IDS}
    level_ids = {LEVEL_IDS[v] for v in levels if v in LEVEL_IDS}
    records: list[IngestionRecord] = []
    for hit in payload["data"]:
        if not isinstance(hit, dict) or isinstance(hit.get("jobId"), bool) or not isinstance(hit.get("jobId"), int):
            continue
        url = https_link(hit.get("jobUrl"))
        title = plain_text(hit.get("jobTitle") if isinstance(hit.get("jobTitle"), str) else None, _TITLE_MAX)
        if not title or url is None or (parts := urlsplit(url)).hostname != VIETNAMWORKS_JOB_HOST or not parts.path.strip("/"):
            continue
        if not _kept(hit, city_ids, level_ids, min_salary_usd):
            continue
        summary = _summary(hit)
        posted = parse_when(hit.get("approvedOn") if isinstance(hit.get("approvedOn"), str) else None, collected_at)
        updated = hit.get("lastUpdatedOn") if isinstance(hit.get("lastUpdatedOn"), str) else None
        identity = f"vietnamworks:{hit['jobId']}"
        fields: dict[str, Any] = {"title": title, "summary": summary, "canonical_url": url, "publisher": "VietnamWorks"}
        if posted:
            fields["published_at"] = posted.isoformat()
        records.append(_record(
            provider="vietnamworks_jobs", identity=identity, title=title, summary=summary,
            observed_at=posted or collected_at, collected_at=collected_at,
            version=content_version("vietnamworks_jobs", hit["jobId"], title, summary, hit.get("expiredOn")),
            basis="provider_published" if posted else "collection", modified_at=None,
            coverage="returned_snapshot", source_fields=fields, canonical_url=url,
            published_at=posted.isoformat() if posted else None,
            provider_updated_at=updated[:64] if updated else None,
        ))
    return records


def merge_keyword_records(batches: Iterable[Sequence[IngestionRecord]]) -> list[IngestionRecord]:
    """Concatenate per-keyword results, keeping the first record of every ``jobId`` (keywords overlap heavily)."""
    merged: dict[str, IngestionRecord] = {}
    for batch in batches:
        for record in batch:
            merged.setdefault(record.provider_id, record)
    return list(merged.values())


JOBS_ADAPTERS: dict[str, PureAdapter] = {"vietnamworks_jobs": PureAdapter(vietnamworks_request, map_vietnamworks)}
