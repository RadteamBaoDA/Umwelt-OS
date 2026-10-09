# Free data sources

Catalog facts for the 14 free-provider presets, taken from `modules/connectors/provider_specs.py` (checked on 2026-10-07; policy revision 1). `Code` shows whether a collection adapter is registered in the shared executor: `registered` means present in code only, `existing adapter` is the earlier Alpha Vantage collector, and `not registered` means no adapter exists. 
Endpoint availability, implemented code, legal eligibility and live acceptance are separate facts: a row here is **not** a license grant and **no** provider is runtime-verified. Evidence and quotes live in `docs/free-data-provider-policy.md`.

## Rules

- All presets are opt-in and start disabled. No paid fallback and no automatic account or key creation.
- The workspace owner declares deployment use (personal / noncommercial / commercial / unknown) and acknowledges a terms version per source. Activation, manual, scheduled and retry collection are denied with `409 provider_terms_ineligible` before any network call unless the declared use fits the eligibility class.
- `open`: any declared use, attribution and dataset obligations remain. `noncommercial`: personal or noncommercial. `personal`: personal only. `review`: always denied until an instance operator records approval (terms URL/version, allowed use, evidence reference) for the exact acknowledged terms version; an owner tick cannot approve.
- Changing declared use or terms version bumps `terms_revision` and invalidates earlier review evidence. The collection request captures `terms_revision`.
- Quota is a durable PostgreSQL ledger (`connector_quota_windows`, `connector_provider_sends`, `connector_quota_debits`) shared by scheduled and manual collection and by both backends. One physical send debits every applicable window atomically before transmission; exhaustion defers with no provider I/O. Unknown official caps are recorded as counted windows with no enforced limit (never treated as unlimited). Local pilot ceilings are configurable internal choices, not provider promises.

## Catalog

| ID | Host | Kind | Interval (min) | Eligibility | Key | Execution | Code | Terms |
|---|---|---|---|---|---|---|---|---|
| `bbc_world` | feeds.bbci.co.uk | news | 30 | review | none | supported | registered | [terms](https://www.bbc.co.uk/usingthebbc/terms/) |
| `vnexpress_business` | vnexpress.net | news | 30 | noncommercial | none | supported | registered | [terms](https://vnexpress.net/rss) |
| `google_news` | news.google.com | news | 30 | review | none | supported | registered | [terms](https://policies.google.com/terms) |
| `hn_top` | hacker-news.firebaseio.com | news | 60 | review | none | supported | registered | [terms](https://github.com/HackerNews/API) |
| `gdelt_economy` | api.gdeltproject.org | news | 60 | review | none | experimental | registered | [terms](https://gdeltproject.org/about.html) |
| `world_bank` | api.worldbank.org | measurement | 1440 | open | none | supported | registered | [terms](https://datacatalog.worldbank.org/public-licenses) |
| `frankfurter` | api.frankfurter.dev | measurement | 1440 | open | none | supported | registered | [terms](https://frankfurter.dev/license/) |
| `ecb` | www.ecb.europa.eu | measurement | 1440 | open | none | supported | registered | [terms](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html) |
| `binance` | data-api.binance.vision | measurement | 15 | review | none | supported | registered | [terms](https://developers.binance.com/en/docs/products/spot/rest-api) |
| `alternative_me` | api.alternative.me | measurement | 1440 | open | none | supported | registered | [terms](https://alternative.me/crypto/fear-and-greed-index/) |
| `usgs` | earthquake.usgs.gov | event | 60 | open | none | supported | registered | [terms](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) |
| `coinpaprika` | api.coinpaprika.com | measurement | 15 | personal | none | supported | registered | [terms](https://docs.coinpaprika.com/api-plans) |
| `coingecko` | api.coingecko.com | measurement | 15 | review | required | supported | registered (requires API key) | [terms](https://www.coingecko.com/en/api_terms) |
| `alpha_vantage` | www.alphavantage.co | measurement | 1440 | personal | required | supported | existing adapter | [terms](https://www.alphavantage.co/terms_of_service/) |

## Attribution

- `bbc_world`: Display BBC as publisher with a link to the original article; feed-only title, summary and link.
- `vnexpress_business`: Identify VnExpress clearly and link the original article; feed-only scope.
- `google_news`: Google News RSS / publisher: show the item's publisher (from its `<source>` element) and link to the original article; feed-only title, summary and link.
- `hn_top`: Attribute Hacker News plus the item and publisher URL; the API license does not license linked articles.
- `gdelt_economy`: Cite the GDELT Project and the publisher URL; article metadata is not republication permission.
- `world_bank`: Credit the World Bank and the original indicator; retain the dataset license and changed notice.
- `frankfurter`: Credit Frankfurter and contributing providers; the default rate is a blended rate, not a single-bank rate.
- `ecb`: Credit the ECB; preserve the rate date and identify transformations.
- `binance`: Credit Binance; BTCUSDT market, USDT quote. No trading endpoints or keys.
- `alternative_me`: Display Alternative.me with a link adjacent to the index, including translated output.
- `usgs`: Credit the U.S. Geological Survey with the event citation; do not fetch detail URLs automatically.
- `coinpaprika`: Credit CoinPaprika; free use is personal with no redistribution.
- `coingecko`: Show Powered by CoinGecko with a link per the API terms and brand guide.
- `alpha_vantage`: Credit Alpha Vantage with symbol and day; no free realtime entitlement.

## Quota windows

| ID | Policy key | Budget | Window | Unit | Limit | Cost/send | Basis |
|---|---|---|---|---|---|---|---|
| `bbc_world` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `bbc_world` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `vnexpress_business` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `vnexpress_business` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `google_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `google_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `hn_top` | `pilot_calls_utc_day` | provider | day | http_calls | 528 | 1 | pilot_local |
| `hn_top` | `pilot_calls_utc_minute` | provider | minute | http_calls | 12 | 1 | pilot_local |
| `gdelt_economy` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `gdelt_economy` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `world_bank` | `pilot_calls_utc_day` | provider | day | http_calls | 32 | 1 | pilot_local |
| `world_bank` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `frankfurter` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `frankfurter` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `ecb` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `ecb` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `binance` | `pilot_request_weight_utc_day` | provider | day | request_weight | 384 | 2 | pilot_local |
| `binance` | `pilot_request_weight_utc_minute` | provider | minute | request_weight | 4 | 2 | pilot_local |
| `binance` | `official_request_weight_unknown` | ip | minute | request_weight | counted, no cap | 2 | official_unknown |
| `alternative_me` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `alternative_me` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `usgs` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `usgs` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `coinpaprika` | `pilot_calls_utc_day` | provider | day | http_calls | 192 | 1 | pilot_local |
| `coinpaprika` | `pilot_calls_utc_second` | provider | second | http_calls | 1 | 1 | pilot_local |
| `coinpaprika` | `official_calls_per_second` | ip | second | http_calls | 10 | 1 | official_documented |
| `coinpaprika` | `official_calls_per_month` | ip | month | http_calls | 20000 | 1 | official_documented |
| `coingecko` | `pilot_calls_utc_day` | credential | day | http_calls | 192 | 1 | pilot_local |
| `coingecko` | `pilot_calls_utc_second` | credential | second | http_calls | 1 | 1 | pilot_local |
| `coingecko` | `official_calls_per_minute` | credential | minute | http_calls | 100 | 1 | official_documented |
| `coingecko` | `official_call_credits_per_month` | credential | month | call_credits | 10000 | 1 | official_documented |
| `alpha_vantage` | `pilot_calls_utc_day` | credential | day | http_calls | 12 | 1 | pilot_local |
| `alpha_vantage` | `pilot_calls_utc_minute` | credential | minute | http_calls | 1 | 1 | pilot_local |
| `alpha_vantage` | `official_calls_per_day` | credential | day | http_calls | 25 | 1 | official_documented |

Alpha Vantage enforces 12 sends per UTC day locally (official 25/day is recorded separately) until the provider reset anchor is evidenced.

## Setup

1. Choose the preset; the source starts disabled. Fixed presets have no scope fields; the only configurable field is the schedule (`schedule_interval_minutes`).
2. Owner declares deployment use and acknowledges the terms version with `PUT /api/v1/connectors/{source_id}/terms`, body `{"declared_use": "personal" | "noncommercial" | "commercial" | "unknown", "terms_version": "<version>"}`. `GET /api/v1/connectors/{source_id}/terms` shows the current state. Owner only.
3. `review` providers: an instance operator records the terms review for the exact acknowledged version (state, allowed use, evidence reference). An owner tick cannot approve.
4. Key providers: Alpha Vantage and CoinGecko. Supply your own free-plan key; it is encrypted server-side (`CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`) and only a deployment-keyed fingerprint is used for the shared budget. CoinGecko takes its Demo key through `PUT /api/v1/connectors/{source_id}/credentials/native-rest` (body `{"expected_revision": <n>, "secret": "<key>"}`, native backend, owner only). The key is write-only: it is stored encrypted, sent only in the `x-cg-demo-api-key` header, and never returned, logged or placed in a URL. The source read reports only `provider_credential_configured` (boolean). Without a ready key, activation answers `invalid_credential` and a collection fails closed with `credential_missing` and no network call. Re-entering the key bumps the credential revision and cancels in-flight work.
5. Stock data for Vietnam stays unavailable; no unofficial Yahoo/SSI endpoint is used.

## Per-provider setup

Each entry gives what the source collects, prerequisites, cadence, quota and attribution. Quotas are the pilot ceilings above, not provider promises. Official caps that are not documented are recorded as counted but not enforced.

- **`bbc_world`**: BBC World News RSS feed (`/news/world/rss.xml`). Title, summary and link only. No key. Cadence 30 min. Attribution: show BBC as publisher with a link to the original article. Needs operator terms review.
- **`vnexpress_business`**: VnExpress business RSS feed (`/rss/kinh-doanh.rss`). Title, summary and link only. No key. Cadence 30 min. Attribution: identify VnExpress and link the original article. Noncommercial use only.
- **`google_news`**: Google News RSS search feed (`news.google.com/rss/search`). Implemented, requires terms acknowledgement (class `review`: an operator must record approval first) and is **not runtime verified**. This is an unofficial RSS endpoint: Google publishes no API contract, quota or stability guarantee for it, and the feed can change or stop without notice. The owner never supplies a URL. The server builds it from validated scope only: `news_query` (1-200 characters, control characters stripped, no `site:` operator), `news_site` (`any`, `reuters.com`, `apnews.com`, `bbc.com`, `vnexpress.net`) and `news_locale` (`en-US` or `vi-VN`, mapped to `hl`/`gl`/`ceid`). Title, summary and link only; each item carries `publisher` from its `<source>` element and the licence label "Google News RSS / publisher". The item link is Google's article link; the original publisher article is not fetched. Cadence 30 min and the 96/day pilot ceiling are the same local choices as the other news feeds (see the quota table in `docs/free-data-provider-policy.md`); no official quota is claimed.
- **`hn_top`**: Hacker News top stories. One list call plus at most 10 item calls per run (11 sends). No key. Cadence 60 min. Attribution: Hacker News with the item and publisher URL. Needs operator terms review.
- **`gdelt_economy`**: GDELT DOC 2.0 economy article list, 5 records, 24-hour window. Experimental. No key. Cadence 60 min. Attribution: cite the GDELT Project and the publisher URL. Needs operator terms review.
- **`world_bank`**: Annual GDP (current US$) for Vietnam, `NY.GDP.MKTP.CD`, up to 3 rows per response. No key. Cadence 1440 min. Attribution: credit the World Bank and the indicator; keep the dataset licence. Coverage is marked `truncated` when the response spans more than one page.
- **`frankfurter`**: USD/VND blended reference rate from `/v2/rate/usd/vnd`. No key. Cadence 1440 min. Attribution: credit Frankfurter and its contributing providers. The rate is a blend, not a single bank's rate. The v2 response shape is an unverified assumption.
- **`ecb`**: ECB euro reference rates, `eurofxref-daily.xml`. No key. Cadence 1440 min. Attribution: credit the ECB and keep the rate date. Each EUR cross rate in the feed becomes one record; no USD/VND or other cross rate is synthesised.
- **`binance`**: BTCUSDT last price from `data-api.binance.vision`. No key and no trading endpoints. Cadence 15 min. The quote is USDT, not USD. Attribution: credit Binance. Needs operator terms review.
- **`alternative_me`**: Fear and Greed index, last 2 points. No key. Cadence 1440 min. Attribution: show Alternative.me with a link next to the index, including translated output.
- **`usgs`**: USGS M4.5+ earthquakes, past day, GeoJSON summary. No key. Cadence 60 min. Detail URLs are never fetched. Attribution: credit the U.S. Geological Survey and cite the event.
- **`coinpaprika`**: BTC ticker, `btc-bitcoin`. No key. Cadence 15 min. Personal use only, with no redistribution. Attribution: credit CoinPaprika.
- **`alpha_vantage`**: Daily equity OHLCV (raw, as traded), `TIME_SERIES_DAILY`. Key required. Cadence 1440 min. Quota: 12 sends per UTC day locally, with the official 25/day recorded separately. Personal use only under the catalog class. Example configuration (confirm symbols and exchange before activation):

  ```json
  {
    "market_symbols": ["IBM"],
    "market_currency": "USD",
    "market_exchange_timezone": "America/New_York",
    "schedule_interval_minutes": 1440
  }
  ```

  Up to five symbols per source. Currency and exchange timezone are explicit because the response does not establish them. The response is daily, not realtime.
- **`coingecko`**: registered, requires an owner-supplied Demo API key (see Setup step 4). One `simple/price` call for bitcoin/usd per run. Code presence only; not runtime verified.

Not verified for any provider in this document: test connection output, sample records, and last-successful-collection behaviour. These need a runtime check.

Recovery: error payloads, including ones returned with HTTP 200, are rejected by each adapter and create no records. A rate-limited run does not publish an empty series.

## Data caveats

- **Feed-only news.** `bbc_world`, `vnexpress_business`, `google_news`, `hn_top` and `gdelt_economy` store title, summary, link and publisher. Linked article full text is not fetched and is not available. Summary text is capped at 4,000 characters and marked truncated when longer.
- **Publisher and licence label.** Each news record carries `publisher` (for example "BBC News") and `license_label`, which is the catalog attribution text. Both are meant to be shown beside the data. Whether the UI shows them is a P4-web item and is not verified here.
- **Frankfurter.** The rate date is the rate period, not a release instant. The v2 shape (`rate` field) is an unverified assumption until a live response is checked.
- **World Bank coverage.** Annual data with up to 3 rows. Coverage is `truncated` when more than one page is returned; exact continuation paging is not implemented.
- **Freshness (target rule, not yet verified in the UI).** The UI should show observed, published and collected times separately. A feed or quote is stale after two polling intervals. FX reference dates are kept over non-business days and are not presented as live. A failed source should show the last good value and the error, never a current-looking empty price.
- **Stock gaps.** Alpha Vantage EOD is conditional on the key and entitlement. SEC filings, FRED and TwelveData are research only (see [provider-research-2026-10-07.md](provider-research-2026-10-07.md)). Vietnam stock quotes are unavailable until an endpoint and its terms are verified. Manual file import is the only fallback offered; no paid fallback is added.
