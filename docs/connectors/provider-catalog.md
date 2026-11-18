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
  `binance`, `vn_stock`, `alternative_me`, `coinpaprika`, `coingecko`, `fed_press`, `open_er_api`, `oecd_cli`,
  `fred`. Credentials, usage terms, quota and provider entitlement remain provider-specific.
- **Hazards and environment:** `usgs`, `nasa_eonet`, `gdacs_rss`, `us_travel_advisories`,
  `noaa_tsunami`, `jma_eqvol`, `gdacs_events`, `nws_alerts`, `emsc_quakes`, `bmkg_quakes`,
  `openweathermap`, `openaq`, `nasa_firms`. Measurements and coordinates come from the provider;
  Open-Meteo is a three-day forecast, not climate history.
- **Security:** `cisa_kev`, `ioda`, `nvd`. CVE identifiers, provider timestamps and provenance are
  preserved by the registered mappers.
- **Space:** `launch_library`, `satnogs_observations`, `noaa_kp`, `iss_position`.
- **Media:** `anilist_anime` and `anilist_manga` collect up to ten keyless, 50-item AniList GraphQL pages per
  sync. Accepted pages and the next-page cursor commit together; updated mode uses a rolling 24-hour
  `updatedAt_greater` overlap and stable ID order, while trending uses its upstream ranking. Anime airing mode
  emits stable episode records whose next airing is within 14 days. `media_ids` is an optional scope target;
  genres, tags, formats, country, season/year, status and rating are pushed upstream and post-filtered.
  Adult results require explicit opt-in. Both share a conservative 60-request/minute durable cap and
  2.1-second in-process pacing. Live network and provider quota behavior remain unverified. `livechart_episodes`
  collects the official episode RSS with conditional requests and bounded
  anime metadata. `tvmaze_schedule` collects the current day's country or web schedule, with TVMaze
  attribution and source filters. Followed-show collection is not implemented. `openlibrary_books` collects
  subject, search, or recent-change work metadata from the official API, with a 100-item/4 MiB response
  bound, a 360-minute poll floor, CC0 attribution, and paginated recent-change cursors. Recent mode is
  capped at ten change rows plus ten work lookups per run, locally paced at three requests per second,
  and protected by a durable provider quota of two calls per second.
  `gutendex_books` collects public-domain ebook metadata from the official Gutendex API, requesting up to
  32 results per page and one page per sync. Language, topic, search, copyright, MIME type and author-year
  ranges are pushed upstream; normalized genres and media fields are post-filtered. The bounded same-host
  `next` URL is committed with accepted records, and the next request is rebuilt from the persisted scope.
  Local pacing is 1.1 seconds with durable one-per-second and 60-per-minute pilot caps. ANN news and encyclopedia,
  MangaUpdates releases, and MangaDex chapter releases use bounded official responses and preserve source attribution.
  Apple Books charts and Podcast RSS provide books and episode records; podcast inputs accept exactly one HTTPS feed
  or iTunes catalog ID. Wikidata and iTunes movie searches plus Letterboxd public RSS add keyless movie snapshots.
  These additions use the shared send gate, provider quotas, bounded parsing and atomic checkpoint/validator receipts.
  Terms-gated providers remain unavailable until required operator review; live acceptance remains open.
  Optional-key `tmdb_movies`, `tmdb_tv`, `trakt_media`, `omdb_lookup`, `mal_anime`, `mal_manga` and `google_books` each use one gated GET per run.
  YouTube Data API and Podcast Index use the gated native collection path. YouTube accepts the same target families
  and format filters as the Atom connector. Each successful sweep commits the upload validators and video records
  together with a ceiling of 40 one-unit requests. Podcast Index recent feeds use a publication watermark and
  language/category filters. Search by term supports value-block filtering on its own endpoint. Both require
  owner credentials and an operator terms review.
  MyAnimeList uses a client ID for each ranking or seasonal snapshot. Google Books uses an API key for each volumes search.
  `hardcover_books` sends a native GraphQL POST with the owner's bearer token. Each run reads up to two bounded
  pages, and the accepted receipt commits records, query fingerprint and ID cursor together. Live GraphQL schema
  verification remains pending.
  TMDB uses a query `api_key`, Trakt uses `trakt-api-key` plus `trakt-api-version: 2`, and OMDb uses a query
  `apikey`; request objects and HTTP client logs redact query credentials. TMDB Movie/TV genre IDs are mapped
  separately, normalized ratings use the canonical 0–100 scale, and required TMDB attribution is retained.
  Trakt accepts trending, anticipated, calendar and search modes. OMDb country, genre, language and minimum
  rating filters run after fetch because OMDb has no matching query parameters. These keyed snapshots are
  deduplicated without a time cursor; option fingerprints and accepted records commit in the same receipt.
- **Technology and research:** `reddit`, `bluesky`, `openalex`, `hn_top`, `lobsters`, `github_trending`, `wwr_jobs`, `vietnamworks_jobs`, `itviec_jobs`,
  `wikipedia_mostread`, `wikipedia_top_views`, `disease_sh`.
- **Other and account connectors:** generic `rss`, `web`, `rest`, and `mcp`; GitHub App and public
  releases; Mastodon, X, YouTube, arXiv, Hugging Face, Telegram; Google Gmail, Calendar and Drive;
  FHIR R4 `health`; Home Assistant; Notion and Slack; and Open-Meteo.
- **Manual imports:** Documents upload (no catalog entry) accepts Markdown notes, and
  finance mode accepts OFX 1/2 or UTF-8 CSV with the exact columns `account_id, transaction_id, date, amount,
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
| `livechart_episodes` | `implemented` | review | supported | yes | no | 30 | 2026-10-07 |
| `tvmaze_schedule` | `implemented` | review | supported | yes | no | 60 | 2026-11-17 |
| `anilist_anime` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `anilist_manga` | `implemented` | noncommercial | supported | yes | no | 60 | 2026-10-07 |
| `openlibrary_books` | `implemented` | open | supported | yes | no | 360 | 2026-10-07 |
| `gutendex_books` | `implemented` | review | supported | yes | no | 1440 | 2026-10-07 |
| `ann_news` | `implemented` | review | supported | yes | no | 60 | 2026-11-17 |
| `mangaupdates_releases` | `implemented` | review | supported | yes | no | 60 | 2026-11-17 |
| `mangadex_releases` | `implemented` | noncommercial | supported | yes | no | 60 | 2026-11-17 |
| `apple_books_rss` | `implemented` | review | supported | yes | no | 360 | 2026-11-17 |
| `podcast_rss` | `implemented` | review | supported | yes | no | 60 | 2026-11-17 |
| `wikidata_movies` | `implemented` | open | supported | yes | no | 360 | 2026-11-17 |
| `itunes_movies` | `implemented` | review | supported | yes | no | 360 | 2026-11-17 |
| `letterboxd_rss` | `implemented` | review | supported | yes | no | 360 | 2026-11-17 |
| `tmdb_movies` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
| `tmdb_tv` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
| `trakt_media` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
| `omdb_lookup` | `implemented` | personal | supported | yes | no | 1440 | 2026-11-18 |
| `mal_anime` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
| `mal_manga` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
| `youtube_api` | `implemented` | review | supported | yes | no | 30 | 2026-11-18 |
| `podcast_index` | `implemented` | review | supported | yes | no | 60 | 2026-11-18 |
| `listenbrainz_releases` | `implemented` | review | supported | yes | no | 360 | 2026-11-18 |
| `steam_releases` | `implemented` | review | supported | yes | no | 360 | 2026-11-18 |
| `igdb_games` | `implemented` | noncommercial | supported | yes | no | 360 | 2026-11-18 |
| `google_books` | `implemented` | personal | supported | yes | no | 1440 | 2026-11-18 |
| `hardcover_books` | `implemented` | personal | supported | yes | no | 360 | 2026-11-18 |
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
| `vn_stock` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
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
| `github_trending` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
| `wwr_jobs` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
| `vietnamworks_jobs` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
| `itviec_jobs` | `implemented` | review | supported | yes | no | 360 | 2026-10-21 |
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


## Media source configuration

The source editor exposes the keyless media providers' identifying fields and supported optional filters.
Open Library requires a subject or query for its selected mode, podcasts require one HTTPS feed URL or iTunes ID,
and YouTube accepts one target family with up to eight channel IDs, handles, or playlist IDs. Existing single-channel
YouTube configurations stay editable. YouTube format choices are all videos, long-form videos, or Shorts.

Media filters use the same canonical values as the Dashboard media editor. Leaving a filter empty uses the provider
default. Configure sends explicit null when a saved option is cleared, while the built-in wizard omits empty options.
The optional Media starter pack uses the existing LiveChart and TVMaze built-in contracts. These UI controls have
unit-test coverage. Live collection and authenticated browser acceptance remain separate verification gates.
