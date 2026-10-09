# Free data sources

Catalog facts for the 13 free-provider presets, generated from `modules/connectors/provider_specs.py` (checked on 2026-10-07; policy revision 1). 
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
| `bbc_world` | feeds.bbci.co.uk | news | 30 | review | none | supported | pending | [terms](https://www.bbc.co.uk/usingthebbc/terms/) |
| `vnexpress_business` | vnexpress.net | news | 30 | noncommercial | none | supported | pending | [terms](https://vnexpress.net/rss) |
| `hn_top` | hacker-news.firebaseio.com | news | 60 | review | none | supported | pending | [terms](https://github.com/HackerNews/API) |
| `gdelt_economy` | api.gdeltproject.org | news | 60 | review | none | experimental | pending | [terms](https://gdeltproject.org/about.html) |
| `world_bank` | api.worldbank.org | measurement | 1440 | open | none | supported | pending | [terms](https://datacatalog.worldbank.org/public-licenses) |
| `frankfurter` | api.frankfurter.dev | measurement | 1440 | open | none | supported | pending | [terms](https://frankfurter.dev/license/) |
| `ecb` | www.ecb.europa.eu | measurement | 1440 | open | none | supported | pending | [terms](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html) |
| `binance` | data-api.binance.vision | measurement | 15 | review | none | supported | pending | [terms](https://developers.binance.com/en/docs/products/spot/rest-api) |
| `alternative_me` | api.alternative.me | measurement | 1440 | open | none | supported | pending | [terms](https://alternative.me/crypto/fear-and-greed-index/) |
| `usgs` | earthquake.usgs.gov | event | 60 | open | none | supported | pending | [terms](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) |
| `coinpaprika` | api.coinpaprika.com | measurement | 15 | personal | none | supported | pending | [terms](https://docs.coinpaprika.com/api-plans) |
| `coingecko` | api.coingecko.com | measurement | 15 | review | required | supported | pending | [terms](https://www.coingecko.com/en/api_terms) |
| `alpha_vantage` | www.alphavantage.co | measurement | 1440 | personal | required | supported | existing adapter | [terms](https://www.alphavantage.co/terms_of_service/) |

## Attribution

- `bbc_world`: Display BBC as publisher with a link to the original article; feed-only title, summary and link.
- `vnexpress_business`: Identify VnExpress clearly and link the original article; feed-only scope.
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

1. Choose the preset; the source starts disabled.
2. Owner declares deployment use and acknowledges the terms version shown in the catalog entry.
3. `review` providers: ask the instance operator to record the terms review for that version.
4. Key providers (CoinGecko Demo, Alpha Vantage): supply your own free-plan key; it is stored server-side and only a deployment-keyed fingerprint is used for the shared budget.
5. Stock data for Vietnam stays unavailable; no unofficial Yahoo/SSI endpoint is used.
