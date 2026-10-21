# Free data sources

Catalog facts for the 50 free-provider presets, taken from `modules/connectors/provider_specs.py` (original 21 checked on 2026-10-07, the built-in expansion on 2026-10-21; policy revision 1). `Code` shows whether a collection adapter is registered in the shared executor: `registered` means present in code only, `existing adapter` is the earlier Alpha Vantage collector, and `not registered` means no adapter exists. 
Endpoint availability, implemented code, legal eligibility and live acceptance are separate facts: a row here is **not** a license grant and **no** provider is runtime-verified. Evidence and quotes live in `docs/free-data-provider-policy.md`.

## Rules

- All presets are opt-in and start disabled. No paid fallback and no automatic account or key creation.
- The workspace owner declares deployment use (personal / noncommercial / commercial / unknown) and acknowledges a terms version per source. Activation, manual, scheduled and retry collection are denied with `409 provider_terms_ineligible` before any network call unless the declared use fits the eligibility class.
- `open`: any declared use, attribution and dataset obligations remain. `noncommercial`: personal or noncommercial. `personal`: personal only. `review`: always denied until an instance operator records approval (terms URL/version, allowed use, evidence reference) for the exact acknowledged terms version; an owner tick cannot approve.
- Changing declared use or terms version bumps `terms_revision` and invalidates earlier review evidence. The collection request captures `terms_revision`.
- Quota is a durable PostgreSQL ledger (`connector_quota_windows`, `connector_provider_sends`, `connector_quota_debits`) shared by scheduled and manual collection and by both backends. One physical send debits every applicable window atomically before transmission; exhaustion defers with no provider I/O. Unknown official caps are recorded as counted windows with no enforced limit (never treated as unlimited). Local pilot ceilings are configurable internal choices, not provider promises.

- **Built-in toggle (single-user instance).** Fixed-scope, keyless presets with an adapter are listed under **Settings → Data sources → Built-in sources** and are enabled with one switch. The first enable creates the source lazily (no seed migration) and, in one idempotent sequence, records the owner's terms acknowledgement and declared use, then activates it; later toggles only pause or resume the same source. For `review` providers the owner is also the instance operator, so the same enable records operator review evidence (`evidence_ref = builtin-toggle:<provider>:<terms_url>@<terms_version>`) and clears the collection block. Only the bootstrap operator may enable a `review` preset; anyone else gets `403 operator_required`. A declared use the class does not admit still returns `409 provider_terms_ineligible`. Scoped and keyed presets are never built-in: they stay in the manual flow with an in-app guide.
- **Category** groups the catalog and the built-in list: `news`, `economy`, `hazards`, `space`, `security`, `tech`.
- **Keyed presets** store the owner key in `connector_rest_credentials`; `header_name` records where it travels: a header name, `query:<param>` or `path:<token>`. The request is built server-side, redacted from `repr`, and a missing key fails closed before the lease and the send.

## Catalog

| ID | Host | Kind | Interval (min) | Eligibility | Key | Execution | Code | Terms | Category |
|---|---|---|---|---|---|---|---|---|---|
| `bbc_world` | feeds.bbci.co.uk | news | 30 | review | none | supported | registered | [terms](https://www.bbc.co.uk/usingthebbc/terms/) | news |
| `vnexpress_business` | vnexpress.net | news | 30 | noncommercial | none | supported | registered | [terms](https://vnexpress.net/rss) | news |
| `tuoitre_news` | tuoitre.vn | news | 30 | noncommercial | none | supported | registered | [terms](https://tuoitre.vn/rss.htm) | news |
| `thanhnien_news` | thanhnien.vn | news | 30 | noncommercial | none | supported | registered | [terms](https://thanhnien.vn/rss.html) | news |
| `dantri_news` | dantri.com.vn | news | 30 | noncommercial | none | supported | registered | [terms](https://dantri.com.vn/rss.htm) | news |
| `google_news` | news.google.com | news | 30 | review | none | supported | registered | [terms](https://policies.google.com/terms) | news |
| `sec_edgar` | data.sec.gov | event | 360 | open | none | supported | registered | [terms](https://www.sec.gov/privacy.htm#security) | economy |
| `openalex` | api.openalex.org | event | 1440 | open | none | supported | registered | [terms](https://help.openalex.org/how-to-use-the-api/api-overview) | tech |
| `hn_top` | hacker-news.firebaseio.com | news | 60 | review | none | supported | registered | [terms](https://github.com/HackerNews/API) | tech |
| `gdelt_economy` | api.gdeltproject.org | news | 60 | review | none | experimental | registered | [terms](https://gdeltproject.org/about.html) | news |
| `world_bank` | api.worldbank.org | measurement | 1440 | open | none | supported | registered | [terms](https://datacatalog.worldbank.org/public-licenses) | economy |
| `frankfurter` | api.frankfurter.dev | measurement | 1440 | open | none | supported | registered | [terms](https://frankfurter.dev/license/) | economy |
| `ecb` | www.ecb.europa.eu | measurement | 1440 | open | none | supported | registered | [terms](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html) | economy |
| `binance` | data-api.binance.vision | measurement | 15 | review | none | supported | registered | [terms](https://developers.binance.com/en/docs/products/spot/rest-api) | economy |
| `alternative_me` | api.alternative.me | measurement | 1440 | open | none | supported | registered | [terms](https://alternative.me/crypto/fear-and-greed-index/) | economy |
| `usgs` | earthquake.usgs.gov | event | 60 | open | none | supported | registered | [terms](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) | hazards |
| `nasa_eonet` | eonet.gsfc.nasa.gov | event | 60 | review | none | supported | registered | [terms](https://eonet.gsfc.nasa.gov/what-is-eonet#disclaimer) | hazards |
| `cisa_kev` | www.cisa.gov | event | 1440 | open | none | supported | registered | [terms](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) | security |
| `coinpaprika` | api.coinpaprika.com | measurement | 15 | personal | none | supported | registered | [terms](https://docs.coinpaprika.com/api-plans) | economy |
| `coingecko` | api.coingecko.com | measurement | 15 | review | required | supported | registered (requires API key) | [terms](https://www.coingecko.com/en/api_terms) | economy |
| `alpha_vantage` | www.alphavantage.co | measurement | 1440 | personal | required | supported | existing adapter | [terms](https://www.alphavantage.co/terms_of_service/) | economy |
| `aljazeera_world` | www.aljazeera.com | news | 30 | review | none | supported | registered | [terms](https://www.aljazeera.com/terms-and-conditions) | news |
| `npr_news` | feeds.npr.org | news | 30 | noncommercial | none | supported | registered | [terms](https://www.npr.org/about-npr/179876898/terms-of-use) | news |
| `fed_press` | www.federalreserve.gov | news | 60 | open | none | supported | registered | [terms](https://www.federalreserve.gov/disclaimer.htm) | economy |
| `gdacs_rss` | www.gdacs.org | event | 60 | open | none | supported | registered | [terms](https://www.gdacs.org/About/overview.aspx) | hazards |
| `lobsters` | lobste.rs | news | 60 | review | none | supported | registered | [terms](https://lobste.rs/about) | tech |
| `us_travel_advisories` | travel.state.gov | event | 360 | open | none | supported | registered | [terms](https://www.state.gov/copyright-information/) | hazards |
| `noaa_tsunami` | www.tsunami.gov | event | 30 | open | none | supported | registered | [terms](https://www.tsunami.gov/) | hazards |
| `jma_eqvol` | www.data.jma.go.jp | event | 30 | open | none | supported | registered | [terms](https://www.jma.go.jp/jma/en/copyright.html) | hazards |
| `gdacs_events` | www.gdacs.org | event | 60 | open | none | supported | registered | [terms](https://www.gdacs.org/About/overview.aspx) | hazards |
| `nws_alerts` | api.weather.gov | event | 15 | open | none | supported | registered | [terms](https://www.weather.gov/disclaimer) | hazards |
| `emsc_quakes` | www.seismicportal.eu | event | 30 | open | none | supported | registered | [terms](https://www.emsc-csem.org/) | hazards |
| `launch_library` | ll.thespacedevs.com | event | 120 | noncommercial | none | supported | registered | [terms](https://thespacedevs.com/llapi) | space |
| `wikipedia_mostread` | api.wikimedia.org | news | 1440 | open | none | supported | registered | [terms](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | tech |
| `wikipedia_top_views` | wikimedia.org | news | 1440 | open | none | supported | registered | [terms](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | tech |
| `bmkg_quakes` | data.bmkg.go.id | event | 30 | open | none | supported | registered | [terms](https://data.bmkg.go.id/) | hazards |
| `satnogs_observations` | network.satnogs.org | event | 120 | open | none | supported | registered | [terms](https://db.satnogs.org/about) | space |
| `noaa_kp` | services.swpc.noaa.gov | measurement | 180 | open | none | supported | registered | [terms](https://www.weather.gov/disclaimer) | space |
| `open_er_api` | open.er-api.com | measurement | 1440 | review | none | supported | registered | [terms](https://www.exchangerate-api.com/terms) | economy |
| `oecd_cli` | sdmx.oecd.org | measurement | 1440 | open | none | supported | registered | [terms](https://www.oecd.org/en/about/terms-conditions.html) | economy |
| `iss_position` | api.wheretheiss.at | measurement | 15 | noncommercial | none | supported | registered | [terms](https://wheretheiss.at/w/developer) | space |
| `disease_sh` | disease.sh | measurement | 1440 | review | none | supported | registered | [terms](https://disease.sh/) | tech |
| `ioda` | api.ioda.inetintel.cc.gatech.edu | event | 60 | noncommercial | none | supported | registered | [terms](https://ioda.inetintel.cc.gatech.edu/) | security |
| `nvd` | services.nvd.nist.gov | event | 360 | open | none | supported | registered | [terms](https://nvd.nist.gov/general/FAQ-Sections/General-FAQs) | security |
| `fred` | api.stlouisfed.org | measurement | 1440 | review | required | supported | registered (requires API key) | [terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) | economy |
| `finnhub_news` | finnhub.io | news | 60 | personal | required | supported | registered (requires API key) | [terms](https://finnhub.io/terms-of-service) | news |
| `newsapi` | newsapi.org | news | 120 | review | required | supported | registered (requires API key) | [terms](https://newsapi.org/terms) | news |
| `openweathermap` | api.openweathermap.org | measurement | 60 | open | required | supported | registered (requires API key) | [terms](https://openweathermap.org/api) | hazards |
| `openaq` | api.openaq.org | measurement | 60 | open | required | supported | registered (requires API key) | [terms](https://docs.openaq.org/about/about) | hazards |
| `nasa_firms` | firms.modaps.eosdis.nasa.gov | event | 180 | open | required | supported | registered (requires API key) | [terms](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies) | hazards |

## Attribution

- `bbc_world`: Display BBC as publisher with a link to the original article; feed-only title, summary and link.
- `vnexpress_business`: Identify VnExpress clearly and link the original article; feed-only scope.
- `tuoitre_news`: Display Tuổi Trẻ clearly as source and link the original article; feed-only scope. Free for individuals and non-profits, must credit the source; the publisher may revoke at any time and retains copyright.
- `thanhnien_news`: Credit "Báo Thanh Niên" or "Theo Báo Thanh Niên" and link the original article; feed-only scope. Terms require credit and prohibit copying without written consent.
- `dantri_news`: Credit Dân trí as the source and link the original article; feed-only scope.
- `google_news`: Google News RSS / publisher: show the item's publisher (from its `<source>` element) and link to the original article; feed-only title, summary and link.
- `openalex`: Credit OpenAlex; data is CC0. Link the work's OpenAlex id and DOI; abstracts are rebuilt from the inverted index.
- `hn_top`: Attribute Hacker News plus the item and publisher URL; the API license does not license linked articles.
- `gdelt_economy`: Cite the GDELT Project and the publisher URL; article metadata is not republication permission.
- `world_bank`: Credit the World Bank and the original indicator; retain the dataset license and changed notice.
- `frankfurter`: Credit Frankfurter and contributing providers; the default rate is a blended rate, not a single-bank rate.
- `ecb`: Credit the ECB; preserve the rate date and identify transformations.
- `binance`: Credit Binance; BTCUSDT market, USDT quote. No trading endpoints or keys.
- `alternative_me`: Display Alternative.me with a link adjacent to the index, including translated output.
- `usgs`: Credit the U.S. Geological Survey with the event citation; do not fetch detail URLs automatically.
- `nasa_eonet`: Credit NASA EONET and the listed event sources; EONET states its data is for visualization and general information only and its extents are approximations. Source and event links are stored as text and never fetched.
- `cisa_kev`: Credit CISA Known Exploited Vulnerabilities Catalog (CC0 data repository `cisagov/kev-data`).
- `coinpaprika`: Credit CoinPaprika; free use is personal with no redistribution.
- `coingecko`: Show Powered by CoinGecko with a link per the API terms and brand guide.
- `alpha_vantage`: Credit Alpha Vantage with symbol and day; no free realtime entitlement.
- `sec_edgar`: Source: U.S. SEC EDGAR
- `aljazeera_world`: Credit Al Jazeera and link the original article; feed-only title, summary and link.
- `npr_news`: Credit NPR and link the original story; RSS is licensed for personal, noncommercial use.
- `fed_press`: Credit the Board of Governors of the Federal Reserve System; link the original release.
- `gdacs_rss`: Credit GDACS (UN OCHA / European Commission JRC) and link the event page.
- `lobsters`: Credit Lobsters and link the story; the feed carries submitter links and titles only.
- `us_travel_advisories`: Credit the U.S. Department of State; advisories are a point-in-time statement, check the original.
- `noaa_tsunami`: Credit the NOAA/NWS Tsunami Warning Centers; this is not an official warning channel.
- `jma_eqvol`: Source: Japan Meteorological Agency; state any processing (feed title and link only).
- `gdacs_events`: Credit GDACS (UN OCHA / European Commission JRC) and link the event report.
- `nws_alerts`: Credit the U.S. National Weather Service; alerts expire, check the original.
- `emsc_quakes`: Credit EMSC (Euro-Mediterranean Seismological Centre); magnitudes are preliminary.
- `launch_library`: Credit The Space Devs Launch Library; free tier is non-commercial with a 15 requests/hour limit.
- `wikipedia_mostread`: Credit Wikipedia (CC BY-SA 4.0) and link the article; counts are for the previous UTC day.
- `wikipedia_top_views`: Credit Wikimedia pageviews (CC0 data) and link the article; counts are for the previous UTC day.
- `bmkg_quakes`: Source: BMKG (Badan Meteorologi, Klimatologi, dan Geofisika); state any processing.
- `satnogs_observations`: Credit SatNOGS Network (CC BY-SA 4.0); station observations are community-contributed.
- `noaa_kp`: Credit NOAA Space Weather Prediction Center; Kp is a 3-hour planetary index (0-9).
- `open_er_api`: Rates By Exchange Rate API (https://www.exchangerate-api.com); attribution is required on the free plan.
- `oecd_cli`: Source: OECD (CC BY 4.0); cite the dataset OECD.SDD.STES DF_CLI and state any processing.
- `iss_position`: Credit wheretheiss.at; the API is free for non-commercial use with a documented ~1 request/second cap.
- `disease_sh`: Credit disease.sh and its upstream sources (Johns Hopkins CSSE and others); totals may be stale.
- `ioda`: Credit the Georgia Tech IODA project; the service is a research tool, terms are not a license grant.
- `nvd`: Credit the NIST National Vulnerability Database; this product uses NVD data but is not endorsed by NIST.
- `fred`: This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis; some series carry third-party copyright.
- `finnhub_news`: Credit Finnhub and link the original article; the free plan is for personal use.
- `newsapi`: Credit NewsAPI.org and the article's publisher; the free Developer plan is for development use only.
- `openweathermap`: Weather data provided by OpenWeather (CC BY-SA 4.0); state any processing.
- `openaq`: Credit OpenAQ and the originating data provider; data licenses vary per provider (mostly CC BY 4.0).
- `nasa_firms`: Credit NASA FIRMS (LANCE/EOSDIS); hotspots are detections, not confirmed fires.

## Quota windows

| ID | Policy key | Budget | Window | Unit | Limit | Cost/send | Basis |
|---|---|---|---|---|---|---|---|
| `bbc_world` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `bbc_world` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `vnexpress_business` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `thanhnien_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `thanhnien_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `vnexpress_business` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `tuoitre_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `dantri_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `tuoitre_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `dantri_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `google_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `google_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `openalex` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `openalex` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
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
| `nasa_eonet` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `cisa_kev` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `nasa_eonet` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `cisa_kev` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
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
| `sec_edgar` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `sec_edgar` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `aljazeera_world` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `aljazeera_world` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `npr_news` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `npr_news` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `fed_press` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `fed_press` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `gdacs_rss` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `gdacs_rss` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `lobsters` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `lobsters` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `us_travel_advisories` | `pilot_calls_utc_day` | provider | day | http_calls | 12 | 1 | pilot_local |
| `us_travel_advisories` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `noaa_tsunami` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `noaa_tsunami` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `jma_eqvol` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `jma_eqvol` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `gdacs_events` | `pilot_calls_utc_day` | provider | day | http_calls | 48 | 1 | pilot_local |
| `gdacs_events` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `nws_alerts` | `pilot_calls_utc_day` | provider | day | http_calls | 192 | 1 | pilot_local |
| `nws_alerts` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `emsc_quakes` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `emsc_quakes` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `launch_library` | `pilot_calls_utc_day` | provider | day | http_calls | 12 | 1 | pilot_local |
| `launch_library` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `wikipedia_mostread` | `pilot_calls_utc_day` | provider | day | http_calls | 4 | 1 | pilot_local |
| `wikipedia_mostread` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `wikipedia_top_views` | `pilot_calls_utc_day` | provider | day | http_calls | 4 | 1 | pilot_local |
| `wikipedia_top_views` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `bmkg_quakes` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `bmkg_quakes` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `satnogs_observations` | `pilot_calls_utc_day` | provider | day | http_calls | 12 | 1 | pilot_local |
| `satnogs_observations` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `noaa_kp` | `pilot_calls_utc_day` | provider | day | http_calls | 16 | 1 | pilot_local |
| `noaa_kp` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `open_er_api` | `pilot_calls_utc_day` | provider | day | http_calls | 4 | 1 | pilot_local |
| `open_er_api` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `oecd_cli` | `pilot_calls_utc_day` | provider | day | http_calls | 4 | 1 | pilot_local |
| `oecd_cli` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `iss_position` | `pilot_calls_utc_day` | provider | day | http_calls | 96 | 1 | pilot_local |
| `iss_position` | `pilot_calls_utc_second` | provider | second | http_calls | 1 | 1 | pilot_local |
| `disease_sh` | `pilot_calls_utc_day` | provider | day | http_calls | 4 | 1 | pilot_local |
| `disease_sh` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `ioda` | `pilot_calls_utc_day` | provider | day | http_calls | 24 | 1 | pilot_local |
| `ioda` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `nvd` | `pilot_calls_utc_day` | provider | day | http_calls | 8 | 1 | pilot_local |
| `nvd` | `official_unknown_calls_utc_day` | provider | day | http_calls | counted, no cap | 1 | official_unknown |
| `fred` | `pilot_calls_utc_day` | credential | day | http_calls | 8 | 1 | pilot_local |
| `fred` | `official_calls_per_minute` | credential | minute | http_calls | 120 | 1 | official_documented |
| `finnhub_news` | `pilot_calls_utc_day` | credential | day | http_calls | 48 | 1 | pilot_local |
| `finnhub_news` | `official_calls_per_minute` | credential | minute | http_calls | 60 | 1 | official_documented |
| `newsapi` | `pilot_calls_utc_day` | credential | day | http_calls | 12 | 1 | pilot_local |
| `newsapi` | `official_calls_per_day` | credential | day | http_calls | 100 | 1 | official_documented |
| `openweathermap` | `pilot_calls_utc_day` | credential | day | http_calls | 48 | 1 | pilot_local |
| `openweathermap` | `official_calls_per_minute` | credential | minute | http_calls | 60 | 1 | official_documented |
| `openaq` | `pilot_calls_utc_day` | credential | day | http_calls | 48 | 1 | pilot_local |
| `openaq` | `official_unknown_calls_utc_day` | credential | day | http_calls | counted, no cap | 1 | official_unknown |
| `nasa_firms` | `pilot_calls_utc_day` | credential | day | http_calls | 16 | 1 | pilot_local |
| `nasa_firms` | `official_unknown_calls_utc_day` | credential | day | http_calls | counted, no cap | 1 | official_unknown |

Alpha Vantage enforces 12 sends per UTC day locally (official 25/day is recorded separately) until the provider reset anchor is evidenced.

## Setup

1. Choose the preset; the source starts disabled. Fixed presets have no scope fields; the only configurable field is the schedule (`schedule_interval_minutes`).
2. Owner declares deployment use and acknowledges the terms version with `PUT /api/v1/connectors/{source_id}/terms`, body `{"declared_use": "personal" | "noncommercial" | "commercial" | "unknown", "terms_version": "<version>"}`. `GET /api/v1/connectors/{source_id}/terms` shows the current state. Owner only.
3. `review` providers: an instance operator records the terms review for the exact acknowledged version (state, allowed use, evidence reference). An owner tick cannot approve.
4. Key providers: Alpha Vantage, CoinGecko, FRED, Finnhub, NewsAPI, OpenWeather, OpenAQ and NASA FIRMS. Supply your own free-plan key; it is encrypted server-side (`CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`) and only a deployment-keyed fingerprint is used for the shared budget. CoinGecko takes its Demo key through `PUT /api/v1/connectors/{source_id}/credentials/native-rest` (body `{"expected_revision": <n>, "secret": "<key>"}`, native backend, owner only). The key is write-only: it is stored encrypted, sent only in the `x-cg-demo-api-key` header, and never returned, logged or placed in a URL. The source read reports only `provider_credential_configured` (boolean). Without a ready key, activation answers `invalid_credential` and a collection fails closed with `credential_missing` and no network call. Re-entering the key bumps the credential revision and cancels in-flight work.
5. Stock data for Vietnam stays unavailable; no unofficial Yahoo/SSI endpoint is used.

## Per-provider setup

Each entry gives what the source collects, prerequisites, cadence, quota and attribution. Quotas are the pilot ceilings above, not provider promises. Official caps that are not documented are recorded as counted but not enforced.

- **`bbc_world`**: BBC World News RSS feed (`/news/world/rss.xml`). Title, summary and link only. No key. Cadence 30 min. Attribution: show BBC as publisher with a link to the original article. Needs operator terms review.
- **`vnexpress_business`**: VnExpress business RSS feed (`/rss/kinh-doanh.rss`). Title, summary and link only. No key. Cadence 30 min. Attribution: identify VnExpress and link the original article. Noncommercial use only.
- **`thanhnien_news`**: Báo Thanh Niên home RSS feed (`/rss/home.rss`). Title, summary and link only. No key. Cadence 30 min. Attribution: credit "Báo Thanh Niên" or "Theo Báo Thanh Niên" and link the original article. Noncommercial use only; copying is prohibited without written consent.
- **`dantri_news`**: Dân trí news RSS feed (`/rss/home.rss`). Title, summary and link only. No key. Cadence 30 min. Attribution: credit Dân trí as the source and link the original article. Noncommercial use only: free for individuals and non-profits, must credit, the publisher may revoke at any time, and copying is prohibited without written consent.
- **`google_news`**: Google News RSS search feed (`news.google.com/rss/search`). Implemented, requires terms acknowledgement (class `review`: an operator must record approval first) and is **not runtime verified**. This is an unofficial RSS endpoint: Google publishes no API contract, quota or stability guarantee for it, and the feed can change or stop without notice. The owner never supplies a URL. The server builds it from validated scope only: `news_query` (1-200 characters, control characters stripped, no `site:` operator), `news_site` (`any`, `reuters.com`, `apnews.com`, `bbc.com`, `vnexpress.net`) and `news_locale` (`en-US` or `vi-VN`, mapped to `hl`/`gl`/`ceid`). Title, summary and link only; each item carries `publisher` from its `<source>` element and the licence label "Google News RSS / publisher". The item link is Google's article link; the original publisher article is not fetched. Cadence 30 min and the 96/day pilot ceiling are the same local choices as the other news feeds (see the quota table in `docs/free-data-provider-policy.md`); no official quota is claimed.
- **`openalex`**: OpenAlex works search (`api.openalex.org/works`, newest first, first page of 25). Implemented, **not runtime verified**. Data is CC0 (stated at help.openalex.org), so the class is `open`; terms acknowledgement is still required. No key is needed for basic use (a free key only raises the daily budget and is not used). The owner never supplies a URL: the server builds it from `scholarly_query` (1-200 characters, control characters stripped). One record per work: title plus the abstract rebuilt from `abstract_inverted_index` (bounded), with `doi`, `authors` (first 10), `source` and `cited_by_count` in the provider fields; `observed_at` is the `publication_date`. Cadence 1440 min with an 8/day pilot ceiling; OpenAlex documents no per-day number for keyless use, so no official quota is claimed.
- **`hn_top`**: Hacker News top stories. One list call plus at most 10 item calls per run (11 sends). No key. Cadence 60 min. Attribution: Hacker News with the item and publisher URL. Needs operator terms review.
- **`gdelt_economy`**: GDELT DOC 2.0 economy article list, 5 records, 24-hour window. Experimental. No key. Cadence 60 min. Attribution: cite the GDELT Project and the publisher URL. Needs operator terms review.
- **`world_bank`**: Annual GDP (current US$) for Vietnam, `NY.GDP.MKTP.CD`, up to 3 rows per response. No key. Cadence 1440 min. Attribution: credit the World Bank and the indicator; keep the dataset licence. Coverage is marked `truncated` when the response spans more than one page.
- **`frankfurter`**: USD/VND blended reference rate from `/v2/rate/usd/vnd`. No key. Cadence 1440 min. Attribution: credit Frankfurter and its contributing providers. The rate is a blend, not a single bank's rate. The v2 response shape is an unverified assumption.
- **`ecb`**: ECB euro reference rates, `eurofxref-daily.xml`. No key. Cadence 1440 min. Attribution: credit the ECB and keep the rate date. Each EUR cross rate in the feed becomes one record; no USD/VND or other cross rate is synthesised.
- **`binance`**: BTCUSDT last price from `data-api.binance.vision`. No key and no trading endpoints. Cadence 15 min. The quote is USDT, not USD. Attribution: credit Binance. Needs operator terms review.
- **`alternative_me`**: Fear and Greed index, last 2 points. No key. Cadence 1440 min. Attribution: show Alternative.me with a link next to the index, including translated output.
- **`usgs`**: USGS M4.5+ earthquakes, past day, GeoJSON summary. No key. Cadence 60 min. Detail URLs are never fetched. Attribution: credit the U.S. Geological Survey and cite the event.
- **`nasa_eonet`**: NASA EONET v3 open natural events (`/api/v3/events?status=open&limit=50`), at most 50 events, one record per event id. No key. Cadence 60 min. Classed `review` because EONET publishes only a disclaimer (visualization and general information only; extents are approximate), with no license, rate-limit or attribution terms. An operator must record terms approval first. Attribution: credit NASA EONET and the event's listed sources.
- **`cisa_kev`**: CISA Known Exploited Vulnerabilities catalog (CC0 JSON file in `cisagov/kev-data`, updated on US business days). No key. Cadence 1440 min. One record per CVE; only the 200 most recently added CVEs are kept and the run is marked truncated. Not runtime verified. Attribution: credit the CISA Known Exploited Vulnerabilities Catalog.
- **`coinpaprika`**: BTC ticker, `btc-bitcoin`. No key. Cadence 15 min. Personal use only, with no redistribution. Attribution: credit CoinPaprika.
- **`alpha_vantage`**: Daily equity OHLCV (raw, as traded), `TIME_SERIES_DAILY`. Key required. Cadence 1440 min. Quota: 12 sends per UTC day locally, with the official 25/day recorded separately. Personal use only under the catalog class. Example configuration (confirm symbols and exchange before activation):
- **`sec_edgar`**: SEC EDGAR recent filings for one company (`data.sec.gov/submissions/CIK##########.json`). Implemented, **not runtime verified**. The owner supplies a validated 10-digit CIK (`sec_cik`) and a contact email (`sec_contact_email`); the server builds the URL and sends `User-Agent: Umwelt-OS <email>` as the SEC requires a declared contact. A missing or invalid CIK or email fails closed before any request. SEC fair access allows at most 10 requests per second; this preset sends one request per run. One record per recent filing (newest 100): identity is the accession number, content is the form plus `primaryDocDescription`, `observed_at` is the filing date, and the filing document URL (`www.sec.gov/Archives/edgar/data/...`) is kept as a citation and never fetched. No key. Cadence 360 min. Attribution: Source: U.S. SEC EDGAR.

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

- **Feed-only news.** `bbc_world`, `vnexpress_business`, `dantri_news`, `google_news`, `hn_top` and `gdelt_economy` store title, summary, link and publisher. Linked article full text is not fetched and is not available. Summary text is capped at 4,000 characters and marked truncated when longer.
- **Publisher and licence label.** Each news record carries `publisher` (for example "BBC News") and `license_label`, which is the catalog attribution text. Both are meant to be shown beside the data. Whether the UI shows them is a P4-web item and is not verified here.
- **Frankfurter.** The rate date is the rate period, not a release instant. The v2 shape (`rate` field) is an unverified assumption until a live response is checked.
- **World Bank coverage.** Annual data with up to 3 rows. Coverage is `truncated` when more than one page is returned; exact continuation paging is not implemented.
- **Freshness (target rule, not yet verified in the UI).** The UI should show observed, published and collected times separately. A feed or quote is stale after two polling intervals. FX reference dates are kept over non-business days and are not presented as live. A failed source should show the last good value and the error, never a current-looking empty price.
- **Stock gaps.** Alpha Vantage EOD is conditional on the key and entitlement. SEC filings, FRED and TwelveData are research only (see [provider-research-2026-10-07.md](provider-research-2026-10-07.md)). Vietnam stock quotes are unavailable until an endpoint and its terms are verified. Manual file import is the only fallback offered; no paid fallback is added.

## Built-in expansion (2026-10-21)

- **Where each is set up.** Fixed-scope keyless presets: one switch under **Built-in sources**. Scoped presets (`google_news`, `sec_edgar`, `openalex`, `fred`, `openweathermap`, `openaq`, `nasa_firms`) and keyed presets (`coingecko`, `fred`, `finnhub_news`, `newsapi`, `openweathermap`, `openaq`, `nasa_firms`): choose the tile in the source editor; the **Setup guide** panel lists the numbered steps (get the account or key with the signup URL, the exact field to paste into, the scope format, the terms, Test connection) in en-us and vi-vi. Generic RSS, REST, Web and MCP sources have their own guides.
- **RSS/Atom presets** (`aljazeera_world`, `npr_news`, `fed_press`, `gdacs_rss`, `lobsters`, `us_travel_advisories`, `noaa_tsunami`, `jma_eqvol`) share the existing feed adapter: title, summary and link only.
- **JSON feeds** (`gdacs_events`, `nws_alerts`, `emsc_quakes`, `launch_library`, `wikipedia_mostread`, `wikipedia_top_views`, `bmkg_quakes`, `satnogs_observations`) are one declarative row each in `modules/connectors/providers/json_feed.py` and produce news-shaped records; coordinates, when present, are written into the summary text only.
- **Bespoke keyless mappers** (`noaa_kp`, `open_er_api`, `oecd_cli`, `iss_position`, `disease_sh`, `ioda`, `nvd`) emit typed measurements. `oecd_cli` uses the generic SDMX-JSON mapper `map_sdmx_json`; `open_er_api` has its own mapper because its body is not SDMX.
- **Probe status.** Every keyless endpoint above answered with the documented shape when probed on 2026-10-21 (see `docs/free-data-provider-policy.md` section 7). Keyed endpoints were **not** probed (no key may be used for a live call without owner approval); their mappers follow the providers' public documentation and fixtures only. Nothing is runtime-verified.
- **Dropped after probing:** `reliefweb_updates` (RSS answers server clients with an empty HTTP 202 challenge), `gvp_volcano_weekly` (HTTP 403 bot verification), `ransomware_live` (documented v2 paths return 404 without a paid key), `safecast` (public list ignores its ordering and returns years-old rows).
- **Deferred:** `acled` (now requires OAuth token exchange, which needs a POST the shared fetcher lacks), `opensky` (OAuth2), `un_comtrade` (heavy scope). **Excluded by design:** AISStream (websocket), ADS-B/AIS position firehoses, Yahoo Finance (unofficial), Reddit (blocks server IPs and needs OAuth), GDELT GEO and IMF DataMapper (unverified or 403), RestCountries (static data), ReliefWeb API (approved appname required; the RSS preset was dropped too), GitHub token (the OAuth connector exists).
- **Research provenance.** The plan drew candidates from worldmonitor, glance, FreshRSS, world-dashboard, WorldEconomicMap, world-economic-dashboard, globewatch, osiris and public API lists; a per-repository mapping was not recorded, so none is claimed here.
