# Free Data Providers và Setup Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Có danh mục và collector thực tế cho tin, vĩ mô, tỷ giá, crypto, thiên tai; trình bày trung thực phần chứng khoán chưa được nguồn miễn phí phù hợp bao phủ.
**Architecture:** RSS reuse reader hiện có, structured adapters trả typed IngestionRecord/ProviderRecordMetadata; Observation owner lưu số liệu. Provider catalog quản lý schema, fixed hosts, lịch, quota/terms và setup.
**Tech Stack:** Existing Python/httpx, PostgreSQL observations, connector registry, native scheduler, source editor.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md); [master](2026-10-07-production-readiness-master.md).

## Global Constraints

- Không paid fallback hoặc tự động đăng ký tài khoản/key.
- Free endpoint không tự chứng minh redistributable/commercial data license; không coi library license là data license.
- Preset opt-in; provider hạn chế personal/noncommercial không được bật khi deployment use unknown/incompatible.
- Không dùng unofficial Yahoo/SSI endpoints chưa được xác minh; chứng khoán VN giữ unavailable.
- V1 không thêm service RSSHub/OpenBB/CCXT; chỉ tham khảo.
- Test trong V3; production-code tasks chỉ build/review.

## Review Focus

- JSON 200 chứa quota/error hoặc HTML response: không tạo bản ghi rỗng/zero — P1/P3/V3.
- Năm GDP, ngày FX, quote collection time bị nhầm với event time — P3/V3.
- USDT được ghi nhầm USD, price stale được gọi realtime — P3/V3.
- XML entity expansion/feed link SSRF/script injection — P2/V3.
- Terms thay đổi hoặc free key hết quota: disable/defer rõ, không tự đổi paid provider — P1/P4/V3.

## P1 — Provider catalog, eligibility và typed metadata

**Files**
- Create: `modules/connectors/provider_specs.py`, `docs/connectors/free-sources.md`.
- Modify: `modules/connectors/catalog.py`, `registry.py`, `public.py`, `modules/knowledge/documents/schemas.py`, `modules/knowledge/observations/schemas.py`.
- Modify settings/connector configuration DTO for deployment use-case and provider terms acknowledgement, following workspace ownership.
- Deferred tests: `tests/unit/modules/connectors/test_free_provider_catalog.py`, `tests/unit/modules/connectors/test_provider_contracts.py`.

**Contract**
```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class FreeProviderSpec:
    """Catalog facts and execution bounds; availability is not a license grant."""
    id: str
    hosts: tuple[str, ...]
    data_kind: Literal["news", "measurement", "event"]
    key_required: bool
    default_interval_minutes: int
    terms_url: str
    eligibility: Literal["open", "personal", "noncommercial", "review"]
    attribution: str
    execution: Literal["supported", "experimental", "research_only"]
```
Store provider source/terms URL and checked_on in catalog output/documentation; don't turn arbitrary declared usage into legal proof. Additive catalog API exposes setup guide, key fields, plan limits, endpoint/status evidence, backend capability.

Catalog eligibility mặc định: World Bank/Frankfurter/ECB/USGS/Alternative.me = open (giữ dataset/attribution obligations); VnExpress = noncommercial; Alpha Vantage/CoinPaprika = personal; BBC/Binance/CoinGecko/HN/GDELT = review đến khi operator ghi nhận terms phù hợp deployment. noncommercial cho phép personal hoặc noncommercial, personal chỉ personal; unknown không vượt qua hai loại này. review đòi owner/operator ghi terms URL/version + allowed deployment use-case, không phải chỉ tick một checkbox không có bằng chứng. Tất cả preset vẫn opt-in. CoinGecko Demo chỉ cấu hình khi có gói free đủ điều kiện; cap theo tài liệu được chụp lúc setup và HTTP response, không coi keyless probe là SLA.

- [ ] **Step 1 — catalog data.** Populate exact provider scope from matrix below; separate endpoint probe, code implementation, eligibility and live acceptance fields. No synthetic PASS when response hasn't traversed ingestion.
- [ ] **Step 2 — eligibility.** Workspace owner selects deployment use personal/noncommercial/commercial/unknown and acknowledges provider terms version. open still requires dataset-specific attribution; review/unknown blocked until operator records reviewed use-case. Incompatible choice returns 409 provider_terms_ineligible before network. No terms evaluation by LLM.
- [ ] **Step 3 — extend trusted types.** Add selected structured provider IDs to ProviderRecordMetadata, WorldDataMeasurement, provider source_fields allowlists and connector config validators. Keep reserved metadata inaccessible to generic REST/client ReceiveBatch. Registry and typed-schema IDs have contract test equality for supported providers.
- [ ] **Step 4 — observations.** Macro/year and FX/day preserve period in provider_fields and actual collection clock; no fabricated publication timestamp. Measurement fields retain unit/currency/symbol/timezone/quality/missing_reason. Structured observations go through existing owner public APIs and receipts, not direct SQL from connector module.
- [ ] **Step 5 — quotas.** Add durable provider/credential quota ledger to p14_collection revision owned by C2; Redis is an optimization. Hash credential fingerprint for shared free-key budgets, never key in metrics. Both scheduled/manual debit same ledger; provider-side limits remain authoritative. No global batch cache containing private configuration.
- [ ] **Step 6 — build/review.** Verify catalog readiness and source activation use same eligibility check, including REST presets and retries.

**Deferred test**
```python
import pytest

@pytest.mark.parametrize("deployment_use", ["commercial", "unknown"])
def test_personal_provider_cannot_activate_for_incompatible_use(deployment_use):
    from modules.connectors.provider_specs import deployment_use_matches
    assert not deployment_use_matches("personal", deployment_use)
```
Define `FREE_PROVIDER_SPECS: tuple[FreeProviderSpec, ...]` and `deployment_use_matches(eligibility: str, deployment_use: str) -> bool` in provider_specs.py. The helper returns true for open, exact personal, or noncommercial with personal/noncommercial; false for unknown use on restricted providers and always false for review (separate persisted operator review required). V3 verifies activation rejects before httpx transport is invoked, including manual/retry paths.

## P2 — RSS/news presets và optional discovery feeds

**Files**
- Modify: `modules/connectors/providers/feed_catalog.py`, native RSS helper from C3, `modules/connectors/catalog.py`.
- Create: `modules/connectors/providers/gdelt.py` only for experimental bounded GDELT query adapter.
- Modify: `apps/web/src/modules/sources/provider-scope.tsx`, source messages; `docs/connectors/free-sources.md`.
- Deferred tests: `tests/unit/modules/connectors/test_public_news_feeds.py`.

**Exact source presets**

| ID | Endpoint | Default | Eligibility/normalization |
| --- | --- | --- | --- |
| bbc_world | https://feeds.bbci.co.uk/news/world/rss.xml | 30 min | Feed usage review; title/summary/link, don't fetch full article automatically |
| vnexpress_business | https://vnexpress.net/rss/kinh-doanh.rss | 30 min | personal/noncommercial matching publisher terms; source attribution |
| hn_top | https://hacker-news.firebaseio.com/v0/topstories.json | 60 min | First 10 IDs/run; GET /v0/item/{id}.json; removed/dead items skipped with count |
| gdelt_economy | https://api.gdeltproject.org/api/v2/doc/doc?query=economy&mode=artlist&format=json&maxrecords=5&timespan=24h | 60 min, experimental | Timeout observed; bounded query, URL/seen-date metadata, no claim of full news coverage |

- [ ] **Step 1 — RSS presets.** Store provider preset feed URLs in catalog; selecting preset pre-fills source config, requires explicit owner Save & enable. ETag/cursor bound to URL/config revision; RSSHub not installed.
- [ ] **Step 2 — parser.** Preserve raw origin URL and parsed timezone-aware dates. Reject DOCTYPE/entities, cap response/item counts/bytes, sanitize HTML for display. Unknown date uses collection basis explicitly, stable ID/version excludes volatile collected_at.
- [ ] **Step 3 — HN bounded adapter.** Use same rest transport bounds/fixed host, fetch first10 IDs sequentially within collector admission, skip missing/deleted items. Article URL is citation, not automatic crawl target. Never imply rights over linked full article.
- [ ] **Step 4 — GDELT.** Separate experimental flag, 8 s request timeout, max50 records/run if configured within limit; on timeout keep last good and expose next retry. No proxy rotation to evade limits; no fallback scraping Reuters/AP.
- [ ] **Step 5 — provenance.** Stable provider identity from feed GUID/canonical URL or HN ID, content hash drives version; preserve publication vs collection timestamps. Changing order must not create duplicate News stories.
- [ ] **Step 6 — build/review.** Source setup previews returned fields and terms. UI says “Google News RSS” for any future Reuters/AP search feed rather than official news agency API.

**Deferred parser fixture expectation**
```json
{"input":{"guid":"story-42","title":"Market &amp; policy","pubDate":"Wed, 07 Oct 2026 08:00:00 GMT"},"expected":{"provider_id":"story-42","title":"Market & policy","observed_at":"2026-10-07T08:00:00Z"}}
```
V3 adds repeated feed/reordered feed/304/missing date/hostile XML fixture tests; clock changes alone do not change versions.

## P3 — Macro, FX, crypto và disaster adapters

**Files**
- Create: `modules/connectors/providers/macro.py`, `crypto.py`, `disasters.py`.
- Modify: `modules/connectors/providers/world_data.py`, `provider_specs.py`, collection dispatch, Documents/Observations schema validators.
- Deferred tests: `tests/unit/modules/connectors/test_macro_providers.py`, `test_crypto_providers.py`, `test_disaster_providers.py`; fixture JSON/XML under `tests/fixtures/providers/`.

**Mapping interfaces**
- `map_world_bank(payload: object, collected_at: datetime) -> list[IngestionRecord]`.
- `map_frankfurter(payload: object, collected_at: datetime) -> list[IngestionRecord]`.
- `map_ecb(xml: bytes, collected_at: datetime) -> list[IngestionRecord]`.
- `map_binance(payload: object, collected_at: datetime) -> list[IngestionRecord]`.
- `map_fear_greed(payload: object, collected_at: datetime) -> list[IngestionRecord]`.
- `map_usgs(payload: object, collected_at: datetime) -> list[IngestionRecord]`.
Functions live in named provider file, pure mapping with no network; execution service handles fetch/auth/limits.

| Provider | Fixed endpoint/scope | Mapping và cadence |
| --- | --- | --- |
| World Bank | https://api.worldbank.org/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3 | country VN, GDP/current USD, annual period; 1440 min |
| Frankfurter | https://api.frankfurter.dev/v2/rate/usd/vnd | base USD/quote VND, rate/date/providers attribution; 1440 min |
| ECB | https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml | EUR reference rates, provider date; 1440 min; no VND claim |
| Binance | https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT | symbol BTCUSDT, quote unit USDT, collection timestamp basis; 15 min |
| Alternative.me | https://api.alternative.me/fng/?limit=2 | sentiment numeric value/index 0–100, provider timestamp; 1440 min |
| USGS | https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson | event ID/time/update, magnitude/place/coordinates, 60 min |
| CoinPaprika | https://api.coinpaprika.com/v1/tickers/btc-bitcoin | Optional personal-use; USD quote/update timestamp; 15 min |
| CoinGecko | https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd | Optional terms-reviewed Demo config/key; explicit quota; no anonymous SLA |
| Alpha Vantage | https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&apikey=YOUR_KEY | Existing adapter fixed by C1, EOD ≤5 symbols/25 calls per key/day; 1440 min |

- [ ] **Step 1 — shared parse policy.** Finite numeric only, no booleans masquerading as numbers; preserve Decimal lexical precision until schema's documented numeric conversion. Missing values become explicit missing, never zero. Do not serialize API keys in source_fields/citation URLs.
```python
from decimal import Decimal, InvalidOperation

def finite_decimal(raw: object) -> Decimal:
    """Parse a provider number without silently accepting booleans or NaN."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError("provider_measurement_invalid")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("provider_measurement_invalid") from exc
    if not value.is_finite():
        raise ValueError("provider_measurement_invalid")
    return value
```
Place in macro.py for these mapping helpers and export via connector-local utility if used by crypto; don't introduce a global utility package.
- [ ] **Step 2 — stable identities.** World Bank country+indicator+year; FX base+quote+provider-date+provider; Fear&Greed timestamp; USGS event ID/version update. Binance latest-only quote timestamp absent: content version from symbol+price; observed_at=collected_at, timestamp_basis=collection, no invented exchange clock.
- [ ] **Step 3 — mapping.** Build typed provider_record envelope and existing observation record per actual returned value/event. Macro record period is annual, not a daily measurement; null most-recent GDP does not overwrite earlier valid annual points.
- [ ] **Step 4 — provider errors.** Detect Alpha Note/Information/Error Message even HTTP200, GDELT malformed payload, CoinGecko/CoinPaprika limits, Binance error objects, World Bank pagination metadata and ECB absent date. Fail bounded request or mark incomplete coverage, don't emit invented empty market series.
- [ ] **Step 5 — optional crypto.** Implement CoinPaprika/CoinGecko adapters behind catalog eligibility and explicit owner selection, not automatic fallback between differing licenses. Same currency/unit/time contract; no silent BTCUSDT→BTCUSD substitution.
- [ ] **Step 6 — build/review.** Verify World Bank GDP as annual, FX as reference rate, Binance exchange price, Fear&Greed sentiment, USGS event. News ingestion excludes standalone periodic market ticks; dashboard observation gadgets render them.

**Deferred test**
```python
from datetime import UTC, datetime
from modules.connectors.providers.macro import map_world_bank

def test_world_bank_null_is_not_zero():
    payload = [{"page": 1, "pages": 1}, [{
        "indicator": {"id": "NY.GDP.MKTP.CD", "value": "GDP"},
        "country": {"id": "VN", "value": "Viet Nam"},
        "countryiso3code": "VNM", "date": "2025", "value": None
    }]]
    records = map_world_bank(payload, datetime(2026, 10, 7, tzinfo=UTC))
    assert records[0].metadata["provider_record"]["world_data"]["value"] is None
    assert records[0].metadata["provider_record"]["world_data"]["quality"] == "missing"
```
P3 must define missing_reason=provider_null. Provider period goes provider_fields["period"]="2025"; no claim of actual publication date.

## P4 — Setup guides, catalog UI và verified source inventory

**Files**
- Modify: `docs/connectors/provider-catalog.md`, `docs/connectors/free-sources.md`, `docs/connectors.md`, `README.md`.
- Modify: `apps/web/src/modules/sources/provider-scope.tsx`, `connector-editor.tsx`, `api.ts`, message catalogs.
- Create: `docs/connectors/provider-research-2026-10-07.md`.
- Deferred tests: `tests/e2e/free-source-setup.spec.ts`, V3 live smoke.

- [ ] **Step 1 — inventory.** Record implemented vs planned vs disabled; links below; retain checked date and probe limitation. Use actual provider eligibility, not “all endpoints public therefore free for teams”.
- [ ] **Step 2 — owner setup.** Each source: what data/period, free/key prerequisites, complete example configuration, cadence, quota, attribution, test connection, sample output, activate/last-success, recovery. Provider key stored encrypted server-side, masked/never echoed after save.
- [ ] **Step 3 — stock gaps.** Alpha EOD conditional; SEC filings research-only, not quotes; FRED free key/TwelveData free tier research-only; VN stock quote unavailable until endpoint+terms verified. Offer manual file import already supported, no premium fallback.
- [ ] **Step 4 — freshness.** UI shows observed/published/collected time separately; stale after 2 polling intervals for feeds/quotes, FX reference date retained over non-business days without claiming live, annual macro period prominent. Source failure shows last-good and error, never claims empty price is current.
- [ ] **Step 5 — attribution.** Link publisher beside News; Alternative.me attribution immediately beside index; retain provider notices and upstream FX attribution. Translation preserves these blocks.
- [ ] **Step 6 — build/review.** Setup text matches current API/UI and actual compose commands; docs don't say activation UI is pending.

## Research evidence và primary references

Public GET probes on 2026-10-07: BBC, VnExpress, World Bank, Frankfurter, ECB, USGS, HN, Alternative.me, CoinPaprika, Binance and keyless CoinGecko returned HTTP200 with expected JSON/XML family. GDELT timed out at8s. These results are not schema/runtime/licensing acceptance.

- https://github.com/koala73/worldmonitor — inspect src/config/feeds.ts; scripts/seed-crypto-quotes.mjs; scripts/_seed-utils.mjs; scripts/seed-market-quotes.mjs; economic World Bank route; data-sources docs.
- https://github.com/public-apis/public-apis — discovery registry; auth/free claims need current provider docs.
- https://github.com/DIYgod/RSSHub ; https://github.com/ccxt/ccxt ; https://github.com/openbq-org/OpenBB ; https://github.com/FreshRSS/FreshRSS ; https://github.com/thinh-vu/vnstock .
- https://frankfurter.dev/ and https://frankfurter.dev/license/ — public API commercial free, provider terms apply.
- https://datahelpdesk.worldbank.org/knowledgebase/articles/889392 and https://datacatalog.worldbank.org/public-licenses .
- https://vnexpress.net/rss — personal/nonprofit and attribution.
- https://alternative.me/crypto/fear-and-greed-index/ — API and attribution/commercial conditions.
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints .
- https://coinpaprika.com/api/pricing/ ; https://www.coingecko.com/en/api/pricing .
- https://www.alphavantage.co/support/ — current free request cap; license separately.
- https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php ; https://github.com/HackerNews/API .
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces ; https://fred.stlouisfed.org/docs/api/fred/ ; https://twelvedata.com/pricing ; https://developers.ssi.com.vn/docs/getting-started/overview .

World Monitor endpoints/ideas are references, not permission to copy AGPL implementation into this repo. Record any code actually ported in OSS notices during implementation.
