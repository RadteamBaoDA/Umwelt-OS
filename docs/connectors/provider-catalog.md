# Provider catalog

The backend catalog returned by `GET /api/v1/connectors/catalog` owns provider IDs, source types,
authentication methods, collection modes, scope fields, bounds, adapter availability and history
descriptions. The source wizard reads this catalog instead of maintaining a second supported-provider
list. UI category headings group concrete catalog rows; they are not connectors.

`code_available` means a provider reaches an adapter registered by the shared collection executor.
It does not prove that credentials, browser services, provider permissions, deployment policy or
live endpoints are ready. `availability` distinguishes implemented adapters from required
credentials and unsupported catalog ideas. Runtime verification remains separate: the providers
below have not been accepted against live services in this implementation pass.

## Source groups

- **News:** `bbc_world`, `vnexpress_business`, `tuoitre_news`, `thanhnien_news`, `dantri_news`,
  `google_news`, `gdelt_economy`, `aljazeera_world`, `npr_news`, `finnhub_news`, `newsapi`.
  Vietnamese press uses its named RSS feeds. GDELT returns bounded article metadata. Google News is
  an unofficial, scoped RSS preset; it is not an official Google API.
- **Economy and markets:** `alpha_vantage`, `sec_edgar`, `world_bank`, `frankfurter`, `ecb`,
  `binance`, `alternative_me`, `coinpaprika`, `coingecko`, `fed_press`, `open_er_api`, `oecd_cli`,
  `fred`. Credentials, usage terms, quota and provider entitlement remain provider-specific.
- **Hazards and environment:** `usgs`, `nasa_eonet`, `gdacs_rss`, `us_travel_advisories`,
  `noaa_tsunami`, `jma_eqvol`, `gdacs_events`, `nws_alerts`, `emsc_quakes`, `bmkg_quakes`,
  `openweathermap`, `openaq`, `nasa_firms`. Measurements and coordinates come from the provider;
  Open-Meteo is a three-day forecast, not climate history.
- **Security:** `cisa_kev`, `ioda`, `nvd`. CVE identifiers, provider timestamps and provenance are
  preserved by the registered mappers.
- **Space:** `launch_library`, `satnogs_observations`, `noaa_kp`, `iss_position`.
- **Technology and research:** `reddit`, `bluesky`, `openalex`, `hn_top`, `lobsters`,
  `wikipedia_mostread`, `wikipedia_top_views`, `disease_sh`.
- **Other and account connectors:** generic `rss`, `web`, `rest`, and `mcp`; GitHub App and public
  releases; Mastodon, X, YouTube, arXiv, Hugging Face, Telegram; Google Gmail, Calendar and Drive;
  FHIR R4 `health`; Home Assistant; Notion and Slack; and Open-Meteo.
- **Manual imports:** `notes` accepts Markdown through the existing upload receipt. `personal_finance`
  accepts OFX 1/2 or UTF-8 CSV with the exact columns `account_id, transaction_id, date, amount,
  currency, description`; dates must be ISO `YYYY-MM-DD`, and money is parsed as `Decimal`. Imports
  remain in a local-only manual source and do not connect to a bank or create transactions.

## Collection contracts and limits

- Gmail reads selected labels over an initial 30-day window, then continues from Gmail history
  cursors. It does not download attachments. Calendar starts 30 days in the past and 90 days ahead;
  per-calendar sync tokens retain recurrence and cancellation state. Drive snapshots selected
  files, exports Google Docs as text, reads only formats already supported by the document parser,
  and follows Drive changes. All three use separate read-only connector OAuth grants and advance
  cursors only with their durable ingestion receipts.
- Reddit collects public posts from one selected subreddit using read-only OAuth. Bluesky reads
  public account posts through AppView. Mastodon reads one explicitly selected public account or
  hashtag from its configured HTTPS instance, optionally with a read token. X reads a configured
  public account or recent query using the owner's app bearer token and API entitlement. These
  adapters do not request write access.
- Notion uses an internal integration token and the pinned API version `2026-03-11`; it reads only
  selected page text, not attachments, databases or workspace-wide search. Slack uses a bot token
  to read top-level messages from selected channels where the bot has access. DMs and thread replies
  are excluded; rate limits are returned to the shared scheduler.
- FHIR reads patient-scoped R4 `Observation` resources from the configured origin and preserves
  source ID, value, unit and measurement time. Home Assistant reads only selected entity states and
  actual source coordinates; it is a bounded snapshot with a scope cursor, not history or device
  control. Both use the exact configured private origin and port, reject redirects and enforce
  resolved-address checks.
- Generic Web supports static pages and JavaScript rendering through the existing isolated browser
  worker. Crawl URL, depth, page count, response bytes and timeout are bounded. The browser worker
  must be configured for JavaScript mode; it is not a separate provider adapter.
- Fixed-scope presets expose their returned-snapshot limits through `history_description` in the
  backend catalog. The catalog's default schedule and provider-specific terms remain authoritative;
  a snapshot provider does not imply complete history or removal detection.
- Pausing or disconnecting revokes collection authority but retains ingested data. Purge remains
  the separate existing source-erasure operation. New personal manual, Google, health, Home
  Assistant, Notion and Slack sources are created `local_only` by the Sources owner.

The catalog and its provider-specific quota, terms, endpoint and attribution facts are maintained
in `modules/connectors/catalog.py` and `modules/connectors/provider_specs.py`. Free-provider usage
details are in [free-sources.md](free-sources.md). Adapter code and mock/build evidence do not
establish provider terms, credentials, live service behavior or deployment acceptance.

## Inventory

Generated from `modules/connectors/catalog.py` and `modules/connectors/provider_specs.py`. Read the table as follows:

- **Availability** is the catalog's own value and governs source selection. `implemented` means a collection adapter is registered; it does not mean the provider works live.
- **Code registered** means the shared collection executor has an adapter wired for the provider. It is not a runtime, licence or deployment acceptance.
- **Runtime verified** is `no` for every row. No provider has live verification yet; endpoint shapes and quotas are plan-documented, not observed.
- **Eligibility** is the catalog class (`open`, `personal`, `noncommercial`, `review`) or `unknown` for providers without a free preset. It is never a grant for a workspace; see `free-sources.md`.
- `google_news` is a scoped preset on an unofficial Google News RSS endpoint with no official API contract. It requires terms acknowledgement plus operator review and is not runtime verified.
- `coingecko` requires an owner-supplied API key and fails closed without one.

| ID | Availability | Eligibility | Execution | Code registered | Runtime verified | Default interval (min) | Terms checked |
|---|---|---|---|---|---|---|---|
| `rss` | `available` | unknown | - | yes | no | - | - |
| `web` | `available` | unknown | - | yes | no | - | - |
| `rest` | `available` | unknown | - | yes | no | - | - |
| `mcp` | `available` | unknown | - | yes | no | - | - |
| `github` | `requires_credentials` | unknown | - | yes | no | - | - |
| `reddit` | `requires_credentials` | unknown | - | yes | no | - | - |
| `bluesky` | `implemented` | review | supported | yes | no | 30 | 2026-10-24 |
| `mastodon` | `implemented` | unknown | - | yes | no | - | - |
| `x` | `requires_credentials` | unknown | - | yes | no | - | - |
| `google_mail` | `requires_credentials` | unknown | - | yes | no | - | - |
| `google_calendar` | `requires_credentials` | unknown | - | yes | no | - | - |
| `google_drive` | `requires_credentials` | unknown | - | yes | no | - | - |
| `health` | `requires_credentials` | unknown | - | yes | no | - | - |
| `home_assistant` | `requires_credentials` | unknown | - | yes | no | - | - |
| `youtube` | `implemented` | unknown | - | yes | no | - | - |
| `arxiv` | `implemented` | unknown | - | yes | no | - | - |
| `huggingface` | `implemented` | unknown | - | yes | no | - | - |
| `github_releases` | `implemented` | unknown | - | yes | no | - | - |
| `telegram` | `requires_credentials` | unknown | - | yes | no | - | - |
| `alpha_vantage` | `requires_credentials` | personal | supported | yes | no | 1440 | 2026-10-07 |
| `open_meteo` | `implemented` | unknown | - | yes | no | - | - |
| `notes` | `available` | unknown | - | yes | no | - | - |
| `personal_finance` | `available` | unknown | - | yes | no | - | - |
| `notion` | `requires_credentials` | unknown | - | yes | no | - | - |
| `slack` | `requires_credentials` | unknown | - | yes | no | - | - |
| `bbc_world` | `implemented` | review | supported | yes | no | 30 | 2026-10-07 |
| `vnexpress_business` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `tuoitre_news` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `thanhnien_news` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `dantri_news` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `google_news` | `implemented` | review | supported | yes | no | 30 | 2026-10-07 |
| `sec_edgar` | `implemented` | open | supported | yes | no | 360 | 2026-10-07 |
| `openalex` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `hn_top` | `implemented` | review | supported | yes | no | 60 | 2026-10-07 |
| `gdelt_economy` | `implemented` | review | experimental | yes | no | 60 | 2026-10-07 |
| `world_bank` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `frankfurter` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `ecb` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `binance` | `implemented` | review | supported | yes | no | 15 | 2026-10-07 |
| `alternative_me` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `usgs` | `implemented` | open | supported | yes | no | 60 | 2026-10-07 |
| `nasa_eonet` | `implemented` | review | supported | yes | no | 60 | 2026-10-07 |
| `cisa_kev` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `coinpaprika` | `implemented` | personal | supported | yes | no | 15 | 2026-10-07 |
| `coingecko` | `implemented` | review | supported | yes | no | 15 | 2026-10-07 |
| `aljazeera_world` | `implemented` | review | supported | yes | no | 30 | 2026-10-07 |
| `npr_news` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-21 |
| `fed_press` | `implemented` | open | supported | yes | no | 60 | 2026-10-21 |
| `gdacs_rss` | `implemented` | open | supported | yes | no | 60 | 2026-10-21 |
| `lobsters` | `implemented` | review | supported | yes | no | 60 | 2026-10-21 |
| `us_travel_advisories` | `implemented` | open | supported | yes | no | 360 | 2026-10-21 |
| `noaa_tsunami` | `implemented` | open | supported | yes | no | 30 | 2026-10-21 |
| `jma_eqvol` | `implemented` | open | supported | yes | no | 30 | 2026-10-21 |
| `gdacs_events` | `implemented` | open | supported | yes | no | 60 | 2026-10-21 |
| `nws_alerts` | `implemented` | open | supported | yes | no | 15 | 2026-10-21 |
| `emsc_quakes` | `implemented` | open | supported | yes | no | 30 | 2026-10-21 |
| `launch_library` | `implemented` | noncommercial | supported | yes | no | 360 | 2026-10-21 |
| `wikipedia_mostread` | `implemented` | open | supported | yes | no | 1440 | 2026-10-21 |
| `wikipedia_top_views` | `implemented` | open | supported | yes | no | 1440 | 2026-10-21 |
| `bmkg_quakes` | `implemented` | open | supported | yes | no | 30 | 2026-10-21 |
| `satnogs_observations` | `implemented` | open | supported | yes | no | 360 | 2026-10-21 |
| `noaa_kp` | `implemented` | open | supported | yes | no | 360 | 2026-10-21 |
| `open_er_api` | `implemented` | review | supported | yes | no | 1440 | 2026-10-21 |
| `oecd_cli` | `implemented` | open | supported | yes | no | 1440 | 2026-10-21 |
| `iss_position` | `implemented` | noncommercial | supported | yes | no | 15 | 2026-10-21 |
| `disease_sh` | `implemented` | review | supported | yes | no | 1440 | 2026-10-21 |
| `ioda` | `implemented` | noncommercial | supported | yes | no | 60 | 2026-10-21 |
| `nvd` | `implemented` | open | supported | yes | no | 360 | 2026-10-21 |
| `fred` | `implemented` | review | supported | yes | no | 1440 | 2026-10-21 |
| `finnhub_news` | `implemented` | personal | supported | yes | no | 60 | 2026-10-21 |
| `newsapi` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
| `openweathermap` | `implemented` | open | supported | yes | no | 60 | 2026-10-21 |
| `openaq` | `implemented` | open | supported | yes | no | 60 | 2026-10-21 |
| `nasa_firms` | `implemented` | open | supported | yes | no | 360 | 2026-10-21 |
