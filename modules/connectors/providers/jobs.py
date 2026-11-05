"""Pure VietnamWorks job-search adapter (unofficial public search endpoint, keyless).

Owner-approved 2026-11-04. ``POST https://ms.vietnamworks.com/job-search/v1.0/search`` carries no contract and may
change or disappear without notice; there is no fallback endpoint. One request per owner keyword (at most five).
The server-side filter parameters are undocumented, so city, level and salary filters are applied here after the
fetch. Only public posting facts become a record: the recruiter contact name, e-mail and the age / gender /
marital / nationality display flags are never read, mapped or stored. The linked posting is a citation and is
never fetched.

ITviec (``itviec_jobs``) is a polite static crawl built on an explicit owner decision (2026-11-04): ITviec's
terms (section 2.2) limit retrieval of site content to "reasonable personal use" without written consent, and the
owner accepts that risk. ``robots.txt`` allows crawling. Only schema.org JSON-LD is parsed, never CSS selectors.
"""

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from modules.connectors.provider_options import DEFAULT_JOB_KEYWORDS, MAX_ITVIEC_SEARCHES
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    content_version,
)
from modules.connectors.providers.news import _record, https_link, parse_when, plain_text
from modules.ingestion.public import IngestionRecord

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


# ---------------------------------------------------------------- ITviec (static JSON-LD crawl)

ITVIEC_HOST = "itviec.com"
ITVIEC_JOBS_PATH = "/it-jobs/"
ITVIEC_MAX_DETAIL_FETCHES = 10  # detail pages per run (the runner enforces it)
ITVIEC_REQUEST_GAP_SECONDS = 2.0  # minimum spacing between any two ITviec requests
# City URL slugs verified live 2026-11-04 (each returned a distinct, non-empty result list).
ITVIEC_CITY_SLUGS = {"ho_chi_minh": "ho-chi-minh-hcm", "ha_noi": "ha-noi", "da_nang": "da-nang"}
# Best-effort level matching on free-text ``experienceRequirements``; postings without that text are kept.
_ITVIEC_LEVEL_WORDS = {
    "intern": ("intern", "thực tập", "thuc tap"), "fresher": ("fresher", "entry", "graduate", "mới tốt nghiệp"),
    "experienced": ("experienced", "middle", "mid-level", "senior", "năm kinh nghiệm", "years"),
    "manager": ("manager", "lead", "quản lý"), "director": ("director", "head of", "cto", "giám đốc"),
}
_LD_JSON = re.compile(r"<script[^>]*type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SLUG_PART = re.compile(r"[^a-z0-9]+")


def itviec_keyword_slug(keyword: str) -> str:
    """URL slug for a search keyword: accents folded, lower-case ASCII words joined by ``-`` (``c#`` becomes ``c``).

    Raises:
        ProviderPayloadError: nothing slug-able remains (``itviec_scope_mismatch``), so no request is built.
    """
    folded = unicodedata.normalize("NFKD", keyword.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode().lower()
    slug = _SLUG_PART.sub("-", folded).strip("-")
    if not slug:
        raise ProviderPayloadError("itviec_scope_mismatch")
    return slug


def itviec_search_requests(keywords: Sequence[str], cities: Sequence[str] = ()) -> list[ProviderRequest]:
    """One GET per keyword x city search page (no city = nationwide page), at most ``MAX_ITVIEC_SEARCHES``.

    Slugs come from a fixed city table and a sanitised keyword, so nothing owner-typed adds URL syntax.

    Raises:
        ProviderPayloadError: too many combinations, an unknown city, or an unusable keyword
        (``itviec_scope_mismatch``).
    """
    slugs: list[str | None] = [ITVIEC_CITY_SLUGS[c] for c in cities if c in ITVIEC_CITY_SLUGS]
    if len(slugs) != len(cities) or not keywords or len(keywords) * max(1, len(slugs)) > MAX_ITVIEC_SEARCHES:
        raise ProviderPayloadError("itviec_scope_mismatch")
    return [
        ProviderRequest("GET", f"https://{ITVIEC_HOST}{ITVIEC_JOBS_PATH}{itviec_keyword_slug(k)}" + (f"/{c}" if c else ""),
                        {"Accept": "text/html"})
        for k in keywords for c in (slugs or [None])
    ]


def itviec_request() -> ProviderRequest:
    """Default-scope search request (adapter-table shape); the runner builds the real ones."""
    return itviec_search_requests(DEFAULT_JOB_KEYWORDS)[0]


def itviec_detail_request(url: str) -> ProviderRequest:
    """GET for one posting URL already vetted by ``itviec_job_url``."""
    return ProviderRequest("GET", url, {"Accept": "text/html"})


def itviec_job_url(raw: object) -> str | None:
    """Canonical posting URL, or ``None`` unless it is ``https://itviec.com/it-jobs/{slug}`` (one path segment).

    Query and fragment are dropped, so a hostile ItemList cannot steer the crawler to another host, path or
    parameterised page. Search pages (``/it-jobs/python/ha-noi``) have two segments and are rejected too.
    """
    link = https_link(raw)
    if link is None:
        return None
    parts = urlsplit(link)
    slug = parts.path[len(ITVIEC_JOBS_PATH):] if parts.path.startswith(ITVIEC_JOBS_PATH) else ""
    if parts.hostname != ITVIEC_HOST or parts.port not in (None, 443) or not slug or "/" in slug:
        return None
    return f"https://{ITVIEC_HOST}{ITVIEC_JOBS_PATH}{slug}"


def _ld_objects(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object in a page (flattening arrays and ``@graph``); malformed blocks are skipped."""
    found: list[dict[str, Any]] = []
    for block in _LD_JSON.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            found.extend(g for g in graph if isinstance(g, dict)) if isinstance(graph, list) else found.append(item)
    return found


def parse_itviec_item_list(html: str) -> list[str]:
    """Posting URLs from a search page's schema.org ``ItemList``, vetted by ``itviec_job_url``, deduplicated in order."""
    urls: dict[str, None] = {}
    for obj in _ld_objects(html):
        if obj.get("@type") != "ItemList" or not isinstance(obj.get("itemListElement"), list):
            continue
        for entry in obj["itemListElement"]:
            if isinstance(entry, dict) and (url := itviec_job_url(entry.get("url"))):
                urls.setdefault(url)
    return list(urls)


def _level_ok(posting: Mapping[str, Any], levels: set[str]) -> bool:
    """Best-effort level filter: keep a posting without experience text; otherwise any chosen level's word must match."""
    requirement = posting.get("experienceRequirements")
    text = " ".join(str(v) for v in requirement.values()) if isinstance(requirement, dict) else requirement
    if not levels or not isinstance(text, str) or not text.strip():
        return True
    lowered = text.casefold()
    return any(word in lowered for level in levels for word in _ITVIEC_LEVEL_WORDS[level])


def _itviec_salary(posting: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
    """``(max salary in USD or None, display text or None)`` from ``baseSalary``; hidden salaries give ``(None, None)``."""
    base = posting.get("baseSalary")
    if not isinstance(base, dict) or not isinstance(base.get("value"), dict):
        return None, None
    value = base["value"]
    text = value.get("value") if isinstance(value.get("value"), str) else None
    return _salary_max_usd({"salaryMax": value.get("maxValue"), "salaryCurrency": base.get("currency")}), text


def _itviec_city(posting: Mapping[str, Any]) -> str:
    """City only (``addressRegion``): street, district and postal code are never stored."""
    places = posting.get("jobLocation")
    cities = {
        loc["address"]["addressRegion"].strip() for loc in (places if isinstance(places, list) else [places])
        if isinstance(loc, dict) and isinstance(loc.get("address"), dict)
        and isinstance(loc["address"].get("addressRegion"), str) and loc["address"]["addressRegion"].strip()
    }
    return ", ".join(sorted(cities)[:3])


def map_itviec(
    pages: Iterable[tuple[str, str]], collected_at: datetime, *, levels: Iterable[str] = (), min_salary_usd: int | None = None,
) -> list[IngestionRecord]:
    """Map fetched posting pages ``(url, html)`` to job records, identity ``itviec:{slug}``, after the owner filters.

    A page without a ``JobPosting`` with a title, or whose URL fails ``itviec_job_url``, is skipped (one
    malformed page must not block the rest). The posting body (description, benefits, logo, apply link) is
    never read: the record is a citation summary of public facts. A hidden salary is kept and marked.
    """
    wanted = set(levels)
    records: list[IngestionRecord] = []
    for page_url, html in pages:
        url = itviec_job_url(page_url)
        posting = next((o for o in _ld_objects(html) if o.get("@type") == "JobPosting"), None) if url else None
        title = plain_text(posting.get("title") if posting and isinstance(posting.get("title"), str) else None, _TITLE_MAX)
        if url is None or posting is None or not title or not _level_ok(posting, wanted):
            continue
        top, salary_text = _itviec_salary(posting)
        if min_salary_usd and top is not None and top < min_salary_usd:
            continue
        org = posting.get("hiringOrganization")
        raw_when = posting.get("datePosted") if isinstance(posting.get("datePosted"), str) else None
        if raw_when and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_when):
            raw_when += "T00:00:00+07:00"  # date-only in Vietnam local time; parse_when needs an explicit zone
        posted = parse_when(raw_when, collected_at)
        valid = posting.get("validThrough") if isinstance(posting.get("validThrough"), str) else None
        experience = posting.get("experienceRequirements")
        parts = [
            ("Company", org.get("name") if isinstance(org, dict) else None), ("Type", posting.get("employmentType")),
            ("Location", _itviec_city(posting)), ("Salary", salary_text or "not disclosed"),
            ("Skills", posting.get("skills")), ("Experience", experience if isinstance(experience, str) else None),
            ("Valid through", valid[:10] if valid else None),
        ]
        summary = plain_text("; ".join(f"{k}: {v}" for k, v in parts if isinstance(v, str) and v), _BODY_MAX)
        identity = f"itviec:{url.rsplit('/', 1)[1]}"
        fields: dict[str, Any] = {"title": title, "summary": summary, "canonical_url": url, "publisher": "ITviec"}
        if posted:
            fields["published_at"] = posted.isoformat()
        records.append(_record(
            provider="itviec_jobs", identity=identity, title=title, summary=summary,
            observed_at=posted or collected_at, collected_at=collected_at,
            version=content_version("itviec_jobs", identity, title, summary, valid),
            basis="provider_published" if posted else "collection", modified_at=None,
            coverage="returned_snapshot", source_fields=fields, canonical_url=url,
            published_at=posted.isoformat() if posted else None,
        ))
    return records


def merge_keyword_records(batches: Iterable[Sequence[IngestionRecord]]) -> list[IngestionRecord]:
    """Concatenate per-keyword results, keeping the first record of every ``jobId`` (keywords overlap heavily)."""
    merged: dict[str, IngestionRecord] = {}
    for batch in batches:
        for record in batch:
            merged.setdefault(record.provider_id, record)
    return list(merged.values())


JOBS_ADAPTERS: dict[str, PureAdapter] = {"vietnamworks_jobs": PureAdapter(vietnamworks_request, map_vietnamworks)}  # itviec_jobs: multi-page runner in collection.py
