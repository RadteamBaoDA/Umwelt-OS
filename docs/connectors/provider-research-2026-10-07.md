# Provider research, 2026-10-07

Evidence behind the free-provider presets in [free-sources.md](free-sources.md) and [provider-catalog.md](provider-catalog.md). This note records what was observed and what was not. It is not schema acceptance, runtime verification, a licence grant or a commercial-use decision.

## Status

- Runtime verification: **none**. Every catalog row reports `runtime_verified = no`.
- Endpoint probes are `plan_documented`: the endpoint comes from the plan, not from a verified live response.
- The 2026-10-07 probes below were public unauthenticated GET requests only.
- No quota or commercial term in this note is taken from a provider page read in this slice. Pilot ceilings are local choices in `modules/connectors/provider_specs.py`.

## Public GET probes, 2026-10-07

| Provider | Result | What it does not show |
|---|---|---|
| BBC News | HTTP 200, RSS | Item schema, licence, rate limits |
| VnExpress | HTTP 200, RSS | Permitted scope beyond the feed; noncommercial terms |
| World Bank | HTTP 200, JSON | Pagination behaviour beyond the first page |
| Frankfurter | HTTP 200, JSON | The v2 response shape is still an assumption; see below |
| ECB | HTTP 200, XML | Parser behaviour on the live document |
| USGS | HTTP 200, GeoJSON | Feature schema as mapped |
| Hacker News | HTTP 200, JSON | Story scope and redistribution (the API licence does not license linked articles) |
| Alternative.me | HTTP 200, JSON | Attribution wording in translated output |
| CoinPaprika | HTTP 200, JSON | Free-use scope (personal only) |
| Binance | HTTP 200, JSON | Quote currency is USDT, not USD |
| CoinGecko (keyless) | HTTP 200, JSON | Says nothing about the Demo key path. CoinGecko is not registered. |
| GDELT | Timed out at 8 s | Whether the endpoint is usable; the preset stays `experimental` |

A 200 response is a reachability observation only. It does not validate the mapped fields, quotas or terms.

## Assumptions and open items

- **Frankfurter v2.** The adapter reads a `rate` field from `/v2/rate/usd/vnd`. This shape is unverified against a live response.
- **World Bank paging.** Coverage is marked `truncated` when more than one page is returned. Continuation paging is not implemented.
- **GDELT.** The 2026-10-07 probe timed out. Re-probe before any runtime work.
- **CoinGecko Demo.** No adapter and no credential slot exist. Adding it needs a key flow and a terms check.
- **Operator terms review.** `review`-class presets (BBC, Hacker News, GDELT, Binance) stay denied until an operator records approval for the exact terms version.
- **Alpha Vantage.** The personal-use grant and the entitlement for daily EOD still need to be confirmed for any deployment. The local budget is 12 sends per UTC day; the official 25/day cap is recorded separately.
- **Sample output and last-success behaviour** for every provider need a runtime check.

## Stock and securities gaps

These do not provide free quotes for this product. None of them has an adapter.

- **Alpha Vantage EOD.** Conditional. Usable only with an owner key and an entitlement that fits the deployment. Not realtime.
- **SEC EDGAR filings.** Research only. Filing metadata is not a price quote.
- **FRED.** Research only. A free key does not establish redistribution or commercial use.
- **TwelveData free tier.** Research only. Free-tier limits and terms were not verified in this slice.
- **Vietnam stock quotes.** Unavailable until an endpoint and its terms are verified. No unofficial Yahoo or SSI endpoint is used.
- **Manual file import** is the only fallback. No paid fallback is added.

## Primary references listed by the research brief

These were recorded as links to check. They were not re-read in this slice.

- Frankfurter: https://frankfurter.dev/ and https://frankfurter.dev/license/
- World Bank: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392 and https://datacatalog.worldbank.org/public-licenses
- VnExpress RSS: https://vnexpress.net/rss
- Alternative.me: https://alternative.me/crypto/fear-and-greed-index/
- Binance market data: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
- CoinPaprika pricing: https://coinpaprika.com/api/pricing/
- CoinGecko pricing: https://www.coingecko.com/en/api/pricing
- Alpha Vantage support: https://www.alphavantage.co/support/
- USGS feeds: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
- Hacker News API: https://github.com/HackerNews/API
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- FRED API: https://fred.stlouisfed.org/docs/api/fred/
- TwelveData pricing: https://twelvedata.com/pricing
- SSI developer docs: https://developers.ssi.com.vn/docs/getting-started/overview

Earlier slices recorded these reads, which this note does not repeat: Alpha Vantage Terms of Service read 2026-10-05 (personal-use grant only absent a written agreement); Open-Meteo schema and terms inspected 2026-10-05.

## Reference projects

These were consulted for endpoint ideas only. No code from them is ported by this documentation slice.

- World Monitor (`koala73/worldmonitor`, AGPL): feed lists, crypto quote seeding, World Bank route. Reference only; no AGPL implementation is copied here.
- Public APIs registry, RSSHub, CCXT, OpenBB, FreshRSS, vnstock: discovery or comparison only. V1 adds no RSSHub, OpenBB or CCXT service.

Any code later ported from a reference must be recorded in the third-party notices at that time.
